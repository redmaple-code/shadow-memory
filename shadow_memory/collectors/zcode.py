"""Collector: ZCode session database (``~/.zcode/cli/db/db.sqlite``).

Read strictly read-only through a ``file:...?mode=ro`` URI so an open ZCode
instance is never blocked or corrupted.  Any missing table/column degrades to
an empty result instead of raising.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..store import iso
from . import common

AGENT = "zcode"

# part types whose text is worth keeping
_TEXT_PART_TYPES = {"text", "input_text", "output_text"}


def default_db() -> Path:
    return Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def _connect_ro(path: Path) -> Optional[sqlite3.Connection]:
    try:
        uri = "file:" + path.as_posix() + "?mode=ro&immutable=0"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except Exception:
        return None


def _tables(conn: sqlite3.Connection) -> set:
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _cols(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _ms_to_iso(value: Any) -> Optional[str]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e11:  # milliseconds
        v = v / 1000.0
    from datetime import datetime, timezone

    return iso(datetime.fromtimestamp(v, tz=timezone.utc))


def _load_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _session_messages(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    """Join message + part rows into ``{role, content, ts}`` records."""
    out: List[Dict[str, Any]] = []
    try:
        msgs = conn.execute(
            "SELECT id, time_created, data, sequence FROM message WHERE session_id=? ORDER BY sequence, time_created",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return out

    # parts grouped by message
    parts_by_msg: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for r in conn.execute(
            "SELECT message_id, sequence, data FROM part WHERE session_id=? ORDER BY sequence",
            (session_id,),
        ):
            parts_by_msg.setdefault(r["message_id"], []).append(_load_json(r["data"]))
    except sqlite3.Error:
        parts_by_msg = {}

    for m in msgs:
        meta = _load_json(m["data"])
        role = meta.get("role") or ""
        if role not in ("user", "assistant", "system"):
            continue
        # skip model-only synthetic injections (token reminders, reminders, ...)
        visibility = (meta.get("metadata") or {}).get("visibility")
        if visibility == "model-only":
            continue
        chunks: List[str] = []
        for part in parts_by_msg.get(m["id"], []):
            ptype = part.get("type")
            if ptype not in _TEXT_PART_TYPES:
                continue
            if part.get("synthetic"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
        if not chunks:
            continue
        out.append(
            {
                "role": role,
                "content": "\n".join(chunks),
                "ts": _ms_to_iso(m["time_created"]),
            }
        )
    return out


def collect(store, db_path: Optional[Path] = None, *, limit: int = 400) -> Dict[str, Any]:
    path = Path(db_path) if db_path else default_db()
    if not path.exists():
        return {"agent": AGENT, "count": 0, "skipped": "no database"}

    conn = _connect_ro(path)
    if conn is None:
        return {"agent": AGENT, "count": 0, "skipped": "cannot open read-only"}

    try:
        tables = _tables(conn)
        if "session" not in tables or "message" not in tables:
            return {"agent": AGENT, "count": 0, "skipped": "schema missing"}

        scols = _cols(conn, "session")
        order = "time_updated" if "time_updated" in scols else "rowid"
        select_cols = [
            c
            for c in ("id", "title", "directory", "time_created", "time_updated")
            if c in scols
        ]
        sessions = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM session ORDER BY {order} DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

        count = 0
        for s in sessions:
            sid = s["id"]
            messages = _session_messages(conn, sid)
            if not messages:
                continue
            cwd = (s["directory"] if "directory" in s.keys() else "") or ""
            project = store.ensure_project(cwd) if cwd and os.path.isdir(cwd) else None
            digest = common.extract_digest(
                messages, cwd or None, fallback_title=(s["title"] if "title" in s.keys() else "") or ""
            )
            updated = _ms_to_iso(s["time_updated"] if "time_updated" in s.keys() else None)
            store.upsert_session_digest(
                agent=AGENT,
                session_id=sid,
                project_id=project["project_id"] if project else None,
                cwd=cwd,
                digest_source=str(path),
                updated_at=updated,
                **digest,
            )
            count += 1
        return {"agent": AGENT, "count": count, "scanned": len(sessions)}
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        return {"agent": AGENT, "count": 0, "error": str(exc)}
    finally:
        conn.close()