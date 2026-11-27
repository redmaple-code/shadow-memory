"""Tests for storage, redaction, dedupe, version chains, expiry and FTS."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from shadow_memory import redact as redact_mod
from shadow_memory.store import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_FORGOTTEN,
    STATUS_SUPERSEDED,
    Store,
    fts_tokens,
    project_id_for,
    resolve_db_path,
    similarity,
)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "memory.sqlite")
    yield s
    s.close()


@pytest.fixture()
def proj(store, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return store.ensure_project(str(root))


# --- redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-abcdefghijklmnop1234567890",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij",
        "password=Hunter2Secret",
        "token=supersecretvalue",
        "13800138000",
        "someone@example.com",
        "xoxb-123456789012-abcdefghij",
    ],
)
def test_redact_masks_secrets(secret):
    out = redact_mod.redact(f"prefix {secret} suffix")
    assert secret not in out
    assert "REDACTED" in out.upper()


def test_redact_private_key_block():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_mod.redact(f"here it is:\n{block}\nend")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "REDACTED_PRIVATE_KEY" in out


def test_redact_is_idempotent():
    once = redact_mod.redact("key sk-abcdefghijklmnop1234 mail a@b.com")
    twice = redact_mod.redact(once)
    assert once == twice


def test_contains_secret():
    assert redact_mod.contains_secret("AKIAIOSFODNN7EXAMPLE")
    assert not redact_mod.contains_secret("普通的一句话，没有密钥")


def test_redact_dict_recurses():
    data = {"api_key": "plainvalue", "nested": ["a@b.com", {"token": "zzzz"}], "n": 1}
    out = redact_mod.redact_dict(data)
    assert out["api_key"] == redact_mod.MASK
    assert out["nested"][0] == "[REDACTED_EMAIL]"
    assert out["nested"][1]["token"] == redact_mod.MASK
    assert out["n"] == 1


# --- basic persistence -------------------------------------------------------


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_MEMORY_HOME", str(tmp_path / "home"))
    path = resolve_db_path()
    assert path == tmp_path / "home" / "memory.sqlite"


def test_wal_mode_and_schema(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    tables = {
        r[0]
        for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    for t in ("project", "memory_fact", "session_digest"):
        assert t in tables
    assert any("memory_fact_fts" in t for t in tables)


def test_project_id_is_stable(proj, tmp_path):
    same = project_id_for(str(tmp_path / "project"))
    assert same == proj["project_id"]
    # trailing separator must not change identity
    assert project_id_for(str(tmp_path / "project") + os.sep) == proj["project_id"]


def test_write_and_get_fact(store, proj):
    res = store.write_fact(content="统一使用中文回答", project_id=proj["project_id"], kind="preference")
    assert res["status"] == "created"
    assert len(res["short_id"]) == 6
    fact = store.get_fact(res["fact_id"])
    assert fact["content"] == "统一使用中文回答"
    assert fact["status"] == STATUS_ACTIVE


def test_write_redacts_content(store, proj):
    res = store.write_fact(
        content="部署密钥 sk-abcdefghijklmnop1234567890 别外传",
        project_id=proj["project_id"],
    )
    fact = store.get_fact(res["fact_id"])
    assert "sk-abcdefghijklmnop" not in fact["content"]
    assert "REDACTED" in fact["content"]


def test_empty_content_rejected(store, proj):
    with pytest.raises(ValueError):
        store.write_fact(content="   ", project_id=proj["project_id"])


# --- short id resolution -----------------------------------------------------


def test_resolve_short_id(store, proj):
    res = store.write_fact(content="唯一的结论内容 ABC", project_id=proj["project_id"])
    short = res["fact_id"][:6]
    assert store.resolve_fact_id(short) == res["fact_id"]
    assert store.resolve_fact_id("#" + short) == res["fact_id"]
    assert store.resolve_fact_id(res["fact_id"]) == res["fact_id"]


def test_resolve_unknown_raises(store):
    with pytest.raises(KeyError):
        store.resolve_fact_id("deadbeef")


# --- dedupe ------------------------------------------------------------------


def test_similarity_threshold():
    assert similarity("使用 pnpm 而不是 npm", "使用 pnpm 而不是 npm") == 1.0
    assert similarity("使用 pnpm 而不是 npm", "数据库用 WAL 模式") < 0.3


def test_near_duplicate_merges(store, proj):
    a = store.write_fact(content="所有接口必须返回 UTC 时间戳", project_id=proj["project_id"])
    b = store.write_fact(content="所有接口必须返回 UTC 时间戳。", project_id=proj["project_id"])
    assert b["status"] == "merged"
    assert b["fact_id"] == a["fact_id"]
    rows = store.search_facts(project_id=proj["project_id"], status="active")
    assert len(rows) == 1


def test_different_facts_not_merged(store, proj):
    store.write_fact(content="前端使用 React 18", project_id=proj["project_id"])
    store.write_fact(content="后端使用 FastAPI 框架", project_id=proj["project_id"])
    rows = store.search_facts(project_id=proj["project_id"], status="active")
    assert len(rows) == 2


def test_merge_keeps_longer_content_and_evidence(store, proj):
    a = store.write_fact(content="数据库必须开启 WAL", project_id=proj["project_id"], evidence="会议结论")
    store.write_fact(
        content="数据库必须开启 WAL 模式",
        project_id=proj["project_id"],
        evidence="压测数据",
    )
    fact = store.get_fact(a["fact_id"])
    assert "WAL 模式" in fact["content"]
    assert "会议结论" in fact["evidence"] and "压测数据" in fact["evidence"]


def test_force_new_skips_dedupe(store, proj):
    store.write_fact(content="完全一样的句子", project_id=proj["project_id"])
    res = store.write_fact(content="完全一样的句子", project_id=proj["project_id"], force_new=True)
    assert res["status"] == "created"
    assert len(store.search_facts(project_id=proj["project_id"], status="active")) == 2


# --- supersede chains --------------------------------------------------------


def test_supersede_chain(store, proj):
    a = store.write_fact(content="部署目标：阿里云", project_id=proj["project_id"], kind="decision")
    b = store.write_fact(
        content="部署目标改为腾讯云",
        project_id=proj["project_id"],
        kind="decision",
        supersedes_id=a["fact_id"],
    )
    assert b["status"] == "superseded"

    chain = store.get_fact_chain(a["fact_id"])
    assert [c["fact_id"] for c in chain] == [a["fact_id"], b["fact_id"]]
    assert chain[0]["status"] == STATUS_SUPERSEDED
    assert chain[0]["superseded_by"] == b["fact_id"]
    assert chain[1]["supersedes_id"] == a["fact_id"]


def test_supersede_accepts_short_id(store, proj):
    a = store.write_fact(content="第一版方案：单体架构", project_id=proj["project_id"])
    b = store.write_fact(
        content="第二版方案：微服务架构",
        project_id=proj["project_id"],
        supersedes_id="#" + a["fact_id"][:6],
    )
    assert store.get_fact(a["fact_id"])["superseded_by"] == b["fact_id"]


def test_chain_of_three(store, proj):
    a = store.write_fact(content="版本一：用 MySQL", project_id=proj["project_id"])
    b = store.write_fact(content="版本二：用 PostgreSQL", project_id=proj["project_id"], supersedes_id=a["fact_id"])
    c = store.write_fact(content="版本三：用 SQLite", project_id=proj["project_id"], supersedes_id=b["fact_id"])
    chain = store.get_fact_chain(c["fact_id"])
    assert [x["fact_id"] for x in chain] == [a["fact_id"], b["fact_id"], c["fact_id"]]
    # walking from the middle yields the same full chain
    assert [x["fact_id"] for x in store.get_fact_chain(b["fact_id"])] == [
        a["fact_id"],
        b["fact_id"],
        c["fact_id"],
    ]


# --- forget / restore --------------------------------------------------------


def test_forget_and_restore(store, proj):
    res = store.write_fact(content="这条会被废弃", project_id=proj["project_id"])
    store.forget_fact(res["fact_id"], reason="写错了")
    fact = store.get_fact(res["fact_id"])
    assert fact["status"] == STATUS_FORGOTTEN
    assert fact["forget_reason"] == "写错了"

    assert store.search_facts(project_id=proj["project_id"], status="active") == []

    store.restore_fact(res["fact_id"])
    assert store.get_fact(res["fact_id"])["status"] == STATUS_ACTIVE


def test_restore_keeps_superseded_status(store, proj):
    a = store.write_fact(content="旧结论 A", project_id=proj["project_id"])
    store.write_fact(content="新结论 B", project_id=proj["project_id"], supersedes_id=a["fact_id"])
    store.forget_fact(a["fact_id"])
    store.restore_fact(a["fact_id"])
    # it has a successor, so it must come back as superseded, not active
    assert store.get_fact(a["fact_id"])["status"] == STATUS_SUPERSEDED


# --- ttl expiry --------------------------------------------------------------


def test_expire_due(store, proj):
    res = store.write_fact(content="临时结论，很快过期", project_id=proj["project_id"], ttl_days=1)
    # backdate it
    old = "2020-01-01T00:00:00Z"
    store._conn.execute(
        "UPDATE memory_fact SET created_at=? WHERE fact_id=?", (old, res["fact_id"])
    )
    expired = store.expire_due()
    assert expired == 1
    assert store.get_fact(res["fact_id"])["status"] == STATUS_EXPIRED


def test_not_yet_expired(store, proj):
    store.write_fact(content="长期有效的结论", project_id=proj["project_id"], ttl_days=365)
    assert store.expire_due() == 0


# --- search -----------------------------------------------------------------


def test_fts_cjk_bigram_tokens():
    toks = fts_tokens("影子记忆服务")
    assert toks == ["影子", "子记", "记忆", "忆服", "服务"]
    assert fts_tokens("hello world") == ["hello", "world"]


def test_search_chinese_substring(store, proj):
    store.write_fact(content="影子记忆服务必须零依赖", project_id=proj["project_id"])
    store.write_fact(content="前端用 React 构建", project_id=proj["project_id"])
    hits = store.search_facts("记忆", project_id=proj["project_id"])
    assert len(hits) == 1
    assert "零依赖" in hits[0]["content"]


def test_search_english(store, proj):
    store.write_fact(content="Always use pnpm for dependency management", project_id=proj["project_id"])
    hits = store.search_facts("pnpm", project_id=proj["project_id"])
    assert len(hits) == 1


def test_search_filters(store, proj):
    store.write_fact(content="筛选测试 decision 条目", project_id=proj["project_id"], kind="decision")
    store.write_fact(content="筛选测试 fact 条目", project_id=proj["project_id"], kind="fact")
    only_dec = store.search_facts("筛选测试", project_id=proj["project_id"], kind="decision")
    assert len(only_dec) == 1 and only_dec[0]["kind"] == "decision"


def test_search_default_excludes_expired(store, proj):
    res = store.write_fact(content="过期项搜索测试", project_id=proj["project_id"], ttl_days=1)
    store._conn.execute(
        "UPDATE memory_fact SET created_at='2020-01-01T00:00:00Z' WHERE fact_id=?", (res["fact_id"],)
    )
    store.expire_due()
    assert store.search_facts("过期项", project_id=proj["project_id"]) == []
    assert len(store.search_facts("过期项", project_id=proj["project_id"], include_expired=True)) == 1


def test_search_special_chars_does_not_crash(store, proj):
    store.write_fact(content="处理特殊字符查询", project_id=proj["project_id"])
    for q in ['"', "a AND b", "*", "()", "-", "NOT x"]:
        store.search_facts(q, project_id=proj["project_id"])


# --- projects & sessions -----------------------------------------------------


def test_project_counts(store, proj):
    store.write_fact(content="计数用记忆", project_id=proj["project_id"])
    counts = store.counts()
    assert counts["projects"] == 1
    assert counts["facts_active"] == 1


def test_session_digest_upsert(store, proj):
    store.upsert_session_digest(
        agent="claude-code",
        session_id="s1",
        project_id=proj["project_id"],
        cwd="/x",
        clean_title="实现登录",
        summary="加了 JWT",
        outcome="登录可用",
        next_steps="补测试",
        touched_files=["src/auth.py"],
        message_count=12,
    )
    sessions = store.list_sessions(proj["project_id"])
    assert len(sessions) == 1
    s = sessions[0]
    assert s["clean_title"] == "实现登录"
    assert s["touched_files"] == ["src/auth.py"]
    assert s["session_key"] == "claude-code:s1"

    # second write updates, does not duplicate
    store.upsert_session_digest(
        agent="claude-code",
        session_id="s1",
        project_id=proj["project_id"],
        clean_title="实现登录（已完成）",
        message_count=20,
    )
    assert len(store.list_sessions(proj["project_id"])) == 1
    assert store.list_sessions(proj["project_id"])[0]["clean_title"] == "实现登录（已完成）"


def test_session_digest_redacts(store, proj):
    store.upsert_session_digest(
        agent="codex",
        session_id="s2",
        project_id=proj["project_id"],
        clean_title="配置 key sk-abcdefghijklmnop9999",
        summary="手机 13900139000",
    )
    s = store.list_sessions(proj["project_id"])[0]
    assert "sk-abcdefghijklmnop" not in s["clean_title"]
    assert "13900139000" not in s["summary"]


def test_older_digest_not_overwritten(store, proj):
    store.upsert_session_digest(
        agent="zcode", session_id="s3", project_id=proj["project_id"],
        clean_title="新的标题", updated_at="2026-11-26T10:00:00Z",
    )
    store.upsert_session_digest(
        agent="zcode", session_id="s3", project_id=proj["project_id"],
        clean_title="旧的标题", updated_at="2026-01-01T10:00:00Z",
    )
    assert store.list_sessions(proj["project_id"])[0]["clean_title"] == "新的标题"


def test_reindex_fts(store, proj):
    store.write_fact(content="重建索引测试条目", project_id=proj["project_id"])
    store._conn.execute("DELETE FROM memory_fact_fts")
    assert store.search_facts("重建索引", project_id=proj["project_id"]) == []
    n = store.reindex_fts()
    assert n == 1
    assert len(store.search_facts("重建索引", project_id=proj["project_id"])) == 1


def test_concurrent_writes_are_serialised(store, proj):
    import threading

    errors = []

    def worker(i):
        try:
            # force_new: this test is about write serialisation, not dedupe
            store.write_fact(
                content=f"并发写入测试条目编号 {i} 内容差异 {i * 7}",
                project_id=proj["project_id"],
                force_new=True,
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.search_facts(project_id=proj["project_id"], status="active")) == 12