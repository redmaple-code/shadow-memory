"""Collector: Claude Code session transcripts (``~/.claude/projects/**/*.jsonl``)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import common

AGENT = "claude-code"


def default_base() -> Path:
    return Path.home() / ".claude" / "projects"


def _iter_messages(path: Path) -> Dict[str, Any]:
    """Parse one transcript file into ``{meta, messages}``."""
    cwd: Optional[str] = None
    session_id = path.stem
    messages: List[Dict[str, Any]] = []
    summary_line = ""

    for obj in common.iter_jsonl(path):
        otype = obj.get("type")
        if obj.get("cwd") and not cwd:
            cwd = obj["cwd"]
        if obj.get("sessionId"):
            session_id = obj["sessionId"]
        if otype == "summary" and obj.get("summary"):
            summary_line = obj["summary"]
            continue
        if otype in ("user", "assistant"):
            msg = obj.get("message")
            if isinstance(msg, dict):
                role = msg.get("role") or otype
                messages.append(
                    {
                        "role": role,
                        "content": msg.get("content"),
                        "ts": obj.get("timestamp"),
                    }
                )
            elif isinstance(obj.get("content"), (str, list)):
                messages.append(
                    {"role": otype, "content": obj.get("content"), "ts": obj.get("timestamp")}
                )

    return {"cwd": cwd, "session_id": session_id, "messages": messages, "summary": summary_line}


def collect(store, home: Optional[Path] = None, *, limit: int = 400) -> Dict[str, Any]:
    base = Path(home) if home else default_base()
    if not base.exists():
        return {"agent": AGENT, "count": 0, "skipped": "no base dir"}

    files: List[Path] = []
    for root, _dirs, names in os.walk(base):
        for n in names:
            if n.endswith(".jsonl"):
                files.append(Path(root) / n)
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    count = 0
    for path in files[:limit]:
        try:
            parsed = _iter_messages(path)
        except Exception:  # pragma: no cover - never crash a collection run
            continue
        if not parsed["messages"]:
            continue
        cwd = parsed["cwd"] or ""
        project = store.ensure_project(cwd) if cwd and os.path.isdir(cwd) else None
        project_id = project["project_id"] if project else None
        digest = common.extract_digest(
            parsed["messages"], cwd or None, fallback_title=parsed["summary"] or path.stem
        )
        store.upsert_session_digest(
            agent=AGENT,
            session_id=parsed["session_id"] or path.stem,
            project_id=project_id,
            cwd=cwd,
            digest_source=str(path),
            **digest,
        )
        count += 1
    return {"agent": AGENT, "count": count, "scanned": len(files)}