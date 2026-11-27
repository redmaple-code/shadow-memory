"""Tests for the shadow compiler: budget, short ids, live pulse, cache safety."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from shadow_memory import compiler as C
from shadow_memory.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "memory.sqlite")
    yield s
    s.close()


@pytest.fixture()
def proj(store, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return store.ensure_project(str(root))


@pytest.fixture()
def agents_dir(proj):
    return Path(proj["root"]) / C.AGENTS_DIR


# --- token estimation --------------------------------------------------------


def test_estimate_tokens_cjk_vs_ascii():
    assert C.estimate_tokens("") == 0
    # one Han char ≈ 1 token
    assert 8 <= C.estimate_tokens("跨设备影子记忆服务运行正常") <= 13
    # ascii is much cheaper per character
    assert C.estimate_tokens("a" * 100) < C.estimate_tokens("记" * 100)


def test_estimate_tokens_grows_with_text():
    assert C.estimate_tokens("a" * 200) > C.estimate_tokens("a" * 100)


# --- MEMORY.md ---------------------------------------------------------------


def test_index_contains_short_ids_not_full(store, proj):
    res = store.write_fact(content="索引必须显示六位短 ID", project_id=proj["project_id"])
    out = C.compile_project(store, proj)
    text = Path(out["files"]["memory"]).read_text(encoding="utf-8")
    assert "#" + res["fact_id"][:6] in text
    assert res["fact_id"] not in text  # full id belongs in decisions.md


def test_index_respects_budget(store, proj):
    for i in range(40):
        store.write_fact(
            content=f"第 {i} 条较长的决策说明：这里描述了一个具体的工程约定，用于验证预算上限不会被突破。",
            project_id=proj["project_id"],
            kind="fact",
            force_new=True,
        )
    out = C.compile_project(store, proj)
    assert out["est_tokens"] <= C.DEFAULT_INDEX_BUDGET * 1.35


def test_index_keeps_top3_readable(store, proj):
    ids = []
    for i in range(4):
        r = store.write_fact(
            content=f"约束条目{i}：必须使用 UTC 时间戳",
            project_id=proj["project_id"],
            kind="constraint",
            force_new=True,
        )
        ids.append(r["fact_id"])
    text = C.render_index(proj, store.search_facts(project_id=proj["project_id"], status="active"), C._pulse(store, proj["project_id"]))["text"]
    top3_lines = [l for l in text.splitlines() if l.startswith("- #")]
    assert len(top3_lines) >= 3
    for line in top3_lines[:3]:
        assert line.endswith("UTC 时间戳")  # never truncated mid-sentence
    # the highest-priority three are present
    for fid in ids[:3]:
        assert "#" + fid[:6] in text


def test_index_priority_constraint_over_fact(store, proj):
    store.write_fact(content="普通事实条目 A", project_id=proj["project_id"], kind="fact", force_new=True)
    store.write_fact(content="红线约束条目 B", project_id=proj["project_id"], kind="constraint", force_new=True)
    text = C.render_index(
        proj,
        store.search_facts(project_id=proj["project_id"], status="active"),
        C._pulse(store, proj["project_id"]),
    )["text"]
    assert text.index("红线约束条目 B") < text.index("普通事实条目 A")


def test_index_empty_project_is_helpful(store, proj):
    out = C.compile_project(store, proj)
    text = Path(out["files"]["memory"]).read_text(encoding="utf-8")
    assert "暂无记忆" in text
    assert out["est_tokens"] <= C.DEFAULT_INDEX_BUDGET


def test_index_no_duplicate_lines(store, proj):
    store.write_fact(content="只应出现一次的结论", project_id=proj["project_id"])
    text = C.render_index(
        proj,
        store.search_facts(project_id=proj["project_id"], status="active"),
        C._pulse(store, proj["project_id"]),
    )["text"]
    assert text.count("只应出现一次的结论") == 1


# --- decisions.md ------------------------------------------------------------


def test_decisions_has_full_ids_and_evidence(store, proj):
    r = store.write_fact(
        content="决策内容：使用 SQLite WAL",
        project_id=proj["project_id"],
        kind="decision",
        evidence="压测结果",
        source_agent="claude-code",
        source_session="sess-1",
    )
    out = C.compile_project(store, proj)
    text = Path(out["files"]["decisions"]).read_text(encoding="utf-8")
    assert r["fact_id"] in text
    assert "压测结果" in text
    assert "claude-code" in text


def test_decisions_excludes_forgotten(store, proj):
    r = store.write_fact(content="会被废弃的决策", project_id=proj["project_id"])
    store.forget_fact(r["fact_id"])
    out = C.compile_project(store, proj)
    tree = Path(out["files"]["decisions"]).read_text(encoding="utf-8")
    assert "会被废弃的决策" not in tree


# --- LIVE_PULSE --------------------------------------------------------------


def _session(store, proj, agent, sid, title, seconds_ago):
    from datetime import timedelta
    from shadow_memory.store import iso, utcnow

    ts = iso(utcnow() - timedelta(seconds=seconds_ago))
    store.upsert_session_digest(
        agent=agent,
        session_id=sid,
        project_id=proj["project_id"],
        cwd=proj["root"],
        clean_title=title,
        summary="摘要",
        outcome="结论",
        touched_files=["src/a.py"],
        message_count=5,
        updated_at=ts,
    )
    return ts


def test_live_pulse_real_total_and_states(store, proj):
    _session(store, proj, "claude-code", "s1", "正在跑的任务", 60)      # RUNNING
    _session(store, proj, "codex", "s2", "刚结束的任务", 10 * 60)        # RECENT
    for i in range(12):
        _session(store, proj, "zcode", f"old{i}", f"旧任务{i}", 20 * 60)  # RECENT, beyond top-10

    out = C.compile_project(store, proj)
    text = Path(out["files"]["live_pulse"]).read_text(encoding="utf-8")
    assert "14" in text  # real total, not the displayed count
    assert "[RUNNING]" in text
    assert "[RECENT]" in text
    assert text.count("- [") == 10  # at most 10 displayed
    assert "另有 4 条未展示" in text


def test_pulse_excludes_stale_sessions(store, proj):
    _session(store, proj, "codex", "old", "两小时前的任务", 2 * 3600)
    text = C.render_live_pulse(proj, C._pulse(store, proj["project_id"]), "now")
    assert "两小时前的任务" not in text
    assert "无活跃会话" in text


def test_index_shows_running_session_titles(store, proj):
    _session(store, proj, "zcode", "s1", "实现登录功能", 30)
    _session(store, proj, "codex", "s2", "重构支付模块", 200)
    _session(store, proj, "claude-code", "s3", "修复导出 bug", 900)
    text = C.render_index(
        proj,
        store.search_facts(project_id=proj["project_id"], status="active"),
        C._pulse(store, proj["project_id"]),
    )["text"]
    assert "[RUNNING] 实现登录功能" in text
    for t in ("重构支付模块", "修复导出 bug"):
        assert t in text


# --- session cards -----------------------------------------------------------


def test_session_cards_written(store, proj):
    _session(store, proj, "codex", "abc123", "会话卡片测试", 60)
    out = C.compile_project(store, proj)
    sessions = Path(out["files"]["sessions"])
    cards = list(sessions.glob("s_*.md"))
    assert len(cards) == 1
    assert len(cards[0].stem) == 10  # s_ + 8 hex
    body = cards[0].read_text(encoding="utf-8")
    assert "会话卡片测试" in body
    assert "末轮结论" in body and "未完事项" in body and "涉及文件" in body
    latest = (sessions / "latest.md").read_text(encoding="utf-8")
    assert "会话卡片测试" in latest
    assert cards[0].name in latest


def test_cards_capped_at_15(store, proj):
    for i in range(20):
        _session(store, proj, "zcode", f"s{i}", f"任务{i}", i + 5)
    out = C.compile_project(store, proj)
    cards = list(Path(out["files"]["sessions"]).glob("s_*.md"))
    assert len(cards) == C.MAX_SESSION_CARDS


# --- caching safety ----------------------------------------------------------


def test_agents_md_written_then_stable(store, proj):
    root = Path(proj["root"])
    C.compile_project(store, proj)
    agents = root / "AGENTS.md"
    assert agents.exists()
    text = agents.read_text(encoding="utf-8")
    assert C.BLOCK_START in text and C.BLOCK_END in text
    assert "smem write" in text

    first_mtime = agents.stat().st_mtime_ns
    time.sleep(0.02)
    C.compile_project(store, proj)
    assert agents.stat().st_mtime_ns == first_mtime  # unchanged content ⇒ no touch


def test_agents_md_preserves_user_content(store, proj):
    root = Path(proj["root"])
    agents = root / "AGENTS.md"
    agents.write_text("# 我自己的规则\n\n不要删我。\n", encoding="utf-8")
    C.compile_project(store, proj)
    text = agents.read_text(encoding="utf-8")
    assert "不要删我。" in text
    assert C.BLOCK_START in text
    # re-running must not duplicate the block
    C.compile_project(store, proj, force_agents_sync=True)
    assert text.count(C.BLOCK_START) == agents.read_text(encoding="utf-8").count(C.BLOCK_START) == 1


def test_agents_md_deferred_while_active(store, proj):
    root = Path(proj["root"])
    C.compile_project(store, proj)
    before = (root / "AGENTS.md").read_text(encoding="utf-8")
    _session(store, proj, "zcode", "live", "正在运行", 5)  # inside the 180 s window
    out = C.compile_project(store, proj)
    assert out["agents_md_deferred"] is True
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == before


def test_agents_md_force_overrides_defer(store, proj):
    _session(store, proj, "zcode", "live", "正在运行", 5)
    C.compile_project(store, proj)  # deferred, file may not exist
    out = C.compile_project(store, proj, force_agents_sync=True)
    assert out["agents_md_deferred"] is False
    assert (Path(proj["root"]) / "AGENTS.md").exists()


def test_agents_md_written_after_quiet_period(store, proj):
    _session(store, proj, "zcode", "s1", "十分钟前的任务", 600)
    out = C.compile_project(store, proj)
    assert out["agents_md_deferred"] is False
    assert (Path(proj["root"]) / "AGENTS.md").exists()


# --- git hygiene -------------------------------------------------------------


def test_git_exclude_created(tmp_path, store):
    root = tmp_path / "repo"
    (root / ".git" / "info").mkdir(parents=True)
    proj = store.ensure_project(str(root))
    C.compile_project(store, proj)
    exclude = root / ".git" / "info" / "exclude"
    assert exclude.exists()
    assert ".agents/" in exclude.read_text(encoding="utf-8")
    # idempotent
    C.compile_project(store, proj)
    assert exclude.read_text(encoding="utf-8").count(".agents/") == 1


def test_local_gitignore_without_git(tmp_path, store):
    root = tmp_path / "plain"
    root.mkdir()
    proj = store.ensure_project(str(root))
    C.compile_project(store, proj)
    gi = root / C.AGENTS_DIR / ".gitignore"
    assert gi.exists() and gi.read_text(encoding="utf-8").strip() == "*"


# --- inbox -------------------------------------------------------------------


def _write_inbox(root: Path, lines):
    p = Path(root) / C.AGENTS_DIR / "inbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    return p


def test_consume_inbox_creates_facts(store, proj):
    _write_inbox(
        proj["root"],
        [
            {"kind": "decision", "text": "inbox 条目一：使用 SQLite"},
            {"content": "inbox 条目二：零第三方依赖", "kind": "constraint"},
        ],
    )
    res = C.consume_inbox(store, proj)
    assert res["consumed"] == 2
    contents = {f["content"] for f in store.search_facts(project_id=proj["project_id"], status="active")}
    assert "inbox 条目一：使用 SQLite" in contents
    assert "inbox 条目二：零第三方依赖" in contents


def test_consume_inbox_clears_and_archives(store, proj):
    inbox = _write_inbox(proj["root"], [{"text": "归档测试条目"}])
    res = C.consume_inbox(store, proj)
    assert inbox.read_text(encoding="utf-8") == ""
    proc = Path(proj["root"]) / C.AGENTS_DIR / "inbox.processed.jsonl"
    assert proc.exists()
    recs = [json.loads(l) for l in proc.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(recs) == 1
    assert recs[0]["status"] == "created"
    assert len(recs[0]["short_id"]) == 6


def test_consume_inbox_replaces_by_short_id(store, proj):
    old = store.write_fact(content="旧结论：用 MySQL", project_id=proj["project_id"], kind="decision")
    _write_inbox(
        proj["root"],
        [{"kind": "decision", "text": "新结论：用 PostgreSQL", "replace": "#" + old["fact_id"][:6]}],
    )
    C.consume_inbox(store, proj)
    assert store.get_fact(old["fact_id"])["status"] == "superseded"
    active = store.search_facts(project_id=proj["project_id"], status="active")
    assert [f["content"] for f in active] == ["新结论：用 PostgreSQL"]


def test_consume_inbox_supersedes_id_full(store, proj):
    old = store.write_fact(content="旧架构：单体", project_id=proj["project_id"])
    _write_inbox(
        proj["root"],
        [{"text": "新架构：微服务", "supersedes_id": old["fact_id"]}],
    )
    C.consume_inbox(store, proj)
    assert store.get_fact(old["fact_id"])["superseded_by"]


def test_inbox_archive_capped_at_200(store, proj):
    lines = [{"text": f"批量条目编号 {i}"} for i in range(260)]
    _write_inbox(proj["root"], lines)
    C.consume_inbox(store, proj)
    proc = Path(proj["root"]) / C.AGENTS_DIR / "inbox.processed.jsonl"
    kept = [l for l in proc.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(kept) == 200


def test_inbox_archive_redacts(store, proj):
    _write_inbox(proj["root"], [{"text": "这条含密钥 sk-abcdefghijklmnop9999 与手机 13800138000"}])
    C.consume_inbox(store, proj)
    proc = Path(proj["root"]) / C.AGENTS_DIR / "inbox.processed.jsonl"
    raw = proc.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in raw
    assert "13800138000" not in raw


def test_consume_inbox_skips_broken_lines(store, proj):
    p = Path(proj["root"]) / C.AGENTS_DIR / "inbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{not json}\n{"text":"好条目"}\n\n', encoding="utf-8")
    res = C.consume_inbox(store, proj)
    assert res["consumed"] == 1


def test_consume_inbox_missing_file_is_noop(store, proj):
    res = C.consume_inbox(store, proj)
    assert res["consumed"] == 0


# --- read_shadow_file --------------------------------------------------------


def test_read_shadow_file(store, proj):
    store.write_fact(content="读取影子文件测试", project_id=proj["project_id"])
    C.compile_project(store, proj)
    data = C.read_shadow_file(Path(proj["root"]), "MEMORY.md")
    assert data["exists"] is True
    assert "读取影子文件测试" in data["content"]
    assert data["est_tokens"] == C.estimate_tokens(data["content"])


def test_read_shadow_file_missing(store, proj):
    data = C.read_shadow_file(Path(proj["root"]), "decisions.md")
    assert data["exists"] is False
    assert data["content"] == ""


def test_read_shadow_file_rejects_traversal(store, proj):
    with pytest.raises(KeyError):
        C.read_shadow_file(Path(proj["root"]), "../AGENTS.md")


def test_compile_is_idempotent(store, proj):
    store.write_fact(content="幂等编译测试", project_id=proj["project_id"])
    first = C.compile_project(store, proj)
    second = C.compile_project(store, proj)
    assert first["written"] and not second["written"]