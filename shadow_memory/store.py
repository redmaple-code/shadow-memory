"""SQLite storage engine: WAL journal, single-writer discipline, version chains.

Design notes
------------
* **WAL + one writer lock.**  ``ThreadingHTTPServer`` serves queries from many
  threads while the daemon writes.  SQLite only allows one writer at a time, so
  every mutating call goes through ``self._write_lock`` (an ``RLock``), which
  turns "database is locked" errors into a queue instead of an exception.
* **FTS5 with CJK bigrams.**  The stock ``unicode61`` tokenizer treats a whole
  run of Han characters as one token, so ``记忆`` would never match ``影子记忆服务``.
  We therefore store a bigram-expanded shadow column and expand queries the same
  way: every Han bigram becomes its own FTS token.
* **Version chains.**  ``supersedes_id`` points backwards, ``superseded_by``
  points forwards, both stored inside the row so a chain can be walked without
  extra tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .redact import redact

DB_FILENAME = "memory.sqlite"
ENV_HOME = "SHADOW_MEMORY_HOME"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_FORGOTTEN = "forgotten"
STATUS_EXPIRED = "expired"

MERGE_THRESHOLD = 0.80
SHORT_ID_LEN = 6

# --- CJK handling ------------------------------------------------------------

_CJK_RX = re.compile(
    "[\u3400-\u4dbf"  # CJK ext A
    "\u4e00-\u9fff"  # CJK unified
    "\uf900-\ufaff"  # compatibility ideographs
    "\u3040-\u30ff"  # hiragana / katakana
    "\uac00-\ud7af"  # hangul
    "]"
)
_WORD_RX = re.compile(r"[A-Za-z0-9_]+")


def fts_tokens(text: str) -> List[str]:
    """Split text into FTS tokens; Han runs become overlapping bigrams."""
    if not text:
        return []
    toks: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if _CJK_RX.match(text[i]):
            j = i
            while j < n and _CJK_RX.match(text[j]):
                j += 1
            run = text[i:j]
            if len(run) == 1:
                toks.append(run)
            else:
                toks.extend(run[k : k + 2] for k in range(len(run) - 1))
            i = j
        else:
            j = i
            while j < n and not _CJK_RX.match(text[j]):
                j += 1
            toks.extend(_WORD_RX.findall(text[i:j]))
            i = j
    return [t for t in toks if t.strip()]


def fts_index_text(text: str) -> str:
    return " ".join(fts_tokens(text))


def fts_query(text: str) -> Optional[str]:
    toks = fts_tokens(text)
    if not toks:
        return None
    quoted = ('"' + t.replace('"', '""') + '"' for t in toks)
    return " AND ".join(quoted)


# --- misc helpers ------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(value: Optional[str]) -> float:
    dt = parse_iso(value)
    if dt is None:
        return float("inf")
    return (utcnow() - dt).total_seconds()


def resolve_home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".shadow-memory"


def resolve_db_path(path: Optional[os.PathLike | str] = None) -> Path:
    if path is not None:
        p = Path(path).expanduser()
        if p.is_dir():
            return p / DB_FILENAME
        return p
    return resolve_home() / DB_FILENAME


def project_id_for(root: str) -> str:
    """Stable identifier derived from the *normalised* project root."""
    try:
        norm = os.path.normcase(os.path.realpath(str(root)))
    except OSError:  # pragma: no cover
        norm = os.path.normcase(str(root))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _normalise_for_sim(text: str) -> str:
    text = (text or "").lower()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def _bigrams(text: str) -> set:
    norm = _normalise_for_sim(text)
    if not norm:
        return set()
    if len(norm) == 1:
        return {norm}
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def similarity(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity (language agnostic)."""
    na, nb = _normalise_for_sim(a), _normalise_for_sim(b)
    if na == nb:
        return 1.0
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    project_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root        TEXT NOT NULL UNIQUE,
    last_active TEXT
);

