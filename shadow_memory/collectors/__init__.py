"""Collector dispatcher.

``collect_all(conn)`` runs every available collector and returns a per-agent
report.  Individual collector failures are captured, never propagated, so one
broken transcript directory cannot stop a sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from . import claude_code, codex, common, zcode

__all__ = ["collect_all", "common", "claude_code", "codex", "zcode"]

COLLECTORS = (
    ("claude-code", claude_code.collect),
    ("codex", codex.collect),
    ("zcode", zcode.collect),
)


def collect_all(store, *, home: Optional[Path] = None, limit: int = 400) -> Dict[str, Any]:
    """Run all collectors.

    ``home``, when given, is used as the *parent* of each agent's dot-directory
    (handy for tests: pass a tmp dir containing ``.claude`` / ``.codex`` /
    ``.zcode``).  With ``home=None`` each collector uses the real user paths.
    """
    report: Dict[str, Any] = {"agents": {}, "total": 0}
    for name, fn in COLLECTORS:
        try:
            if home is not None:
                base = _override_base(name, Path(home))
                result = fn(store, base, limit=limit)
            else:
                result = fn(store, limit=limit)
        except Exception as exc:  # pragma: no cover - defensive
            result = {"agent": name, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
        report["agents"][name] = result
        report["total"] += int(result.get("count") or 0)
    return report


def _override_base(name: str, home: Path) -> Path:
    if name == "claude-code":
        return home / ".claude" / "projects"
    if name == "codex":
        return home / ".codex" / "sessions"
    if name == "zcode":
        return home / ".zcode" / "cli" / "db" / "db.sqlite"
    return home