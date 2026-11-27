"""Local-only HTTP API + single-page viewer (stdlib ``ThreadingHTTPServer``).

Binding defaults to ``127.0.0.1:17820``.  There is no authentication because
there is no remote surface: the socket never leaves loopback.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from .compiler import compile_project, consume_inbox, read_shadow_file
from .daemon import Daemon, run_sync
from .onboard import inspect_agents
from .store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17820


class AppState:
    """Everything a request handler needs, assembled once."""

    def __init__(
        self,
        store: Store,
        *,
        home: Optional[Path] = None,
        daemon: Optional[Daemon] = None,
    ) -> None:
        self.store = store
        self.home = home
        self.daemon = daemon
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self.started_at = __import__("time").time()
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def resolve_project(self, project_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if project_id:
            return self.store.get_project(project_id)
        projects = self.store.list_projects()
        return projects[0] if len(projects) == 1 else None

    def project_for_root(self, root: str) -> Dict[str, Any]:
        return self.store.ensure_project(root)


class Handler(BaseHTTPRequestHandler):
    server_version = f"shadow-memory/{__version__}"
    state: AppState  # injected by create_server

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    def _send(self, code: int, body: bytes, ctype: str, *, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=None).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": message}, code)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/":
                from .web_ui import HTML

                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8", cache=True)
            elif path == "/api/status":
                self._json(self._api_status())
            elif path == "/api/projects":
                self._json({"ok": True, "projects": self.state.store.list_projects()})
            elif path == "/api/facts":
                self._json(self._api_facts(qs))
            elif path == "/api/sessions":
                self._json(self._api_sessions(qs))
            elif path == "/api/shadow":
                self._json(self._api_shadow(qs))
            elif path == "/api/agents":
                self._json({"ok": True, "agents": inspect_agents(self.state.store, self.state.home)})
            elif path.startswith("/api/facts/") and path.endswith("/chain"):
                fid = path[len("/api/facts/") : -len("/chain")].strip("/")
                self._json(self._api_chain(fid))
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._error(f"not found: {path}", 404)
        except KeyError as exc:
            self._error(str(exc), 404)
        except Exception as exc:  # pragma: no cover - defensive
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._body()
        try:
            if path == "/api/facts":
                self._json(self._api_write_fact(body))
            elif path == "/api/sync":
                self._json(self._api_sync(body))
            elif path == "/api/compile":
                self._json(self._api_compile(body))
            elif path == "/api/inbox/consume":
                self._json(self._api_consume(body))
            elif path.startswith("/api/facts/") and path.endswith("/forget"):
                fid = path[len("/api/facts/") : -len("/forget")].strip("/")
                self._json(self._api_forget(fid, body))
            elif path.startswith("/api/facts/") and path.endswith("/restore"):
                fid = path[len("/api/facts/") : -len("/restore")].strip("/")
                self._json(self._api_restore(fid))
            else:
                self._error(f"not found: {path}", 404)
        except KeyError as exc:
            self._error(str(exc), 404)
        except ValueError as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._error(f"{type(exc).__name__}: {exc}", 500)

    # -- endpoints ----------------------------------------------------------

    def _api_status(self) -> Dict[str, Any]:
        st = self.state
        counts = st.store.counts()
        agents = inspect_agents(st.store, st.home)
        import time as _t

        return {
            "ok": True,
            "version": __version__,
            "service": "shadow-memory",
            "host": st.host,
            "port": st.port,
            "uptime_seconds": round(_t.time() - st.started_at, 1),
            "data": {"db": str(st.store.path), "home": str(Path(st.store.path).parent)},
            "counts": counts,
            "daemon": st.daemon.status() if st.daemon else {"running": False},
            "agents": agents,
            "budget": 450,
        }

    def _api_facts(self, qs: Dict[str, str]) -> Dict[str, Any]:
        store = self.state.store
        status = qs.get("status") or None
        facts = store.search_facts(
            q=qs.get("q") or None,
            project_id=qs.get("project_id") or None,
            kind=qs.get("kind") or None,
            status=status,
            limit=int(qs.get("limit") or 200),
            include_expired=qs.get("include_expired") == "1",
        )
        return {"ok": True, "count": len(facts), "facts": facts}

    def _api_chain(self, fact_id: str) -> Dict[str, Any]:
        store = self.state.store
        chain = store.get_fact_chain(fact_id)
        if not chain:
            raise KeyError(f"no fact matches {fact_id!r}")
        current = store.get_fact(chain[-1]["fact_id"])
        return {"ok": True, "fact": current, "chain": chain, "length": len(chain)}

    def _api_write_fact(self, body: Dict[str, Any]) -> Dict[str, Any]:
        store = self.state.store
        content = body.get("content") or body.get("text")
        if not content or not str(content).strip():
            raise ValueError("content required")
        project_id = body.get("project_id") or None
        if not project_id and body.get("root"):
            project_id = store.ensure_project(body["root"])["project_id"]
        if not project_id:
            proj = self.state.resolve_project(None)
            project_id = proj["project_id"] if proj else None
        result = store.write_fact(
            content=str(content),
            project_id=project_id,
            scope=str(body.get("scope") or "project"),
            kind=str(body.get("kind") or "fact"),
            evidence=str(body.get("evidence") or ""),
            confidence=float(body.get("confidence") or 0.8),
            source_agent=str(body.get("source_agent") or "web"),
            supersedes_id=body.get("supersedes_id") or body.get("replace") or None,
            ttl_days=body.get("ttl_days"),
        )
        self._recompile(project_id)
        return {"ok": True, **result}

    def _api_forget(self, fact_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        store = self.state.store
        fact = store.get_fact(fact_id)
        res = store.forget_fact(fact_id, str(body.get("reason") or "web"))
        self._recompile(fact.get("project_id") if fact else None)
        return {"ok": True, **res}

    def _api_restore(self, fact_id: str) -> Dict[str, Any]:
        store = self.state.store
        fact = store.get_fact(fact_id)
        res = store.restore_fact(fact_id)
        self._recompile(fact.get("project_id") if fact else None)
        return {"ok": True, **res}

    def _api_sessions(self, qs: Dict[str, str]) -> Dict[str, Any]:
        store = self.state.store
        pid = qs.get("project_id") or None
        sessions = store.list_sessions(pid, limit=int(qs.get("limit") or 60))
        pulse = []
        for s in sessions:
            if (s.get("age_seconds") or 0) <= 30 * 60:
                pulse.append(
                    {
                        "session_key": s["session_key"],
                        "agent": s["agent"],
                        "title": s.get("clean_title"),
                        "state": "RUNNING" if (s.get("age_seconds") or 0) <= 180 else "RECENT",
                        "age_seconds": round(s.get("age_seconds") or 0, 1),
                    }
                )
        return {"ok": True, "count": len(sessions), "pulse": pulse, "sessions": sessions}

    def _api_shadow(self, qs: Dict[str, str]) -> Dict[str, Any]:
        proj = self.state.resolve_project(qs.get("project_id"))
        if not proj:
            raise ValueError("project_id required (multiple projects)")
        filename = qs.get("file") or "MEMORY.md"
        data = read_shadow_file(Path(proj["root"]), filename)
        return {"ok": True, "project_id": proj["project_id"], **data}

    def _api_sync(self, body: Dict[str, Any]) -> Dict[str, Any]:
        store = self.state.store
        pid = body.get("project_id")
        if pid:
            proj = store.get_project(pid)
            if not proj:
                raise KeyError(f"unknown project {pid}")
            rep = run_sync(store, cwd=proj["root"], home=self.state.home)
        else:
            rep = run_sync(store, all_projects=True, home=self.state.home)
        return {"ok": True, **rep}

    def _api_compile(self, body: Dict[str, Any]) -> Dict[str, Any]:
        pid = body.get("project_id")
        proj = self.state.resolve_project(pid)
        if not proj:
            raise ValueError("project_id required")
        res = compile_project(self.state.store, proj, force_agents_sync=True)
        return {"ok": True, **res}

    def _api_consume(self, body: Dict[str, Any]) -> Dict[str, Any]:
        proj = self.state.resolve_project(body.get("project_id"))
        if not proj:
            raise ValueError("project_id required")
        res = consume_inbox(self.state.store, proj)
        compiled = compile_project(self.state.store, proj, force_agents_sync=True)
        return {"ok": True, "inbox": res, "compiled": compiled}

    def _recompile(self, project_id: Optional[str]) -> None:
        if not project_id:
            return
        proj = self.state.store.get_project(project_id)
        if not proj:
            return
        try:
            compile_project(self.state.store, proj, force_agents_sync=True)
        except Exception:  # pragma: no cover - never fail a write over a render
            pass


def create_server(
    store: Store,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    home: Optional[Path] = None,
    daemon: Optional[Daemon] = None,
) -> ThreadingHTTPServer:
    state = AppState(store, home=home, daemon=daemon)
    state.host = host
    state.port = port
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True
    httpd.app_state = state  # type: ignore[attr-defined]
    return httpd


def serve_forever(
    store: Store,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    home: Optional[Path] = None,
    daemon: Optional[Daemon] = None,
) -> None:
    httpd = create_server(store, host, port, home=home, daemon=daemon)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()