CREATE TABLE IF NOT EXISTS memory_fact (
    fact_id        TEXT PRIMARY KEY,
    scope          TEXT NOT NULL DEFAULT 'project',
    project_id     TEXT,
    kind           TEXT NOT NULL DEFAULT 'fact',
    content        TEXT NOT NULL,
    evidence       TEXT DEFAULT '',
    confidence     REAL NOT NULL DEFAULT 0.8,
    status         TEXT NOT NULL DEFAULT 'active',
    supersedes_id  TEXT,
    superseded_by  TEXT,
    source_agent   TEXT DEFAULT '',
    source_session TEXT DEFAULT '',
    ttl_days       INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    forget_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_project ON memory_fact(project_id, status);
CREATE INDEX IF NOT EXISTS idx_fact_kind    ON memory_fact(kind);
CREATE INDEX IF NOT EXISTS idx_fact_sup     ON memory_fact(supersedes_id);

CREATE TABLE IF NOT EXISTS session_digest (
    session_key   TEXT PRIMARY KEY,
    agent         TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    project_id    TEXT,
    cwd           TEXT DEFAULT '',
    clean_title   TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    outcome       TEXT DEFAULT '',
    next_steps    TEXT DEFAULT '',
    touched_files TEXT DEFAULT '[]',
    digest_source TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_project ON session_digest(project_id);
CREATE INDEX IF NOT EXISTS idx_digest_updated ON session_digest(updated_at);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fact_fts USING fts5(
    fact_id UNINDEXED,
    content,
    search_text,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


class Store:
    """Thread-safe façade over one SQLite database."""

    def __init__(self, path: Optional[os.PathLike | str] = None, *, create: bool = True):
        self.path = resolve_db_path(path)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        if create:
            self._init_schema()

    # -- connection plumbing ------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=10.0, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.executescript(_FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self):
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._write_lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- projects -----------------------------------------------------------

    def ensure_project(self, root: str, name: Optional[str] = None) -> Dict[str, Any]:
        root = os.path.abspath(str(root))
        pid = project_id_for(root)
        pname = name or os.path.basename(root.rstrip("\\/")) or root
        now = iso()
        with self._write_lock:
            row = self._conn.execute(
                "SELECT * FROM project WHERE project_id=?", (pid,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO project(project_id, name, root, last_active) VALUES (?,?,?,?)",
                    (pid, pname, root, now),
                )
            else:
                self._conn.execute(
                    "UPDATE project SET last_active=?, name=COALESCE(?, name) WHERE project_id=?",
                    (now, name, pid),
                )
        return self.get_project(pid) or {"project_id": pid, "name": pname, "root": root}

    def touch_project(self, project_id: Optional[str]) -> None:
        if not project_id:
            return
        with self._write_lock:
            self._conn.execute(
                "UPDATE project SET last_active=? WHERE project_id=?", (iso(), project_id)
            )

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM project WHERE project_id=?", (project_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_project_by_root(self, root: str) -> Optional[Dict[str, Any]]:
        return self.get_project(project_id_for(os.path.abspath(str(root))))

    def list_projects(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM memory_fact f
                      WHERE f.project_id=p.project_id AND f.status='active') AS fact_count,
                   (SELECT COUNT(*) FROM session_digest s
                      WHERE s.project_id=p.project_id) AS session_count
              FROM project p
             ORDER BY COALESCE(p.last_active,'') DESC, p.name
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # -- fact ids -----------------------------------------------------------

    def _new_fact_id(self) -> str:
        while True:
            fid = uuid.uuid4().hex[:16]
            exists = self._conn.execute(
                "SELECT 1 FROM memory_fact WHERE fact_id=? OR substr(fact_id,1,6)=?",
                (fid, fid[:SHORT_ID_LEN]),
            ).fetchone()
            if not exists:
                return fid

    def resolve_fact_id(self, token: str) -> str:
        """Accept a full id, ``#shortid`` or a bare prefix; return the full id."""
        if not token:
            raise KeyError("empty fact id")
        t = str(token).strip().lstrip("#").strip()
        if not t:
            raise KeyError("empty fact id")
        row = self._conn.execute(
            "SELECT fact_id FROM memory_fact WHERE fact_id=?", (t,)
        ).fetchone()
        if row:
            return row["fact_id"]
        rows = self._conn.execute(
            "SELECT fact_id FROM memory_fact WHERE fact_id LIKE ?", (t + "%",)
        ).fetchall()
        if not rows:
            raise KeyError(f"no fact matches {token!r}")
        if len(rows) > 1:
            raise KeyError(f"ambiguous fact id {token!r} ({len(rows)} matches)")
        return rows[0]["fact_id"]

    # -- facts --------------------------------------------------------------

    def write_fact(
        self,
        *,
        content: str,
        project_id: Optional[str] = None,
        scope: str = "project",
        kind: str = "fact",
        evidence: str = "",
        confidence: float = 0.8,
        source_agent: str = "",
        source_session: str = "",
        ttl_days: Optional[int] = None,
        supersedes_id: Optional[str] = None,
        force_new: bool = False,
    ) -> Dict[str, Any]:
        content = redact((content or "").strip())
        evidence = redact((evidence or "").strip())
        if not content:
            raise ValueError("content must not be empty")

        result: Dict[str, Any] = {"status": "created", "fact_id": None}
        with self._write_lock:
            # 1. explicit supersede
            replace_target: Optional[str] = None
            if supersedes_id:
                replace_target = self.resolve_fact_id(supersedes_id)

            # 2. implicit dedupe (bigram Jaccard)
            if replace_target is None and not force_new:
                best_id, best_sim = None, 0.0
                q = "SELECT fact_id, content FROM memory_fact WHERE status='active' AND scope=? AND kind=?"
                params: List[Any] = [scope, kind]
                if project_id:
                    q += " AND project_id=?"
                    params.append(project_id)
                else:
                    q += " AND project_id IS NULL"
                for row in self._conn.execute(q, params).fetchall():
                    sim = similarity(content, row["content"])
                    if sim > best_sim:
                        best_id, best_sim = row["fact_id"], sim
                if best_id and best_sim >= MERGE_THRESHOLD:
                    self._merge_into(best_id, content, evidence, confidence)
                    result.update(status="merged", fact_id=best_id,
                                  similarity=round(best_sim, 4))
                    result["short_id"] = best_id[:SHORT_ID_LEN]
                    return result

            now = iso()
            fid = self._new_fact_id()
            self._conn.execute(
                """
                INSERT INTO memory_fact(
                    fact_id, scope, project_id, kind, content, evidence, confidence,
                    status, supersedes_id, superseded_by, source_agent, source_session,
                    ttl_days, created_at, updated_at, forget_reason
                ) VALUES (?,?,?,?,?,?,?,'active',?,NULL,?,?,?,?,?,NULL)
                """,
                (
                    fid, scope, project_id, kind, content, evidence, float(confidence),
                    replace_target, source_agent, source_session, ttl_days, now, now,
                ),
            )
            self._fts_upsert(fid, content)

            if replace_target:
                self._conn.execute(
                    "UPDATE memory_fact SET status=?, superseded_by=?, updated_at=? WHERE fact_id=?",
                    (STATUS_SUPERSEDED, fid, now, replace_target),
                )
                result.update(status="superseded", superseded=replace_target)

            if project_id:
                self._conn.execute(
                    "UPDATE project SET last_active=? WHERE project_id=?", (now, project_id)
                )

        result["fact_id"] = fid
        result["short_id"] = fid[:SHORT_ID_LEN]
        return result

    def _merge_into(self, fact_id: str, content: str, evidence: str, confidence: float) -> None:
        row = self._conn.execute(
            "SELECT * FROM memory_fact WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if row is None:  # pragma: no cover
            return
        new_conf = max(float(row["confidence"]), float(confidence))
        new_evidence = row["evidence"] or ""
        if evidence and evidence not in new_evidence:
            new_evidence = (new_evidence + " | " + evidence).strip(" |") if new_evidence else evidence
        # prefer the longer, more informative phrasing
        new_content = content if len(content) > len(row["content"]) else row["content"]
        self._conn.execute(
            "UPDATE memory_fact SET content=?, evidence=?, confidence=?, updated_at=? WHERE fact_id=?",
            (new_content, new_evidence, new_conf, iso(), fact_id),
        )
        self._fts_upsert(fact_id, new_content)

    def _fts_upsert(self, fact_id: str, content: str) -> None:
        if not getattr(self, "fts_enabled", False):
            return
        try:
            self._conn.execute("DELETE FROM memory_fact_fts WHERE fact_id=?", (fact_id,))
            self._conn.execute(
                "INSERT INTO memory_fact_fts(fact_id, content, search_text) VALUES (?,?,?)",
                (fact_id, content, fts_index_text(content)),
            )
        except sqlite3.OperationalError:  # pragma: no cover
            self.fts_enabled = False

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        try:
            fid = self.resolve_fact_id(fact_id)
        except KeyError:
            return None
        row = self._conn.execute(
            "SELECT * FROM memory_fact WHERE fact_id=?", (fid,)
        ).fetchone()
        return dict(row) if row else None

    def forget_fact(self, fact_id: str, reason: str = "") -> Dict[str, Any]:
        fid = self.resolve_fact_id(fact_id)
        with self._write_lock:
            self._conn.execute(
                "UPDATE memory_fact SET status=?, forget_reason=?, updated_at=? WHERE fact_id=?",
                (STATUS_FORGOTTEN, redact(reason or ""), iso(), fid),
            )
        fact = self.get_fact(fid) or {}
        return {"fact_id": fid, "status": fact.get("status"), "short_id": fid[:SHORT_ID_LEN]}

    def restore_fact(self, fact_id: str) -> Dict[str, Any]:
        fid = self.resolve_fact_id(fact_id)
        with self._write_lock:
            row = self._conn.execute(
                "SELECT superseded_by, status FROM memory_fact WHERE fact_id=?", (fid,)
            ).fetchone()
            # a fact that was superseded stays superseded on restore unless its
            # successor is gone; only 'forgotten'/'expired' come back to active
            target = STATUS_ACTIVE
            if row and row["superseded_by"]:
                target = STATUS_SUPERSEDED
            self._conn.execute(
                "UPDATE memory_fact SET status=?, forget_reason=NULL, updated_at=? WHERE fact_id=?",
                (target, iso(), fid),
            )
        fact = self.get_fact(fid) or {}
        return {"fact_id": fid, "status": fact.get("status"), "short_id": fid[:SHORT_ID_LEN]}

    def supersede(self, old_fact_id: str, new_content: str, **kwargs) -> Dict[str, Any]:
        old = self.get_fact(old_fact_id)
        if not old:
            raise KeyError(f"no fact matches {old_fact_id!r}")
        kwargs.setdefault("project_id", old.get("project_id"))
        kwargs.setdefault("scope", old.get("scope"))
        kwargs.setdefault("kind", old.get("kind"))
        kwargs.setdefault("source_agent", old.get("source_agent") or "")
        return self.write_fact(new_content, supersedes_id=old["fact_id"], **kwargs)

    def get_fact_chain(self, fact_id: str) -> List[Dict[str, Any]]:
        """Return the full version chain, oldest → newest.

        Order is derived by *walking the pointers* (back to the root, then
        forward to the tip), never by timestamp — two versions written inside
        the same second would otherwise sort arbitrarily.
        """
        fid = self.resolve_fact_id(fact_id)
        start = self.get_fact(fid)
        if not start:
            return []

        # 1. walk backwards to the oldest ancestor
        oldest = start
        seen_back: set = {oldest["fact_id"]}
        guard = 0
        while oldest.get("supersedes_id") and guard < 500:
            nxt = self.get_fact(oldest["supersedes_id"])
            if not nxt or nxt["fact_id"] in seen_back:
                break
            seen_back.add(nxt["fact_id"])
            oldest = nxt
            guard += 1

        # 2. walk forwards from there, collecting the whole chain
        chain: List[Dict[str, Any]] = []
        seen: set = set()
        cursor: Optional[Dict[str, Any]] = oldest
        guard = 0
        while cursor and guard < 500:
            if cursor["fact_id"] in seen:
                break
            seen.add(cursor["fact_id"])
            chain.append(cursor)
            nxt_id = cursor.get("superseded_by")
            cursor = self.get_fact(nxt_id) if nxt_id else None
            guard += 1
        return chain

    def search_facts(
        self,
        q: Optional[str] = None,
        project_id: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT f.* FROM memory_fact f"
        where: List[str] = []
        params: List[Any] = []
        if q:
            fq = fts_query(q)
            if fq and getattr(self, "fts_enabled", False):
                # MATCH must reference the table name, not an alias
                sql = (
                    "SELECT f.* FROM memory_fact_fts "
                    "JOIN memory_fact f ON f.fact_id=memory_fact_fts.fact_id"
                )
                where.append("memory_fact_fts MATCH ?")
                params.append(fq)
            else:
                where.append("(f.content LIKE ? OR f.evidence LIKE ? OR f.kind LIKE ?)")
                like = f"%{q}%"
                params.extend([like, like, like])
        if project_id:
            where.append("f.project_id = ?")
            params.append(project_id)
        if kind:
            where.append("f.kind = ?")
            params.append(kind)
        if status:
            where.append("f.status = ?")
            params.append(status)
        elif not include_expired:
            where.append("f.status IN ('active','superseded')")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY f.updated_at DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS syntax problem → degrade to LIKE
            like = f"%{q}%"
            rows = self._conn.execute(
                "SELECT * FROM memory_fact WHERE content LIKE ? ORDER BY updated_at DESC LIMIT ?",
                (like, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def expire_due(self) -> int:
        now = utcnow()
        expired = 0
        rows = self._conn.execute(
            "SELECT fact_id, created_at, ttl_days FROM memory_fact "
            "WHERE status='active' AND ttl_days IS NOT NULL AND ttl_days > 0"
        ).fetchall()
        with self._write_lock:
            for r in rows:
                created = parse_iso(r["created_at"])
                if created and created + timedelta(days=int(r["ttl_days"])) <= now:
                    self._conn.execute(
                        "UPDATE memory_fact SET status=?, updated_at=? WHERE fact_id=?",
                        (STATUS_EXPIRED, iso(), r["fact_id"]),
                    )
                    expired += 1
        return expired

    def counts(self) -> Dict[str, int]:
        def one(sql: str, args: Iterable[Any] = ()) -> int:
            row = self._conn.execute(sql, tuple(args)).fetchone()
            return int(row[0]) if row else 0

        return {
            "projects": one("SELECT COUNT(*) FROM project"),
            "facts_active": one("SELECT COUNT(*) FROM memory_fact WHERE status='active'"),
            "facts_total": one("SELECT COUNT(*) FROM memory_fact"),
            "sessions": one("SELECT COUNT(*) FROM session_digest"),
        }

    # -- session digests ----------------------------------------------------

    def upsert_session_digest(
        self,
        *,
        agent: str,
        session_id: str,
        project_id: Optional[str] = None,
        cwd: str = "",
        clean_title: str = "",
        summary: str = "",
        outcome: str = "",
        next_steps: str = "",
        touched_files: Optional[List[str]] = None,
        digest_source: str = "",
        message_count: int = 0,
        updated_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = f"{agent}:{session_id}" if session_id else f"{agent}:{hashlib.sha1((cwd + clean_title).encode()).hexdigest()[:16]}"
        row = {
            "session_key": key,
            "agent": agent,
            "session_id": session_id,
            "project_id": project_id,
            "cwd": str(cwd or ""),
            "clean_title": redact(clean_title or ""),
            "summary": redact(summary or ""),
            "outcome": redact(outcome or ""),
            "next_steps": redact(next_steps or ""),
            "touched_files": json.dumps(list(touched_files or []), ensure_ascii=False),
            "digest_source": digest_source,
            "message_count": int(message_count or 0),
            "updated_at": updated_at or iso(),
        }
        with self._write_lock:
            existing = self._conn.execute(
                "SELECT updated_at FROM session_digest WHERE session_key=?", (key,)
            ).fetchone()
            if existing and (existing["updated_at"] or "") > row["updated_at"]:
                return dict(existing, session_key=key, skipped=True)
            self._conn.execute(
                """
                INSERT INTO session_digest(session_key, agent, session_id, project_id, cwd,
                    clean_title, summary, outcome, next_steps, touched_files,
                    digest_source, message_count, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                    agent=excluded.agent, session_id=excluded.session_id,
                    project_id=excluded.project_id, cwd=excluded.cwd,
                    clean_title=excluded.clean_title, summary=excluded.summary,
                    outcome=excluded.outcome, next_steps=excluded.next_steps,
                    touched_files=excluded.touched_files, digest_source=excluded.digest_source,
                    message_count=excluded.message_count, updated_at=excluded.updated_at
                """,
                tuple(row.values()),
            )
        return row

    def list_sessions(self, project_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if project_id:
            rows = self._conn.execute(
                "SELECT * FROM session_digest WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
                (project_id, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM session_digest ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["touched_files"] = json.loads(d.get("touched_files") or "[]")
            except (ValueError, TypeError):
                d["touched_files"] = []
            d["age_seconds"] = age_seconds(d.get("updated_at"))
            out.append(d)
        return out

    def get_session(self, session_key: str) -> Optional[Dict[str, Any]]:
        r = self._conn.execute(
            "SELECT * FROM session_digest WHERE session_key=?", (session_key,)
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["touched_files"] = json.loads(d.get("touched_files") or "[]")
        except (ValueError, TypeError):
            d["touched_files"] = []
        d["age_seconds"] = age_seconds(d.get("updated_at"))
        return d

    def latest_session_update_seconds(self, project_id: Optional[str] = None) -> float:
        if project_id:
            row = self._conn.execute(
                "SELECT MAX(updated_at) FROM session_digest WHERE project_id=?", (project_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT MAX(updated_at) FROM session_digest").fetchone()
        return age_seconds(row[0] if row else None)

    def reindex_fts(self) -> int:
        if not getattr(self, "fts_enabled", False):
            return 0
        with self._write_lock:
            self._conn.execute("DELETE FROM memory_fact_fts")
            rows = self._conn.execute("SELECT fact_id, content FROM memory_fact").fetchall()
            self._conn.executemany(
                "INSERT INTO memory_fact_fts(fact_id, content, search_text) VALUES (?,?,?)",
                [(r["fact_id"], r["content"], fts_index_text(r["content"])) for r in rows],
            )
        return len(rows)


def open_store(path: Optional[os.PathLike | str] = None) -> Store:
    return Store(path)