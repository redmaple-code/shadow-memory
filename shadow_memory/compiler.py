"""Shadow memory compiler.

Turns the SQLite ledger into small, always-cheap text files that an agent can
read on every turn:

    .agents/MEMORY.md           the 450-token index (hard priorities, newest handoff)
    .agents/decisions.md        full ledger with real fact ids + evidence
    .agents/LIVE_PULSE.md       30-minute live activity, real totals
    .agents/sessions/s_<8hex>.md one card per cross-agent session
    .agents/sessions/latest.md  the most recent 15 cards
    .agents/inbox.processed.jsonl  redacted archive of consumed inbox lines

Two invariants that matter more than features:

* **Never truncate a line mid-sentence.**  The budget allocator drops whole
  low-priority blocks instead of cutting text.
* **Cache safety.**  While a session is actively running (< 180 s), the
  ``AGENTS.md`` block is *not* rewritten, so a host agent's prompt cache is not
  invalidated mid-conversation.  Identical content never touches ``mtime``.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .redact import redact
from .store import Store, iso

AGENTS_DIR = ".agents"
DEFAULT_INDEX_BUDGET = 450
ACTIVE_WINDOW_SECONDS = 180
PULSE_WINDOW_SECONDS = 30 * 60
RUNNING_WINDOW_SECONDS = 3 * 60
MAX_SESSIONS_IN_INDEX = 5
MIN_SESSIONS_IN_INDEX = 3
MAX_SESSION_CARDS = 15
INBOX_ARCHIVE_LIMIT = 200

BLOCK_START = "<!-- shadow-memory-index:start -->"
BLOCK_END = "<!-- shadow-memory-index:end -->"
_BLOCK_RX = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)

_CJK_RX = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)

_KIND_RANK = {
    "constraint": 0,
    "redline": 0,
    "禁止": 0,
    "decision": 1,
    "decided": 1,
    "preference": 2,
    "handoff": 3,
    "todo": 4,
    "fact": 5,
    "note": 6,
}


# --- primitives --------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough but stable token estimate: 1 per Han char, 0.28 per other char."""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if _CJK_RX.match(ch):
            cjk += 1
    other = len(text) - cjk
    return int(math.ceil(cjk * 1.0 + other * 0.28))


def short_id(fact_id: str) -> str:
    return "#" + (fact_id or "")[:6]


