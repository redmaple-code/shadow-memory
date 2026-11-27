"""Tests for the zero-token collectors (common / Claude Code / Codex / ZCode)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import shadow_memory.collectors as collectors_pkg
from shadow_memory.collectors import claude_code, codex, common, zcode
from shadow_memory.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "memory.sqlite")
    yield s
    s.close()


@pytest.fixture()
def projdir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return path


# =============================================================================
# common: text cleaning
# =============================================================================


def test_strip_code_removes_fences_and_inline():
    text = "结论如下:\n```python\nprint(1)\n```\n还有 `inline_code` 这种"
    out = common.strip_code(text)
    assert "print(1)" not in out
    assert "inline_code" not in out
    assert "结论如下" in out


def test_strip_urls_keeps_link_label():
    out = common.strip_urls("见 [文档](https://example.com/a/b.py) 和 https://x.y/z")
    assert "文档" in out
    assert "example.com" not in out
    assert "x.y/z" not in out


def test_clean_prose_strips_markdown():
    out = common.clean_prose("### 标题\n\n- **重点**：这是 ~~删除~~ 内容\n")
    assert "#" not in out
    assert "**" not in out
    assert "~~" not in out
    assert "标题" in out and "重点" in out


def test_clean_title_strips_politeness_prefix():
    assert common.clean_title("请帮我实现 JWT 登录，改 backend/auth.py") == "实现 JWT 登录"
    assert common.clean_title("帮我检查一下 OVITO 渲染性能。") == "检查一下 OVITO 渲染性能"


def test_clean_title_trims_long_text_with_ellipsis():
    title = common.clean_title("这是一个非常非常长的标题" * 8, max_chars=40)
    assert len(title) <= 41
    assert title.endswith("…")


def test_clean_title_empty():
    assert common.clean_title("") == ""
    assert common.clean_title(None) == ""


def test_first_sentences_never_cuts_mid_sentence():
    text = "第一句话。第二句话。第三句话很长很长很长。"
    out = common.first_sentences(text, max_chars=5)
    assert out == "第一句话。"
    # a bigger budget keeps whole sentences, still no partial tail
    assert common.first_sentences(text, max_chars=12) == "第一句话。第二句话。"


def test_extract_next_steps():
    text = "改动完成。\n下一步： 运行 pytest\nTODO: 补文档"
    steps = common.extract_next_steps(text)
    assert "运行 pytest" in steps
    assert "补文档" in steps


def test_extract_next_steps_ignores_code_blocks():
    text = "```\n下一步: 这是代码里的\n```\nTODO: 真实待办"
    steps = common.extract_next_steps(text)
    assert "代码里的" not in steps
    assert "真实待办" in steps


# =============================================================================
# common: path extraction
# =============================================================================


def test_extract_paths_excludes_urls():
    text = "见 https://example.com/a/b.py 与 src/x.ts"
    paths = common.extract_paths(text, cwd="/repo")
    assert "src/x.ts" in paths
    assert not any("example.com" in p for p in paths)


def test_extract_paths_relativises_cwd():
    paths = common.extract_paths("改 G:" + "\\repo\\src\\main.py", cwd="G:/repo")
    assert paths == ["src/main.py"]


def test_extract_paths_normalises_dot_slash():
    paths = common.extract_paths("看 ./lib/util.ts", cwd="/repo")
    assert "lib/util.ts" in paths


def test_extract_paths_ignores_host_port_and_versions():
    paths = common.extract_paths("监听 127.0.0.1:8080 版本 v1.2.3", cwd="/repo")
    assert paths == []


def test_extract_paths_dedupes():
    paths = common.extract_paths("src/a.py 和 src/a.py 还有 ./src/a.py", cwd="/repo")
    assert paths.count("src/a.py") == 1


def test_extract_paths_limit():
    text = " ".join(f"src/file{i}.py" for i in range(50))
    assert len(common.extract_paths(text, cwd="/repo", limit=5)) == 5


# =============================================================================
# common: digest
# =============================================================================


def test_normalise_messages_role_mapping():
    msgs = common.normalise_messages(
        [
            {"role": "human", "content": "hi"},
            {"role": "ai", "content": [{"type": "text", "text": "hello"}]},
            {"role": "user", "content": "   "},  # dropped
            "not a dict",  # dropped
        ]
    )
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["text"] == "hello"


def test_extract_digest_empty():
    d = common.extract_digest([])
    assert d["message_count"] == 0
    assert d["touched_files"] == []
    assert d["clean_title"] == ""


def test_extract_digest_full_shape():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "请修复 backend/api.py 的分页"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "已修复 backend/api.py。\n```python\nx = 1\n```\n下一步： 补单元测试",
                }
            ],
        },
    ]
    d = common.extract_digest(msgs, cwd="/repo")
    assert d["clean_title"] == "修复 backend/api.py 的分页"
    assert "backend/api.py" in d["touched_files"]
    assert "x = 1" not in d["outcome"]
    assert d["next_steps"] == "补单元测试"
    assert d["message_count"] == 2
    assert "→" in d["summary"]


def test_extract_digest_falls_back_to_second_assistant():
    msgs = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "有意义的结论。"},
        {"role": "assistant", "content": "```code only```"},
    ]
    d = common.extract_digest(msgs)
    assert "有意义的结论" in d["outcome"]


def test_digest_caps_touched_files():
    files = " ".join(f"src/m{i}.py" for i in range(40))
    d = common.extract_digest([{"role": "user", "content": files}], cwd="/repo")
    assert len(d["touched_files"]) == 20


def test_iter_jsonl_skips_broken_lines(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"a":1}\n\nnot json\n[1,2]\n{"b":2}\n', encoding="utf-8")
    objs = list(common.iter_jsonl(p))
    assert [o.get("a") or o.get("b") for o in objs] == [1, 2]


def test_iter_jsonl_missing_file(tmp_path):
    assert list(common.iter_jsonl(tmp_path / "nope.jsonl")) == []


# =============================================================================
# Claude Code
# =============================================================================


@pytest.fixture()
def claude_projects(tmp_path, projdir):
    base = tmp_path / ".claude" / "projects" / "-repo"
    _write_jsonl(
        base / "sess-abc.jsonl",
        [
            {
                "type": "user",
                "sessionId": "sess-abc",
                "cwd": str(projdir),
                "timestamp": "2026-11-26T10:00:00Z",
                "message": {"role": "user", "content": "请帮我实现 JWT 登录，改 backend/auth.py"},
            },
            {
                "type": "assistant",
                "sessionId": "sess-abc",
                "cwd": str(projdir),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "已完成。修改了 backend/auth.py 与 tests/test_auth.py。\n"
                            "```python\nprint('ok')\n```\n下一步： 运行 pytest",
                        }
                    ],
                },
            },
            {"type": "summary", "summary": "实现 JWT 登录"},
        ],
    )
    return base


def test_claude_collect(store, claude_projects, projdir):
    report = claude_code.collect(store, claude_projects)
    assert report["count"] == 1
    sessions = store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["agent"] == "claude-code"
    assert s["session_id"] == "sess-abc"
    assert s["clean_title"] == "实现 JWT 登录"
    assert s["summary"]
    assert "print('ok')" not in s["outcome"]
    assert s["next_steps"] == "运行 pytest"
    assert "backend/auth.py" in s["touched_files"]
    assert "tests/test_auth.py" in s["touched_files"]
    assert s["message_count"] == 2
    assert s["project_id"] == store.ensure_project(str(projdir))["project_id"]
    assert s["digest_source"].endswith("sess-abc.jsonl")


def test_claude_collect_is_idempotent(store, claude_projects):
    claude_code.collect(store, claude_projects)
    claude_code.collect(store, claude_projects)
    assert len(store.list_sessions()) == 1


def test_claude_collect_missing_dir(store, tmp_path):
    report = claude_code.collect(store, tmp_path / "nowhere")
    assert report["count"] == 0
    assert report["skipped"] == "no base dir"


def test_claude_collect_skips_empties(store, tmp_path):
    base = tmp_path / "proj"
    _write_jsonl(base / "empty.jsonl", [{"type": "summary", "summary": "只有摘要"}])
    report = claude_code.collect(store, base)
    assert report["count"] == 0


def test_claude_collect_nested_and_limit(store, tmp_path, projdir):
    base = tmp_path / "proj"
    for i in range(4):
        _write_jsonl(
            base / f"sub{i}" / f"s{i}.jsonl",
            [
                {"type": "user", "cwd": str(projdir), "message": {"role": "user", "content": f"任务 {i}"}},
                {"type": "assistant", "cwd": str(projdir), "message": {"role": "assistant", "content": f"完成 {i}"}},
            ],
        )
    assert claude_code.collect(store, base, limit=2)["count"] == 2


def test_claude_collect_redacts(store, tmp_path, projdir):
    base = tmp_path / "proj"
    _write_jsonl(
        base / "secret.jsonl",
        [
            {
                "type": "user",
                "cwd": str(projdir),
                "message": {"role": "user", "content": "配置 key sk-abcdefghijklmnop9999 与手机 13800138000"},
            }
        ],
    )
    claude_code.collect(store, base)
    s = store.list_sessions()[0]
    blob = json.dumps(s, ensure_ascii=False)
    assert "sk-abcdefghijklmnop" not in blob
    assert "13800138000" not in blob


def test_claude_collect_tolerates_broken_file(store, tmp_path, projdir):
    base = tmp_path / "brokenbase"
    base.mkdir(parents=True)
    (base / "bad.jsonl").write_text("{{{ not json at all\n", encoding="utf-8")
    _write_jsonl(
        base / "good.jsonl",
        [{"type": "user", "cwd": str(projdir), "message": {"role": "user", "content": "好文件"}}],
    )
    report = claude_code.collect(store, base)
    assert report["count"] == 1


# =============================================================================
# Codex
# =============================================================================


@pytest.fixture()
def codex_sessions(tmp_path, projdir):
    base = tmp_path / ".codex" / "sessions" / "2026" / "11"
    _write_jsonl(
        base / "rollout-1.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "cx-1", "cwd": str(projdir)}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "重构支付模块"}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "已重构，改了 backend/pay.py。\nTODO: 补集成测试",
                },
            },
        ],
    )
    return base


def test_codex_collect(store, codex_sessions, projdir):
    report = codex.collect(store, codex_sessions)
    assert report["count"] == 1
    s = store.list_sessions()[0]
    assert s["agent"] == "codex"
    assert s["session_id"] == "cx-1"
    assert s["clean_title"] == "重构支付模块"
    assert "backend/pay.py" in s["touched_files"]
    assert s["next_steps"] == "补集成测试"
    assert s["message_count"] == 2
    assert s["project_id"] == store.ensure_project(str(projdir))["project_id"]


def test_codex_event_msg_user_message(store, tmp_path, projdir):
    base = tmp_path / "s"
    _write_jsonl(
        base / "r.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "cx-2", "cwd": str(projdir)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "修一下导出"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "已修复 exporter.py"}},
        ],
    )
    codex.collect(store, base)
    s = store.list_sessions()[0]
    assert s["clean_title"] == "修一下导出"
    assert "exporter.py" in s["touched_files"]


def test_codex_legacy_flat_shape(store, tmp_path, projdir):
    base = tmp_path / "s"
    _write_jsonl(
        base / "r.jsonl",
        [
            {"type": "user", "cwd": str(projdir), "message": "旧格式提问"},
            {"type": "assistant", "message": "旧格式回答，改了 lib/old.ts"},
        ],
    )
    codex.collect(store, base)
    assert store.list_sessions()[0]["clean_title"] == "旧格式提问"


def test_codex_missing_dir(store, tmp_path):
    report = codex.collect(store, tmp_path / "none")
    assert report["count"] == 0 and report["skipped"] == "no base dir"


# =============================================================================
# ZCode
# =============================================================================

ZCODE_SCHEMA = """
CREATE TABLE session(id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                     time_created INTEGER, time_updated INTEGER);
CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                     time_updated INTEGER, data TEXT, sequence INTEGER);
CREATE TABLE part(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                  time_created INTEGER, time_updated INTEGER, data TEXT, sequence INTEGER);
"""


def _make_zcode_db(path: Path, projdir: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(ZCODE_SCHEMA)
    c.execute(
        "INSERT INTO session VALUES (?,?,?,?,?)",
        ("sess-z1", "重构支付模块", str(projdir), 1795698000000, 1795698612000),
    )

    def msg(mid: str, role: str, seq: int, extra: dict | None = None) -> None:
        data = {"role": role}
        if extra:
            data.update(extra)
        c.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?)",
            (mid, "sess-z1", 1795698000000 + seq, 1795698000000 + seq,
             json.dumps(data, ensure_ascii=False), seq),
        )

    def part(pid: str, mid: str, data: dict, seq: int) -> None:
        c.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (pid, mid, "sess-z1", 1795698000000, 1795698000000,
             json.dumps(data, ensure_ascii=False), seq),
        )

    msg("m1", "user", 0)
    part("p1", "m1", {"type": "tool", "tool": "Bash",
                      "state": {"status": "running", "input": {"command": "ls"}}}, 0)
    part("p2", "m1", {"type": "text", "text": "请重构 backend/pay.py，去掉重复逻辑"}, 1)

    msg("m2", "assistant", 1)
    part("p3", "m2", {"type": "text", "text": "已重构 backend/pay.py。\n下一步： 补集成测试"}, 0)
    part("p4", "m2", {"type": "text", "text": "<total_tokens>reminder</total_tokens>",
                      "synthetic": True}, 1)

    msg("m3", "user", 2, {"metadata": {"visibility": "model-only",
                                       "runtimeMessage": {"source": "total_tokens_reminder"}}})
    part("p5", "m3", {"type": "text", "text": "模型专用提醒，不该被采集"}, 0)

    c.commit()
    c.close()
    return path


def test_zcode_collect(store, tmp_path, projdir):
    db = _make_zcode_db(tmp_path / ".zcode" / "cli" / "db" / "db.sqlite", projdir)
    report = zcode.collect(store, db)
    assert report["count"] == 1
    s = store.list_sessions()[0]
    assert s["agent"] == "zcode"
    assert s["session_id"] == "sess-z1"
    assert "重构" in s["clean_title"]
    assert "backend/pay.py" in s["touched_files"]
    assert s["next_steps"] == "补集成测试"
    # tool part + synthetic part + model-only message all skipped
    assert s["message_count"] == 2
    assert "不该被采集" not in json.dumps(s, ensure_ascii=False)
    assert s["updated_at"].startswith("2026-11-26")


def test_zcode_read_only_does_not_modify_db(store, tmp_path, projdir):
    db = _make_zcode_db(tmp_path / "z.db", projdir)
    before = db.stat().st_size
    zcode.collect(store, db)
    assert db.stat().st_size == before


def test_zcode_missing_db(store, tmp_path):
    report = zcode.collect(store, tmp_path / "nope.sqlite")
    assert report["count"] == 0 and report["skipped"] == "no database"


def test_zcode_schema_missing_degrades(store, tmp_path):
    db = tmp_path / "partial.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE session(id TEXT PRIMARY KEY)")
    c.commit()
    c.close()
    report = zcode.collect(store, db)
    assert report["count"] == 0
    assert report["skipped"] == "schema missing"


def test_zcode_no_part_table_degrades(store, tmp_path, projdir):
    db = tmp_path / "noparts.sqlite"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE session(id TEXT, title TEXT, directory TEXT, time_updated INTEGER);")
    c.executescript(
        "CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT, sequence INTEGER);"
    )
    c.execute("INSERT INTO session VALUES ('s1','t',?,1)", (str(projdir),))
    c.execute("INSERT INTO message VALUES ('m1','s1',1,'{\"role\":\"user\"}',0)")
    c.commit()
    c.close()
    report = zcode.collect(store, db)
    assert report["count"] == 0
    assert "error" not in report or report["count"] == 0


def test_zcode_garbage_db_does_not_raise(store, tmp_path):
    db = tmp_path / "garbage.sqlite"
    db.write_bytes(b"this is definitely not a sqlite database")
    report = zcode.collect(store, db)  # must not raise
    assert report["count"] == 0


def test_zcode_limit_orders_by_updated(store, tmp_path, projdir):
    db = _make_zcode_db(tmp_path / "z.db", projdir)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO session VALUES ('old','旧会话',?,1,2)", (str(projdir),))
    c.execute("INSERT INTO message VALUES ('mo','old',1,1,'{\"role\":\"user\"}',0)")
    c.execute("INSERT INTO part VALUES ('po','mo','old',1,1,?,0)",
              (json.dumps({"type": "text", "text": "旧内容"}),))
    c.commit()
    c.close()
    report = zcode.collect(store, db, limit=1)
    assert report["count"] == 1
    assert store.list_sessions()[0]["session_id"] == "sess-z1"


# =============================================================================
# dispatcher
# =============================================================================


@pytest.fixture()
def full_home(tmp_path, projdir):
    _write_jsonl(
        tmp_path / ".claude" / "projects" / "p" / "c.jsonl",
        [
            {"type": "user", "cwd": str(projdir), "sessionId": "c1",
             "message": {"role": "user", "content": "claude 任务"}},
            {"type": "assistant", "cwd": str(projdir), "sessionId": "c1",
             "message": {"role": "assistant", "content": "claude 完成"}},
        ],
    )
    _write_jsonl(
        tmp_path / ".codex" / "sessions" / "p" / "x.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "x1", "cwd": str(projdir)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "codex 任务"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "codex 完成"}},
        ],
    )
    _make_zcode_db(tmp_path / ".zcode" / "cli" / "db" / "db.sqlite", projdir)
    return tmp_path


def test_collect_all_runs_every_agent(store, full_home):
    report = collectors_pkg.collect_all(store, home=full_home)
    assert set(report["agents"]) == {"claude-code", "codex", "zcode"}
    assert report["total"] == 3
    assert store.counts()["sessions"] == 3


def test_collect_all_isolates_failures(store, full_home, monkeypatch):
    def boom(store_, home=None, *, limit=400):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(collectors_pkg, "COLLECTORS", (("claude-code", boom),))
    report = collectors_pkg.collect_all(store, home=full_home)
    assert report["agents"]["claude-code"]["count"] == 0
    assert "collector exploded" in report["agents"]["claude-code"]["error"]
    assert report["total"] == 0


def test_collect_all_empty_home(store, tmp_path):
    report = collectors_pkg.collect_all(store, home=tmp_path / "empty")
    assert report["total"] == 0
    for r in report["agents"].values():
        assert r["count"] == 0