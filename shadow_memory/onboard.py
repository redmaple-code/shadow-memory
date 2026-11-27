"""One-shot onboarding: detect agent configs, inject a controlled block, undo it.

Safety rules baked in:

* Every write is preceded by a ``<file>.bak.<timestamp>`` backup.
* Injection is idempotent — re-running never stacks a second block.
* Files we did not create are never deleted; only a file that ends up empty
  (i.e. it contained nothing but our block) is removed on uninstall.
* A missing agent directory is *not* created unless ``create_missing=True``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MARKER_START = "<!-- shadow-memory:start -->"
MARKER_END = "<!-- shadow-memory:end -->"
_BLOCK_RX = re.compile(re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END), re.S)

# agent -> config file (relative to home)
TARGETS: Dict[str, str] = {
    "claude-code": ".claude/CLAUDE.md",
    "codex": ".codex/AGENTS.md",
    "zcode": ".zcode/AGENTS.md",
    "antigravity": ".gemini/GEMINI.md",
}

AGENT_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
    "zcode": "ZCode",
    "antigravity": "Antigravity / Gemini",
}

BLOCK_BODY = """{start}
## Shadow Memory（跨 Agent 影子记忆）
- 开工先读 `.agents/MEMORY.md`（≤450 tokens）与 `.agents/LIVE_PULSE.md`，不要读整个历史。
- 记结论 `smem write -k decision "结论"`；改口取代旧结论 `smem write -k decision --replace #<短ID> "新结论"`。
- 废弃错误记忆 `smem forget <短ID>`。禁止写入密钥/手机号/邮箱（服务端仍会二次脱敏）。
{end}"""


def block_text() -> str:
    return BLOCK_BODY.format(start=MARKER_START, end=MARKER_END)


def _path_for(agent: str, home: Optional[Path]) -> Path:
    base = Path(home) if home else Path.home()
    return base / TARGETS[agent]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _backup(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + f".bak.{stamp}")
    n = 1
    while dest.exists():
        dest = path.with_name(path.name + f".bak.{stamp}-{n}")
        n += 1
    try:
        dest.write_bytes(path.read_bytes())
    except OSError:
        return None
    return str(dest)


def _count_sessions(store, agent: str) -> int:
    if store is None:
        return 0
    try:
        row = store._conn.execute(
            "SELECT COUNT(*) FROM session_digest WHERE agent=?", (agent,)
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # pragma: no cover
        return 0


def inspect_agents(store=None, home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Report detection state for every supported agent."""
    out: List[Dict[str, Any]] = []
    for agent, rel in TARGETS.items():
        path = _path_for(agent, home)
        agent_dir = path.parent
        text = _read(path) if path.exists() else ""
        out.append(
            {
                "agent": agent,
                "label": AGENT_LABELS.get(agent, agent),
                "path": str(path),
                "rel_path": rel,
                "installed": agent_dir.exists(),
                "file_exists": path.exists(),
                "configured": bool(_BLOCK_RX.search(text)),
                "sessions_count": _count_sessions(store, agent),
            }
        )
    return out


def apply_init(
    dry_run: bool = True,
    *,
    create_missing: bool = False,
    home: Optional[Path] = None,
    agents: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Inject the controlled block into every detected agent config."""
    selected = agents or list(TARGETS)
    results: List[Dict[str, Any]] = []

    for agent in selected:
        if agent not in TARGETS:
            results.append({"agent": agent, "action": "unknown-agent", "changed": False})
            continue
        path = _path_for(agent, home)
        entry: Dict[str, Any] = {
            "agent": agent,
            "path": str(path),
            "changed": False,
            "backup": None,
            "action": "unchanged",
        }

        if not path.parent.exists() and not create_missing:
            entry["action"] = "skipped (agent not installed)"
            results.append(entry)
            continue

        existing = _read(path) if path.exists() else ""
        block = block_text()

        if _BLOCK_RX.search(existing):
            new_text = _BLOCK_RX.sub(block, existing, count=1)
        elif existing.strip():
            new_text = existing.rstrip("\n") + "\n\n" + block + "\n"
        else:
            new_text = block + "\n"

        if existing == new_text:
            results.append(entry)
            continue

        entry["action"] = "update" if existing.strip() else "create"
        if not dry_run:
            if path.exists():
                entry["backup"] = _backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8", newline="\n")
            entry["changed"] = True
        else:
            entry["preview"] = new_text
        results.append(entry)

    return {
        "dry_run": dry_run,
        "changed": sum(1 for r in results if r.get("changed") or (dry_run and r.get("preview"))),
        "results": results,
    }


def apply_uninstall(
    dry_run: bool = True,
    *,
    home: Optional[Path] = None,
    agents: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Remove the controlled block, leaving no trace."""
    selected = agents or list(TARGETS)
    results: List[Dict[str, Any]] = []

    for agent in selected:
        if agent not in TARGETS:
            results.append({"agent": agent, "action": "unknown-agent", "changed": False})
            continue
        path = _path_for(agent, home)
        entry: Dict[str, Any] = {
            "agent": agent,
            "path": str(path),
            "changed": False,
            "backup": None,
            "action": "unchanged",
        }
        if not path.exists():
            entry["action"] = "skipped (no config file)"
            results.append(entry)
            continue
        existing = _read(path)
        if not _BLOCK_RX.search(existing):
            entry["action"] = "skipped (not configured)"
            results.append(entry)
            continue

        new_text = _BLOCK_RX.sub("", existing, count=1)
        new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip("\n")
        entry["action"] = "delete-file" if not new_text.strip() else "remove-block"
        if not dry_run:
            entry["backup"] = _backup(path)
            if not new_text.strip():
                path.unlink()
            else:
                path.write_text(new_text + "\n", encoding="utf-8", newline="\n")
            entry["changed"] = True
        else:
            entry["preview"] = new_text
        results.append(entry)

    return {
        "dry_run": dry_run,
        "changed": sum(1 for r in results if r.get("changed") or (dry_run and "preview" in r)),
        "results": results,
    }