def _one_line(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ⏎ ", (text or "").strip())


def _ago(seconds: float) -> str:
    if seconds is None or seconds == float("inf"):
        return "—"
    s = int(max(0, seconds))
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _state(age: float) -> str:
    return "[RUNNING]" if age <= RUNNING_WINDOW_SECONDS else "[RECENT]"


def _write_if_changed(path: Path, content: str) -> bool:
    """Write only when bytes differ — keeps mtime stable for cache friendliness."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as fh:
                if fh.read() == content:
                    return False
    except (OSError, UnicodeDecodeError):
        pass
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return True


# --- project resolution ------------------------------------------------------


def _resolve_project(store: Store, project) -> Dict[str, Any]:
    if isinstance(project, dict):
        return project
    if project is None:
        raise ValueError("project required")
    return store.ensure_project(str(project))


# --- data gathering ----------------------------------------------------------


def _rank_facts(facts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(f: Dict[str, Any]):
        rank = _KIND_RANK.get(str(f.get("kind") or "").lower(), 5)
        return (rank, -float(f.get("confidence") or 0.0), -len(f.get("content") or ""))

    return sorted(facts, key=key)


def _pulse(store: Store, project_id: Optional[str]) -> Dict[str, Any]:
    sessions = store.list_sessions(project_id, limit=200)
    window = [s for s in sessions if (s.get("age_seconds") or 0) <= PULSE_WINDOW_SECONDS]
    running = [s for s in window if (s.get("age_seconds") or 0) <= RUNNING_WINDOW_SECONDS]
    return {
        "total_recent": len(window),
        "running": len(running),
        "shown": window[:10],
        "sessions": sessions,
    }


# --- renderers ---------------------------------------------------------------

def render_index(
    project: Dict[str, Any],
    facts: Sequence[Dict[str, Any]],
    pulse: Dict[str, Any],
    *,
    budget: int = DEFAULT_INDEX_BUDGET,
) -> Dict[str, Any]:
    """Compose ``MEMORY.md`` honouring ``budget`` without cutting any line."""
    name = project.get("name") or project.get("root") or "project"
    ranked = _rank_facts(facts)

    header = [
        f"# Shadow Memory · {name}",
        "> 自动生成的跨 Agent 记忆索引，请勿手改。写入用 `smem write`，改口用 `--replace #<id>`。",
        "",
    ]

    # --- Top 3: prefer short, high-priority facts so every line stays complete
    top_pool = [f for f in ranked if len(_one_line(f.get("content") or "")) <= 200]
    top_pool += [f for f in ranked if f not in top_pool]
    top3 = top_pool[:3]
    top_lines = ["## 红线 · Top 3"]
    for f in top3:
        top_lines.append(f"- {short_id(f['fact_id'])} [{f.get('kind')}] {_one_line(f['content'])}")
    if not top3:
        top_lines.append("- （暂无记忆，先用 `smem write -k decision \"...\"` 记一条）")
    top_lines.append("")

    # --- handoff
    handoff_lines: List[str] = []
    handoffs = [f for f in ranked if str(f.get("kind") or "").lower() in ("handoff", "todo")]
    if handoffs:
        h = handoffs[0]
        handoff_lines = ["## 最新交接", f"- {short_id(h['fact_id'])} {_one_line(h['content'])}", ""]
    else:
        latest = (pulse["sessions"] or [None])[0]
        if latest:
            text = latest.get("next_steps") or latest.get("outcome") or latest.get("clean_title")
            if text:
                handoff_lines = [
                    "## 最新交接",
                    f"- {_one_line(text)[:200]}（{latest.get('agent')} · {_ago(latest.get('age_seconds'))}前）",
                    "",
                ]

    # --- recent sessions (3..5, titles complete)
    sess_lines: List[str] = []
    chosen = pulse["sessions"][:MAX_SESSIONS_IN_INDEX]
    if len(chosen) >= MIN_SESSIONS_IN_INDEX:
        sess_lines = ["## 最近会话"]
        for s in chosen:
            title = s.get("clean_title") or "(未命名会话)"
            sess_lines.append(
                f"- {_state(s.get('age_seconds'))} {title} · {s.get('agent')} · {_ago(s.get('age_seconds'))}"
            )
        sess_lines.append("")

    # --- pulse summary (lowest priority)
    pulse_lines = [
        "## 现场脉冲",
        f"- 30 分钟内活跃会话 {pulse['total_recent']} 条（RUNNING {pulse['running']}）· 详见 LIVE_PULSE.md",
        "",
    ]

    blocks = [
        ("header", header, True),
        ("top3", top_lines, True),
        ("handoff", handoff_lines, False),
        ("sessions", sess_lines, True),
        ("pulse", pulse_lines, False),
    ]

    out: List[str] = []
    used = 0
    soft_ceiling = int(budget * 1.35)
    for _name, lines, mandatory in blocks:
        if not lines:
            continue
        cost = sum(estimate_tokens(l) + 1 for l in lines)
        if mandatory or used + cost <= budget:
            out.extend(lines)
            used += cost
        elif used + cost <= soft_ceiling:
            # squeeze in only if still within the soft ceiling
            out.extend(lines)
            used += cost

    text = "\n".join(out).rstrip() + "\n"
    return {"text": text, "est_tokens": estimate_tokens(text)}


def render_decisions(project: Dict[str, Any], facts: Sequence[Dict[str, Any]], when: str) -> str:
    name = project.get("name") or project.get("root") or "project"
    lines = [
        f"# 决策账本 · {name}",
        f"> {len(facts)} 条 active 记忆 · 编译于 {when} · 完整 fact_id 可用于 `--replace`",
        "",
    ]
    if not facts:
        lines.append("（暂无 active 记忆）")
    for f in _rank_facts(facts):
        lines.append(f"## {f.get('kind')} · {short_id(f['fact_id'])}")
        lines.append("")
        lines.append(f"- **内容**：{_one_line(f.get('content'))}")
        if f.get("evidence"):
            lines.append(f"- **依据**：{_one_line(f['evidence'])}")
        src = " / ".join(x for x in (f.get("source_agent"), f.get("source_session")) if x) or "—"
        lines.append(f"- **来源**：{src}")
        lines.append(
            f"- **置信度**：{float(f.get('confidence') or 0):.2f} · 创建 {f.get('created_at')} · 更新 {f.get('updated_at')}"
        )
        lines.append(f"- **fact_id**：`{f['fact_id']}`")
        if f.get("supersedes_id"):
            lines.append(f"- **取代**：`{f['supersedes_id']}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_live_pulse(project: Dict[str, Any], pulse: Dict[str, Any], when: str) -> str:
    name = project.get("name") or project.get("root") or "project"
    lines = [
        f"# LIVE_PULSE · {name}",
        f"> 30 分钟内活跃会话 **{pulse['total_recent']}** 条（RUNNING {pulse['running']}）· 展示前 {min(10, len(pulse['shown']))} 条 · {when}",
        "",
    ]
    if not pulse["shown"]:
        lines.append("（30 分钟内无活跃会话）")
    for s in pulse["shown"]:
        files = s.get("touched_files") or []
        files_bit = f" · 涉及 {len(files)} 文件" if files else ""
        lines.append(
            f"- {_state(s.get('age_seconds'))} {s.get('agent')} · {s.get('clean_title') or '(未命名)'}"
            f"{files_bit} · {_ago(s.get('age_seconds'))}前"
        )
    if pulse["total_recent"] > len(pulse["shown"]):
        lines.append("")
        lines.append(f"… 另有 {pulse['total_recent'] - len(pulse['shown'])} 条未展示")
    return "\n".join(lines).rstrip() + "\n"


def render_session_card(project: Dict[str, Any], s: Dict[str, Any]) -> str:
    files = s.get("touched_files") or []
    lines = [
        f"# {s.get('clean_title') or '(未命名会话)'}",
        "",
        f"- **Agent**：{s.get('agent')}",
        f"- **Session**：`{s.get('session_id')}`",
        f"- **项目**：{project.get('name')} · `{project.get('root')}`",
        f"- **更新时间**：{s.get('updated_at')}（{_ago(s.get('age_seconds'))}前）",
        f"- **消息数**：{s.get('message_count')}",
        "",
        "## 摘要",
        s.get("summary") or "（无）",
        "",
        "## 末轮结论",
        s.get("outcome") or "（无）",
        "",
        "## 未完事项",
        s.get("next_steps") or "（无）",
        "",
        "## 涉及文件",
    ]
    if files:
        lines.extend(f"- `{p}`" for p in files)
    else:
        lines.append("（无）")
    return "\n".join(lines).rstrip() + "\n"


def render_latest(project: Dict[str, Any], sessions: Sequence[Dict[str, Any]]) -> str:
    name = project.get("name") or project.get("root") or "project"
    lines = [f"# 最近会话 · {name}", "", f"共 {len(sessions)} 条（最多保留 {MAX_SESSION_CARDS}）", ""]
    for s in sessions:
        card = _card_name(s)
        lines.append(
            f"## {s.get('clean_title') or '(未命名)'}\n"
            f"- {s.get('agent')} · {_ago(s.get('age_seconds'))}前 · [{card}]({card})"
        )
        if s.get("outcome"):
            lines.append(f"- 结论：{_one_line(s['outcome'])[:200]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _card_name(s: Dict[str, Any]) -> str:
    import hashlib

    key = s.get("session_key") or f"{s.get('agent')}:{s.get('session_id')}"
    return "s_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8] + ".md"


# --- AGENTS.md / git hygiene -------------------------------------------------


def agents_block(project: Dict[str, Any]) -> str:
    name = project.get("name") or "project"
    return "\n".join(
        [
            BLOCK_START,
            f"## Shadow Memory · {name} 的跨 Agent 影子记忆",
            f"- 索引：`{AGENTS_DIR}/MEMORY.md`（≤{DEFAULT_INDEX_BUDGET} tokens）· 决策：`{AGENTS_DIR}/decisions.md` · 现场：`{AGENTS_DIR}/LIVE_PULSE.md`",
            '- 写记忆：`smem write -k decision "结论"`；改口：`smem write -k decision --replace #<短ID> "新结论"`',
            BLOCK_END,
        ]
    )


def sync_agents_md(
    root: Path,
    block: str,
    *,
    defer: bool = False,
    filename: str = "AGENTS.md",
) -> Dict[str, Any]:
    """Idempotently upsert the controlled block into ``<root>/<filename>``."""
    path = Path(root) / filename
    if defer:
        return {"path": str(path), "updated": False, "deferred": True}

    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""

    if _BLOCK_RX.search(existing):
        new_text = _BLOCK_RX.sub(block, existing, count=1)
    elif existing.strip():
        new_text = existing.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new_text = block + "\n"

    changed = _write_if_changed(path, new_text)
    return {"path": str(path), "updated": changed, "deferred": False}


def ensure_git_exclude(root: Path) -> Dict[str, Any]:
    """Keep ``.agents/`` out of version control without touching global config."""
    root = Path(root)
    git = root / ".git"
    entry = f"{AGENTS_DIR}/"
    if git.exists():
        info = git / "info"
        # worktrees store `.git` as a file pointing at the real dir
        if git.is_file():
            try:
                content = git.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"gitdir:\s*(.+)", content)
                if m:
                    target = Path(m.group(1).strip())
                    if not target.is_absolute():
                        target = (root / target).resolve()
                    info = target / "info"
            except OSError:
                pass
        try:
            info.mkdir(parents=True, exist_ok=True)
            exclude = info / "exclude"
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if entry not in current.splitlines():
                with exclude.open("a", encoding="utf-8", newline="\n") as fh:
                    if current and not current.endswith("\n"):
                        fh.write("\n")
                    fh.write(entry + "\n")
                return {"mode": "git-exclude", "path": str(exclude), "updated": True}
            return {"mode": "git-exclude", "path": str(exclude), "updated": False}
        except OSError:
            pass
    gi = root / AGENTS_DIR / ".gitignore"
    written = _write_if_changed(gi, "*\n")
    return {"mode": "local-gitignore", "path": str(gi), "updated": written}


# --- inbox -------------------------------------------------------------------


def inbox_paths(root: Path) -> Dict[str, Path]:
    d = Path(root) / AGENTS_DIR
    return {"dir": d, "inbox": d / "inbox.jsonl", "processed": d / "inbox.processed.jsonl"}


def consume_inbox(
    store: Store,
    project: Dict[str, Any],
    *,
    archive_limit: int = INBOX_ARCHIVE_LIMIT,
) -> Dict[str, Any]:
    """Consume ``.agents/inbox.jsonl`` -> facts, archive redacted, truncate."""
    paths = inbox_paths(Path(project["root"]))
    inbox = paths["inbox"]
    if not inbox.exists():
        return {"consumed": 0, "results": [], "path": str(inbox)}

    try:
        raw_lines = inbox.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"consumed": 0, "results": [], "path": str(inbox)}

    results: List[Dict[str, Any]] = []
    archive: List[Dict[str, Any]] = []
    now = iso()
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        text = obj.get("text") or obj.get("content") or obj.get("memory")
        if not isinstance(text, str) or not text.strip():
            continue
        kind = str(obj.get("kind") or "fact")
        scope = str(obj.get("scope") or "project")
        replace = obj.get("replace") or obj.get("supersedes_id")
        evidence = obj.get("evidence") or ""
        confidence = obj.get("confidence", 0.8)
        try:
            res = store.write_fact(
                content=text,
                project_id=project["project_id"] if scope == "project" else None,
                scope=scope,
                kind=kind,
                evidence=evidence if isinstance(evidence, str) else "",
                confidence=float(confidence),
                source_agent=str(obj.get("source_agent") or "inbox"),
                source_session=str(obj.get("source_session") or ""),
                ttl_days=obj.get("ttl_days"),
                supersedes_id=str(replace) if replace else None,
            )
        except (ValueError, KeyError) as exc:
            results.append({"status": "error", "error": str(exc), "text": redact(text)[:200]})
            continue
        results.append(res)
        archive.append(
            {
                "ts": now,
                "status": res.get("status"),
                "fact_id": res.get("fact_id"),
                "short_id": res.get("short_id"),
                "kind": kind,
                "text": redact(text)[:500],
            }
        )

    # append archive, keep last N lines
    if archive:
        proc = paths["processed"]
        existing: List[str] = []
        if proc.exists():
            try:
                existing = proc.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                existing = []
        existing.extend(json.dumps(a, ensure_ascii=False) for a in archive)
        existing = [l for l in existing if l.strip()][-archive_limit:]
        _write_if_changed(proc, "\n".join(existing) + "\n")

    # clear the inbox (truncate rather than delete so editors keep the handle)
    try:
        with inbox.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("")
    except OSError:
        pass

    return {
        "consumed": len(archive),
        "results": results,
        "path": str(inbox),
    }


# --- main entry --------------------------------------------------------------


def compile_project(
    store: Store,
    project,
    *,
    budget: int = DEFAULT_INDEX_BUDGET,
    sync_agents: bool = True,
    force_agents_sync: bool = False,
    write: bool = True,
) -> Dict[str, Any]:
    """Rebuild every shadow file for one project."""
    proj = _resolve_project(store, project)
    root = Path(proj["root"])
    pid = proj.get("project_id")
    when = iso()

    facts = store.search_facts(project_id=pid, status="active", limit=500)
    pulse = _pulse(store, pid)

    index = render_index(proj, facts, pulse, budget=budget)
    decisions = render_decisions(proj, facts, when)
    live = render_live_pulse(proj, pulse, when)

    out_dir = root / AGENTS_DIR
    sessions_dir = out_dir / "sessions"

    written: List[str] = []
    if write:
        if _write_if_changed(out_dir / "MEMORY.md", index["text"]):
            written.append(str(out_dir / "MEMORY.md"))
        if _write_if_changed(out_dir / "decisions.md", decisions):
            written.append(str(out_dir / "decisions.md"))
        if _write_if_changed(out_dir / "LIVE_PULSE.md", live):
            written.append(str(out_dir / "LIVE_PULSE.md"))

        cards = pulse["sessions"][:MAX_SESSION_CARDS]
        for s in cards:
            card_path = sessions_dir / _card_name(s)
            if _write_if_changed(card_path, render_session_card(proj, s)):
                written.append(str(card_path))
        if _write_if_changed(sessions_dir / "latest.md", render_latest(proj, cards)):
            written.append(str(sessions_dir / "latest.md"))

    agents_result = {"updated": False, "deferred": False, "path": str(root / "AGENTS.md")}
    if sync_agents:
        active_age = store.latest_session_update_seconds(pid)
        defer = (not force_agents_sync) and active_age <= ACTIVE_WINDOW_SECONDS
        agents_result = sync_agents_md(root, agents_block(proj), defer=defer)

    if write:
        ensure_git_exclude(root)

    return {
        "project_id": pid,
        "project": proj.get("name"),
        "root": str(root),
        "facts": len(facts),
        "sessions": len(pulse["sessions"]),
        "est_tokens": index["est_tokens"],
        "budget": budget,
        "agents_md_deferred": bool(agents_result.get("deferred")),
        "agents_md_updated": bool(agents_result.get("updated")),
        "written": written,
        "files": {
            "memory": str(out_dir / "MEMORY.md"),
            "decisions": str(out_dir / "decisions.md"),
            "live_pulse": str(out_dir / "LIVE_PULSE.md"),
            "sessions": str(sessions_dir),
        },
    }


def read_shadow_file(root: Path, filename: str) -> Dict[str, Any]:
    """Read one compiled shadow file plus its live token estimate."""
    allowed = {
        "MEMORY.md": Path(root) / AGENTS_DIR / "MEMORY.md",
        "decisions.md": Path(root) / AGENTS_DIR / "decisions.md",
        "LIVE_PULSE.md": Path(root) / AGENTS_DIR / "LIVE_PULSE.md",
        "inbox.processed.jsonl": Path(root) / AGENTS_DIR / "inbox.processed.jsonl",
        "sessions/latest.md": Path(root) / AGENTS_DIR / "sessions" / "latest.md",
    }
    path = allowed.get(filename)
    if path is None:
        raise KeyError(f"unknown shadow file: {filename}")
    if not path.exists():
        return {"file": filename, "path": str(path), "exists": False, "content": "", "est_tokens": 0}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""
    return {
        "file": filename,
        "path": str(path),
        "exists": True,
        "content": content,
        "est_tokens": estimate_tokens(content),
        "bytes": len(content.encode("utf-8")),
        "mtime": path.stat().st_mtime if path.exists() else None,
    }