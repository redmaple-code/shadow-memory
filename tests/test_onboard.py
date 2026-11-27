"""Tests for onboarding: detection, dry-run, backups, idempotency, uninstall."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shadow_memory import onboard
from shadow_memory.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "memory.sqlite")
    yield s
    s.close()


@pytest.fixture()
def home(tmp_path):
    """A fake HOME that looks like a machine with all four agents installed."""
    h = tmp_path / "home"
    for rel in (
        ".claude/CLAUDE.md",
        ".codex/AGENTS.md",
        ".zcode/AGENTS.md",
        ".gemini/GEMINI.md",
    ):
        p = h / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel} 的用户自有内容\n\n原有规则一。\n", encoding="utf-8")
    return h


def _block_count(text: str) -> int:
    return text.count(onboard.MARKER_START)


# --- detection ---------------------------------------------------------------


def test_inspect_reports_four_agents(store, home):
    agents = onboard.inspect_agents(store, home)
    assert [a["agent"] for a in agents] == ["claude-code", "codex", "zcode", "antigravity"]
    for a in agents:
        assert a["installed"] is True
        assert a["file_exists"] is True
        assert a["configured"] is False
        assert a["sessions_count"] == 0
        assert Path(a["path"]).exists()


def test_inspect_empty_home(tmp_path, store):
    empty = tmp_path / "empty"
    empty.mkdir()
    agents = onboard.inspect_agents(store, empty)
    assert all(not a["installed"] for a in agents)
    assert all(not a["file_exists"] for a in agents)


def test_inspect_reports_session_counts(store, home):
    store.upsert_session_digest(agent="codex", session_id="s1", clean_title="t")
    store.upsert_session_digest(agent="codex", session_id="s2", clean_title="t2")
    by_agent = {a["agent"]: a for a in onboard.inspect_agents(store, home)}
    assert by_agent["codex"]["sessions_count"] == 2
    assert by_agent["zcode"]["sessions_count"] == 0


# --- init: dry-run -----------------------------------------------------------


def test_init_dry_run_touches_nothing(store, home):
    before = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.md")}
    result = onboard.apply_init(dry_run=True, home=home)
    assert result["dry_run"] is True
    assert result["changed"] == 4
    for path, content in before.items():
        assert path.read_text(encoding="utf-8") == content
    # a preview was produced for every agent
    for r in result["results"]:
        assert "preview" in r
        assert onboard.MARKER_START in r["preview"]


def test_init_dry_run_creates_no_backup(store, home):
    onboard.apply_init(dry_run=True, home=home)
    assert not list(home.rglob("*.bak.*"))


# --- init: apply -------------------------------------------------------------


def test_init_apply_injects_block(store, home):
    result = onboard.apply_init(dry_run=False, home=home)
    assert result["changed"] == 4
    for r in result["results"]:
        text = Path(r["path"]).read_text(encoding="utf-8")
        assert _block_count(text) == 1
        assert onboard.MARKER_END in text
        # user content is preserved
        assert "原有规则一。" in text
        assert r["backup"] is not None


def test_init_apply_creates_backup_file(store, home):
    onboard.apply_init(dry_run=False, home=home)
    claude = home / ".claude" / "CLAUDE.md"
    backups = list((home / ".claude").glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1
    assert "原有规则一。" in backups[0].read_text(encoding="utf-8")
    assert onboard.MARKER_START not in backups[0].read_text(encoding="utf-8")


def test_init_is_idempotent(store, home):
    onboard.apply_init(dry_run=False, home=home)
    first = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.md")}
    second = onboard.apply_init(dry_run=False, home=home)
    assert second["changed"] == 0
    for path, content in first.items():
        assert path.read_text(encoding="utf-8") == content
        assert _block_count(content) == 1
    # no extra backups on a no-op run
    assert len(list((home / ".claude").glob("CLAUDE.md.bak.*"))) == 1


def test_init_block_is_four_lines(home):
    block = onboard.block_text()
    inner = block.split(onboard.MARKER_START)[1].split(onboard.MARKER_END)[0]
    lines = [l for l in inner.splitlines() if l.strip()]
    assert len(lines) == 4
    assert lines[0].startswith("## Shadow Memory")
    assert "smem write" in block and "smem forget" in block


def test_init_skips_uninstalled_agent(tmp_path, store):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = onboard.apply_init(dry_run=False, home=empty)
    assert result["changed"] == 0
    for r in result["results"]:
        assert r["action"].startswith("skipped")
    assert not (empty / ".claude").exists()


def test_init_create_missing_opt_in(tmp_path, store):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = onboard.apply_init(dry_run=False, create_missing=True, home=empty)
    assert result["changed"] == 4
    assert (empty / ".claude" / "CLAUDE.md").exists()
    assert (empty / ".gemini" / "GEMINI.md").exists()


def test_init_single_agent_selection(store, home):
    result = onboard.apply_init(dry_run=False, home=home, agents=["codex"])
    assert result["changed"] == 1
    assert onboard.MARKER_START in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert onboard.MARKER_START not in (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")


def test_init_unknown_agent_reported(store, home):
    result = onboard.apply_init(dry_run=True, home=home, agents=["nope"])
    assert result["results"][0]["action"] == "unknown-agent"


def test_second_init_keeps_one_backup_per_change(store, home):
    onboard.apply_init(dry_run=False, home=home, agents=["zcode"])
    onboard.apply_init(dry_run=False, home=home, agents=["zcode"])
    assert len(list((home / ".zcode").glob("AGENTS.md.bak.*"))) == 1


# --- uninstall ---------------------------------------------------------------


def test_uninstall_dry_run_touches_nothing(store, home):
    onboard.apply_init(dry_run=False, home=home)
    before = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.md")}
    result = onboard.apply_uninstall(dry_run=True, home=home)
    assert result["changed"] == 4
    for path, content in before.items():
        assert path.read_text(encoding="utf-8") == content


def test_uninstall_removes_block_keeps_user_content(store, home):
    onboard.apply_init(dry_run=False, home=home)
    result = onboard.apply_uninstall(dry_run=False, home=home)
    assert result["changed"] == 4
    for r in result["results"]:
        assert r["action"] == "remove-block"
        text = Path(r["path"]).read_text(encoding="utf-8")
        assert onboard.MARKER_START not in text
        assert "原有规则一。" in text
        # no leftover scaffolding: no triple blank lines, no dangling markers
        assert "\n\n\n" not in text
        assert text.lstrip().startswith("#")


def test_uninstall_deletes_file_we_created(tmp_path, store):
    empty = tmp_path / "fresh"
    empty.mkdir()
    onboard.apply_init(dry_run=False, create_missing=True, home=empty)
    assert (empty / ".codex" / "AGENTS.md").exists()
    result = onboard.apply_uninstall(dry_run=False, home=empty)
    assert all(r["action"] == "delete-file" for r in result["results"])
    assert not (empty / ".codex" / "AGENTS.md").exists()
    assert not (empty / ".claude" / "CLAUDE.md").exists()


def test_uninstall_is_idempotent(store, home):
    onboard.apply_init(dry_run=False, home=home)
    onboard.apply_uninstall(dry_run=False, home=home)
    second = onboard.apply_uninstall(dry_run=False, home=home)
    assert second["changed"] == 0
    for r in second["results"]:
        assert r["action"].startswith("skipped")


def test_uninstall_without_config_file(store, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = onboard.apply_uninstall(dry_run=False, home=empty)
    assert result["changed"] == 0


def test_init_uninstall_roundtrip_restores_original(store, home):
    originals = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.md")}
    onboard.apply_init(dry_run=False, home=home)
    onboard.apply_uninstall(dry_run=False, home=home)
    for path, content in originals.items():
        assert path.read_text(encoding="utf-8").strip() == content.strip()


def test_backup_names_are_unique_within_same_second(store, home):
    p = home / ".codex" / "AGENTS.md"
    first = onboard._backup(p)
    second = onboard._backup(p)
    assert first != second
    assert Path(first).exists() and Path(second).exists()


def test_init_does_not_write_outside_home(store, home):
    """Guards the hard requirement: never touch the real user config."""
    onboard.apply_init(dry_run=False, home=home)
    real = Path.home() / ".claude" / "CLAUDE.md"
    # our fake home lives under tmp_path; the real file must be untouched by us
    assert str(home) != str(Path.home())
    if real.exists():
        assert onboard.MARKER_START not in real.read_text(encoding="utf-8", errors="replace")