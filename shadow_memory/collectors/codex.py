"""Collector: OpenAI Codex CLI session rollouts (``~/.codex/sessions/**/*.jsonl``).

Codex writes two record shapes we care about:

* ``{"type": "session_meta", "payload": {"id": ..., "cwd": ...}}``
* ``{"type": "response_item", "payload": {"type": "message", "role": ..., "content": [...]}}``
* ``{"type": "event_msg", "payload": {"type": "agent_message", "message": "..."}}``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import common

AGENT = "codex"


def default_base() -> Path:
    return Path.home() / ".codex" / "sessions"


def _parse_file(path: Path) -> Dict[str, Any]:
    cwd: Optional[str] = None
    session_id = path.stem
    messages: List[Dict[str, Any]] = []
    title_hint = ""

    for obj in common.iter_jsonl(path):
        otype = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if otype == "session_meta":
            cwd = payload.get("cwd") or obj.get("cwd") or cwd
            session_id = payload.get("id") or obj.get("id") or session_id
            continue
        if otype == "response_item":
            if payload.get("type") == "message":
                role = payload.get("role") or "user"
                messages.append(
                    {"role": role, "content": payload.get("content"), "ts": obj.get("timestamp")}
                )
            continue
        if otype == "event_msg":
            etype = payload.get("type")
            if etype == "user_message":
                messages.append(
                    {"role": "user", "content": payload.get("message") or "", "ts": obj.get("timestamp")}
                )
            elif etype == "agent_message":
                messages.append(
                    {
                        "role": "assistant",
                        "content": payload.get("message") or "",
                        "ts": obj.get("timestamp"),
                    }
                )
            continue
        # legacy / alternative flat shapes
        if otype in ("user", "assistant") and obj.get("message"):
            messages.append(
                {"role": otype, "content": obj.get("message"), "ts": obj.get("timestamp")}
            )
        if obj.get("cwd") and not cwd:
            cwd = obj["cwd"]

    return {"cwd": cwd, "session_id": session_id, "messages": messages, "title_hint": title_hint}


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
            parsed = _parse_file(path)
        except Exception:  # pragma: no cover
            continue
        if not parsed["messages"]:
            continue
        cwd = parsed["cwd"] or ""
        project = store.ensure_project(cwd) if cwd and os.path.isdir(cwd) else None
        digest = common.extract_digest(
            parsed["messages"], cwd or None, fallback_title=path.stem
        )
        store.upsert_session_digest(
            agent=AGENT,
            session_id=parsed["session_id"] or path.stem,
            project_id=project["project_id"] if project else None,
            cwd=cwd,
            digest_source=str(path),
            **digest,
        )
        count += 1
    return {"agent": AGENT, "count": count, "scanned": len(files)}