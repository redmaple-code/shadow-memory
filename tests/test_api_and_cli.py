"""End-to-end tests for the HTTP API, the web UI and the ``smem`` CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shadow_memory import api as api_mod
from shadow_memory import cli, compiler
from shadow_memory.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated home + data dir + one project (never touches the real ones)."""
    monkeypatch.setenv("SHADOW_MEMORY_HOME", str(tmp_path / "smem-home"))
    home = tmp_path / "agent-home"
    home.mkdir()
    project_root = tmp_path / "proj"
    project_root.mkdir()
    store = Store(tmp_path / "smem-home" / "memory.sqlite")
    proj = store.ensure_project(str(project_root))
    yield {
        "store": store,
        "home": home,
        "root": project_root,
        "proj": proj,
        "tmp": tmp_path,
        "db": tmp_path / "smem-home" / "memory.sqlite",
    }
    store.close()


@pytest.fixture()
def server(env):
    httpd = api_mod.create_server(
        env["store"], "127.0.0.1", 0, home=env["home"]
    )
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def post_json(base: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status_of(base: str, path: str, payload: dict | None = None) -> int:
    """Return the HTTP status for a request expected to fail."""
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# =============================================================================
# Web UI
# =============================================================================


def test_root_serves_html(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        html = resp.read().decode("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "shadow-memory" in html


def test_html_has_all_four_tabs(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    for tab in ("记忆账本", "跨会话现场", "影子文件", "接入与状态"):
        assert tab in html
    # three-column workstation layout: sidebar / main / inspector
    assert "sidebar" in html and "inspector" in html


def test_html_is_offline_capable(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    # no external scripts/stylesheets/fonts at all
    assert "cdn." not in html
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "<link" not in html


def test_html_has_inspector_and_modal(server):
    with urllib.request.urlopen(server + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert "版本演进链" in html
    assert "检查器" in html
    assert "+ 手动记一条" in html
    assert "token" in html.lower()


def test_favicon_returns_no_content(server):
    with urllib.request.urlopen(server + "/favicon.ico", timeout=10) as resp:
        assert resp.status == 204


# =============================================================================
# GET endpoints
# =============================================================================


def test_status(server, env):
    d = get_json(server, "/api/status")
    assert d["ok"] is True
    assert d["service"] == "shadow-memory"
    assert d["host"] == "127.0.0.1"
    assert str(env["db"]) in d["data"]["db"]
    assert d["counts"]["projects"] == 1
    assert len(d["agents"]) == 4
    assert d["budget"] == 450


def test_projects(server, env):
    d = get_json(server, "/api/projects")
    assert d["ok"] is True
    assert len(d["projects"]) == 1
    p = d["projects"][0]
    assert p["project_id"] == env["proj"]["project_id"]
    assert p["fact_count"] == 0 and p["session_count"] == 0


def test_facts_empty(server):
    d = get_json(server, "/api/facts")
    assert d["ok"] is True and d["count"] == 0 and d["facts"] == []


def test_write_and_list_facts(server, env):
    res = post_json(server, "/api/facts", {
        "content": "API 写入的记忆条目",
        "kind": "decision",
        "project_id": env["proj"]["project_id"],
        "evidence": "单元测试",
    })
    assert res["ok"] is True and res["status"] == "created"
    assert len(res["short_id"]) == 6

    d = get_json(server, "/api/facts?project_id=" + env["proj"]["project_id"])
    assert d["count"] == 1
    fact = d["facts"][0]
    assert fact["content"] == "API 写入的记忆条目"
    assert fact["kind"] == "decision"


def test_write_fact_redacts(server, env):
    post_json(server, "/api/facts", {
        "content": "这里的密钥 sk-abcdefghijklmnop1234 不该留存",
        "project_id": env["proj"]["project_id"],
    })
    d = get_json(server, "/api/facts")
    blob = json.dumps(d, ensure_ascii=False)
    assert "sk-abcdefghijklmnop" not in blob
    assert "REDACTED" in blob


def test_write_fact_without_content_is_400(server):
    assert status_of(server, "/api/facts", {"kind": "fact"}) == 400


def test_write_fact_triggers_recompile(server, env):
    post_json(server, "/api/facts", {
        "content": "写入应立即重编译影子文件",
        "project_id": env["proj"]["project_id"],
    })
    memory = env["root"] / ".agents" / "MEMORY.md"
    assert memory.exists()
    assert "写入应立即重编译影子文件" in memory.read_text(encoding="utf-8")


def test_facts_search_filter(server, env):
    pid = env["proj"]["project_id"]
    post_json(server, "/api/facts", {"content": "搜索目标：影子索引", "kind": "decision", "project_id": pid})
    post_json(server, "/api/facts", {"content": "另一个无关条目", "kind": "fact", "project_id": pid})
    d = get_json(server, "/api/facts?q=" + urllib.parse.quote("影子") )
    assert d["count"] == 1
    d = get_json(server, "/api/facts?kind=fact")
    assert d["count"] == 1 and d["facts"][0]["kind"] == "fact"


def test_fact_chain_endpoint(server, env):
    pid = env["proj"]["project_id"]
    first = post_json(server, "/api/facts", {"content": "第一版：用 MySQL", "project_id": pid})
    second = post_json(server, "/api/facts", {
        "content": "第二版：用 PostgreSQL",
        "project_id": pid,
        "supersedes_id": "#" + first["short_id"],
    })
    assert second["status"] == "superseded"

    d = get_json(server, f"/api/facts/{first['short_id']}/chain")
    assert d["ok"] is True
    assert d["length"] == 2
    assert [c["fact_id"] for c in d["chain"]] == [first["fact_id"], second["fact_id"]]
    assert d["fact"]["fact_id"] == second["fact_id"]  # current tip
    assert d["chain"][0]["status"] == "superseded"


def test_chain_unknown_returns_404(server):
    assert status_of(server, "/api/facts/deadbeef/forget") == 404
    try:
        get_json(server, "/api/facts/deadbeef/chain")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:  # pragma: no cover
        pytest.fail("expected 404")


def test_forget_and_restore(server, env):
    pid = env["proj"]["project_id"]
    created = post_json(server, "/api/facts", {"content": "将被废弃的条目", "project_id": pid})
    out = post_json(server, f"/api/facts/{created['short_id']}/forget", {"reason": "测试"})
    assert out["ok"] is True and out["status"] == "forgotten"

    active = get_json(server, "/api/facts?status=active")
    assert active["count"] == 0

    back = post_json(server, f"/api/facts/{created['short_id']}/restore")
    assert back["ok"] is True and back["status"] == "active"
    assert get_json(server, "/api/facts?status=active")["count"] == 1


def test_sessions_endpoint(server, env):
    env["store"].upsert_session_digest(
        agent="zcode",
        session_id="s1",
        project_id=env["proj"]["project_id"],
        clean_title="实时会话",
        summary="摘要",
        outcome="结论",
        next_steps="下一步",
        touched_files=["src/a.py"],
    )
    d = get_json(server, "/api/sessions")
    assert d["ok"] is True and d["count"] == 1
    assert d["sessions"][0]["clean_title"] == "实时会话"
    assert d["pulse"][0]["state"] in ("RUNNING", "RECENT")


def test_shadow_files(server, env):
    pid = env["proj"]["project_id"]
    post_json(server, "/api/facts", {"content": "预览用记忆", "project_id": pid})
    for name in ("MEMORY.md", "decisions.md", "LIVE_PULSE.md"):
        d = get_json(server, f"/api/shadow?project_id={pid}&file={name}")
        assert d["ok"] is True and d["exists"] is True
        assert d["content"]
        assert d["est_tokens"] > 0
    mem = get_json(server, f"/api/shadow?project_id={pid}&file=MEMORY.md")
    assert mem["est_tokens"] <= compiler.DEFAULT_INDEX_BUDGET * 1.35


def test_shadow_inbox_processed(server, env):
    inbox = env["root"] / compiler.AGENTS_DIR / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps({"text": "inbox 归档查看"}, ensure_ascii=False) + "\n", encoding="utf-8")
    post_json(server, "/api/inbox/consume", {"project_id": env["proj"]["project_id"]})
    d = get_json(server, f"/api/shadow?project_id={env['proj']['project_id']}&file=inbox.processed.jsonl")
    assert d["exists"] is True
    assert "inbox 归档查看" in d["content"]


def test_shadow_rejects_unknown_file(server, env):
    code = status_of(server, "/api/shadow?file=../AGENTS.md")
    assert code in (400, 404)


def test_agents_endpoint(server):
    d = get_json(server, "/api/agents")
    assert d["ok"] is True
    assert {a["agent"] for a in d["agents"]} == {"claude-code", "codex", "zcode", "antigravity"}
    for a in d["agents"]:
        assert "configured" in a and "path" in a and "sessions_count" in a


def test_unknown_route_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + "/api/nope", timeout=10)
    assert exc.value.code == 404


# =============================================================================
# POST endpoints
# =============================================================================


def test_sync_all(server, env):
    d = post_json(server, "/api/sync", {})
    assert d["ok"] is True
    assert isinstance(d["projects"], list)
    assert d["collected"]["total"] == 0  # isolated fake home has no transcripts


def test_sync_single_project(server, env):
    d = post_json(server, "/api/sync", {"project_id": env["proj"]["project_id"]})
    assert d["ok"] is True
    assert len(d["projects"]) == 1
    assert d["projects"][0]["project_id"] == env["proj"]["project_id"]


def test_sync_unknown_project_404(server):
    assert status_of(server, "/api/sync", {"project_id": "nope"}) == 404


def test_compile_endpoint(server, env):
    d = post_json(server, "/api/compile", {"project_id": env["proj"]["project_id"]})
    assert d["ok"] is True
    assert d["project_id"] == env["proj"]["project_id"]
    assert (env["root"] / ".agents" / "MEMORY.md").exists()
    assert (env["root"] / "AGENTS.md").exists()


def test_consume_inbox_endpoint(server, env):
    inbox = env["root"] / compiler.AGENTS_DIR / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps({"kind": "decision", "text": "inbox 端到端条目"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    d = post_json(server, "/api/inbox/consume", {"project_id": env["proj"]["project_id"]})
    assert d["ok"] is True
    assert d["inbox"]["consumed"] == 1
    facts = get_json(server, "/api/facts?status=active")
    assert any(f["content"] == "inbox 端到端条目" for f in facts["facts"])


def test_consume_inbox_requires_project(server, env):
    # add a second project so "auto-pick" is ambiguous
    other = env["tmp"] / "proj2"
    other.mkdir()
    env["store"].ensure_project(str(other))
    assert status_of(server, "/api/inbox/consume", {}) == 400


def test_server_sets_security_headers(server):
    with urllib.request.urlopen(server + "/api/status", timeout=10) as resp:
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-store"


def test_status_reflects_new_facts(server, env):
    before = get_json(server, "/api/status")["counts"]["facts_active"]
    post_json(server, "/api/facts", {"content": "状态计数更新", "project_id": env["proj"]["project_id"]})
    after = get_json(server, "/api/status")["counts"]["facts_active"]
    assert after == before + 1


def test_api_write_without_project_uses_single_project(server, env):
    env["store"]._conn.execute("DELETE FROM project WHERE project_id != ?", (env["proj"]["project_id"],))
    d = post_json(server, "/api/facts", {"content": "自动归属唯一项目"})
    assert d["ok"] is True
    fact = get_json(server, f"/api/facts?q=" + urllib.parse.quote("自动归属"))["facts"][0]
    assert fact["project_id"] == env["proj"]["project_id"]


# =============================================================================
# CLI
# =============================================================================


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("init", "sync", "start", "web", "write", "forget", "status", "uninstall"):
        assert cmd in out


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "smem 0.1.0" in capsys.readouterr().out


def test_cli_no_args_prints_help(capsys):
    assert cli.main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_status(env, capsys):
    code = cli.main(["--db", str(env["db"]), "status", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "shadow-memory" in out
    assert "数据库" in out
    assert "活跃记忆" in out


def test_cli_status_json(env, capsys):
    code = cli.main(["--db", str(env["db"]), "status", "--home", str(env["home"]), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out[out.rindex("\n{\n"):])
    assert payload["counts"]["projects"] == 1


def test_cli_write(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    code = cli.main(["--db", str(env["db"]), "write", "-k", "decision", "CLI 写入的结论"])
    out = capsys.readouterr().out
    assert code == 0
    assert "已写入" in out
    assert "450 tokens" in out
    facts = env["store"].search_facts(project_id=env["proj"]["project_id"], status="active")
    assert [f["content"] for f in facts] == ["CLI 写入的结论"]


def test_cli_write_replace_short_id(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    cli.main(["--db", str(env["db"]), "write", "旧结论：单体"])
    old = env["store"].search_facts(project_id=env["proj"]["project_id"], status="active")[0]
    code = cli.main([
        "--db", str(env["db"]), "write", "-k", "decision",
        "--replace", "#" + old["fact_id"][:6], "新结论：微服务",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "已取代" in out
    assert env["store"].get_fact(old["fact_id"])["status"] == "superseded"


def test_cli_write_empty_text(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    code = cli.main(["--db", str(env["db"]), "write", "   "])
    assert code == 2
    assert "错误" in capsys.readouterr().err


def test_cli_forget(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    cli.main(["--db", str(env["db"]), "write", "会被 CLI 废弃的条目"])
    fact = env["store"].search_facts(project_id=env["proj"]["project_id"], status="active")[0]
    code = cli.main(["--db", str(env["db"]), "forget", fact["fact_id"][:6], "--reason", "过期了"])
    out = capsys.readouterr().out
    assert code == 0
    assert "已废弃" in out
    assert env["store"].get_fact(fact["fact_id"])["status"] == "forgotten"


def test_cli_forget_unknown(env, capsys):
    code = cli.main(["--db", str(env["db"]), "forget", "zzzzzz"])
    assert code == 2
    assert "错误" in capsys.readouterr().err


def test_cli_restore(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    cli.main(["--db", str(env["db"]), "write", "待恢复条目"])
    fact = env["store"].search_facts(project_id=env["proj"]["project_id"], status="active")[0]
    cli.main(["--db", str(env["db"]), "forget", fact["fact_id"][:6]])
    capsys.readouterr()
    code = cli.main(["--db", str(env["db"]), "restore", fact["fact_id"][:6]])
    assert code == 0
    assert "已恢复" in capsys.readouterr().out


def test_cli_sync_all(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    code = cli.main(["--db", str(env["db"]), "sync", "--all", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "编译" in out
    assert (env["root"] / ".agents" / "MEMORY.md").exists()


def test_cli_sync_json(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    code = cli.main(["--db", str(env["db"]), "sync", "--all", "--home", str(env["home"]), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert "projects" in payload and "collected" in payload


def test_cli_inspect(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    cli.main(["--db", str(env["db"]), "write", "inspect 用条目"])
    capsys.readouterr()
    code = cli.main(["--db", str(env["db"]), "inspect"])
    out = capsys.readouterr().out
    assert code == 0
    assert "记忆" in out
    assert "inspect 用条目" in out


def test_cli_inspect_file(env, capsys, monkeypatch):
    monkeypatch.chdir(env["root"])
    cli.main(["--db", str(env["db"]), "write", "预览条目内容"])
    capsys.readouterr()
    code = cli.main(["--db", str(env["db"]), "inspect", "--file", "MEMORY.md"])
    out = capsys.readouterr().out
    assert code == 0
    assert "预览条目内容" in out


def test_cli_init_dry_run(env, capsys):
    code = cli.main(["--db", str(env["db"]), "init", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert not (env["home"] / ".claude" / "CLAUDE.md").exists()


def test_cli_init_apply_then_uninstall(env, capsys):
    code = cli.main([
        "--db", str(env["db"]), "init", "--yes",
        "--create-missing", "--home", str(env["home"]),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "已接入" in out
    target = env["home"] / ".zcode" / "AGENTS.md"
    assert "shadow-memory:start" in target.read_text(encoding="utf-8")

    code = cli.main(["--db", str(env["db"]), "uninstall", "--yes", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "已卸载" in out
    assert not target.exists()


def test_cli_uninstall_dry_run(env, capsys):
    cli.main(["--db", str(env["db"]), "init", "--yes", "--create-missing", "--home", str(env["home"])])
    capsys.readouterr()
    code = cli.main(["--db", str(env["db"]), "uninstall", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert (env["home"] / ".codex" / "AGENTS.md").exists()  # still there


def test_cli_init_single_agent(env, capsys):
    code = cli.main([
        "--db", str(env["db"]), "init", "--yes", "--create-missing",
        "--home", str(env["home"]), "--agent", "codex",
    ])
    capsys.readouterr()
    assert code == 0
    assert (env["home"] / ".codex" / "AGENTS.md").exists()
    assert not (env["home"] / ".claude" / "CLAUDE.md").exists()


def test_cli_uses_env_home(env, capsys, monkeypatch):
    """Without --db the CLI must honour SHADOW_MEMORY_HOME."""
    monkeypatch.chdir(env["root"])
    code = cli.main(["status", "--home", str(env["home"])])
    out = capsys.readouterr().out
    assert code == 0
    assert str(env["db"]) in out


# =============================================================================
# module entry point (acceptance criterion #2)
# =============================================================================


def test_python_m_shadow_memory_help():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "shadow_memory", "--help"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    assert "shadow-memory" in proc.stdout
    assert "sync" in proc.stdout


def test_no_third_party_imports_in_package():
    """The runtime must be pure stdlib — this is a hard project constraint."""
    import ast

    stdlib = set(sys.stdlib_module_names)
    offenders = []
    pkg = REPO_ROOT / "shadow_memory"
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in stdlib and root != "shadow_memory":
                        offenders.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import inside the package
                    continue
                root = (node.module or "").split(".")[0]
                if root and root not in stdlib and root != "shadow_memory":
                    offenders.append((path.name, node.module))
    assert offenders == []


def test_no_agent_hub_import():
    for path in (REPO_ROOT / "shadow_memory").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import node." not in text
        assert "from node" not in text
        assert "agent-hub" not in text.replace("agent-hub-recall", "")
        assert "D:\\agent-hub" not in text