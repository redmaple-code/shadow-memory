"""Background sync loop + shared sync orchestration.

``run_sync`` is the single implementation used by ``smem sync``, ``POST
/api/sync`` and the daemon's slow tick, so behaviour can never diverge.

Cadence:

* **fast tick (5 s)**  — consume ``.agents/inbox.jsonl`` for every project that
  currently has a non-empty inbox (facts land within seconds of a write);
* **slow tick (60 s)** — incremental session collection + full recompile.
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collectors import collect_all
from .compiler import compile_project, consume_inbox, inbox_paths
from .store import Store

INBOX_INTERVAL = 5.0
SYNC_INTERVAL = 60.0


def _inbox_has_content(root: Path) -> bool:
    p = inbox_paths(root)["inbox"]
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def active_projects(store: Store) -> List[Dict[str, Any]]:
    """Projects with pending inbox work, newest activity first."""
    out = []
    for proj in store.list_projects():
        root = Path(proj["root"])
        if root.exists() and _inbox_has_content(root):
            out.append(proj)
    return out


def run_sync(
    store: Store,
    *,
    cwd: Optional[str] = None,
    all_projects: bool = False,
    collect: bool = True,
    home: Optional[Path] = None,
    sync_agents: bool = True,
    force_agents_sync: bool = False,
    budget: Optional[int] = None,
) -> Dict[str, Any]:
    """Consume inboxes, harvest sessions, recompile shadow files."""
    report: Dict[str, Any] = {"collected": None, "projects": [], "errors": []}

    if collect:
        try:
            report["collected"] = collect_all(store, home=home)
        except Exception as exc:  # pragma: no cover - defensive
            report["errors"].append(f"collect: {exc}")

    if all_projects:
        targets = store.list_projects()
    elif cwd:
        targets = [store.ensure_project(cwd)]
    else:
        targets = store.list_projects()

    for proj in targets:
        root = Path(proj["root"])
        if not root.exists():
            report["errors"].append(f"missing root: {root}")
            continue
        try:
            proj = store.get_project(proj["project_id"]) or proj
            inbox = consume_inbox(store, proj)
            kwargs: Dict[str, Any] = {
                "sync_agents": sync_agents,
                "force_agents_sync": force_agents_sync,
            }
            if budget is not None:
                kwargs["budget"] = budget
            compiled = compile_project(store, proj, **kwargs)
            report["projects"].append(
                {
                    "project_id": proj["project_id"],
                    "name": proj.get("name"),
                    "root": proj.get("root"),
                    "inbox_consumed": inbox["consumed"],
                    "facts": compiled["facts"],
                    "sessions": compiled["sessions"],
                    "est_tokens": compiled["est_tokens"],
                    "agents_md_deferred": compiled["agents_md_deferred"],
                }
            )
        except Exception as exc:  # pragma: no cover - defensive
            report["errors"].append(f"{proj.get('root')}: {type(exc).__name__}: {exc}")

    return report


class Daemon:
    """Two-speed background worker.  ``start()`` is non-blocking."""

    def __init__(
        self,
        store: Store,
        *,
        inbox_interval: float = INBOX_INTERVAL,
        sync_interval: float = SYNC_INTERVAL,
        home: Optional[Path] = None,
        quiet: bool = False,
    ) -> None:
        self.store = store
        self.inbox_interval = float(inbox_interval)
        self.sync_interval = float(sync_interval)
        self.home = home
        self.quiet = quiet
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_inbox_at: Optional[float] = None
        self.last_sync_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.inbox_runs = 0
        self.sync_runs = 0

    # -- public -------------------------------------------------------------

    def start(self) -> "Daemon":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="smem-daemon", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "running": self.running,
            "inbox_runs": self.inbox_runs,
            "sync_runs": self.sync_runs,
            "last_inbox_ago": None if self.last_inbox_at is None else round(now - self.last_inbox_at, 1),
            "last_sync_ago": None if self.last_sync_at is None else round(now - self.last_sync_at, 1),
            "last_error": self.last_error,
            "inbox_interval": self.inbox_interval,
            "sync_interval": self.sync_interval,
        }

    # -- ticks --------------------------------------------------------------

    def run_inbox_tick(self) -> Dict[str, Any]:
        """Consume + recompile only the projects that have pending inbox lines."""
        touched = 0
        for proj in active_projects(self.store):
            try:
                consume_inbox(self.store, proj)
                compile_project(self.store, proj, sync_agents=True)
                touched += 1
            except Exception as exc:  # pragma: no cover
                self.last_error = f"{proj.get('root')}: {exc}"
        self.inbox_runs += 1
        self.last_inbox_at = time.time()
        return {"projects": touched}

    def run_sync_tick(self) -> Dict[str, Any]:
        try:
            report = run_sync(self.store, home=self.home)
        except Exception as exc:  # pragma: no cover
            self.last_error = f"{type(exc).__name__}: {exc}"
            report = {"errors": [self.last_error]}
        self.sync_runs += 1
        self.last_sync_at = time.time()
        return report

    def _loop(self) -> None:
        next_sync = 0.0
        while not self._stop.is_set():
            try:
                self.run_inbox_tick()
            except Exception:  # pragma: no cover
                self.last_error = traceback.format_exc(limit=2)
            if time.time() >= next_sync:
                self.run_sync_tick()
                next_sync = time.time() + self.sync_interval
            self._stop.wait(self.inbox_interval)