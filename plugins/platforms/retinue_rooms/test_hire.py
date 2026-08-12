"""Unit tests for the hire flow (no gateway required)."""

from __future__ import annotations

import json
import os

import pytest

from . import hire


def test_slugify():
    assert hire.slugify_name("Scout") == "scout"
    assert hire.slugify_name("Data Scout 2") == "data-scout-2"
    assert hire.slugify_name("  --Éditeur!  ") == "diteur"


def test_soul_template_contains_all_three_fields():
    soul = hire.soul_template("Scout", "find facts fast", "check sources; be terse")
    assert "You are Scout." in soul
    assert "Your job: find facts fast" in soul
    assert "check sources; be terse" in soul


def test_soul_template_pins_identity_against_engine_bleed():
    """Agents were introducing themselves with the engine's identity
    ("Claude Code" phrasing) instead of their persona — the SOUL must pin
    the persona name explicitly and name the engine as not-who-you-are."""
    soul = hire.soul_template("Scout", "find facts fast", "")
    assert "Identity: your name is Scout" in soul
    assert "never introduce or describe yourself by an engine name" in soul


def test_scaffold_creates_profile(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: claude-haiku-4-5\n  provider: anthropic\nagent:\n  tool_choice: auto\n"
    )
    (tmp_path / ".env").write_text("PROVIDER_KEY=abc\n")
    meta = hire.scaffold_profile(str(tmp_path), "Data Scout", "research things", "be terse")
    assert meta["slug"] == "data-scout"

    pdir = tmp_path / "profiles" / "data-scout"
    assert "You are Data Scout." in (pdir / "SOUL.md").read_text()
    config = (pdir / "config.yaml").read_text()
    assert "claude-haiku-4-5" in config and "provider: anthropic" in config
    assert (pdir / ".env").read_text() == "PROVIDER_KEY=abc\n"
    saved = json.loads((pdir / hire.AGENT_META_FILENAME).read_text())
    assert saved["job"] == "research things"


def test_scaffold_without_root_config_uses_fallback_model(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Solo", "do things", "")
    config = (tmp_path / "profiles" / "solo" / "config.yaml").read_text()
    assert "model:" in config and "tool_choice: auto" in config
    assert not (tmp_path / "profiles" / "solo" / ".env").exists()


def test_scaffold_rejects_duplicates_and_bad_input(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "job", "")
    with pytest.raises(FileExistsError):
        hire.scaffold_profile(str(tmp_path), "SCOUT", "job", "")
    with pytest.raises(ValueError):
        hire.scaffold_profile(str(tmp_path), "", "job", "")
    with pytest.raises(ValueError):
        hire.scaffold_profile(str(tmp_path), "Scout2", "", "")
    with pytest.raises(ValueError):
        hire.scaffold_profile(str(tmp_path), "default", "job", "")


def test_list_agents_mixes_hired_and_handmade(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    os.makedirs(tmp_path / "profiles" / "handmade")
    (tmp_path / "profiles" / "handmade" / "SOUL.md").write_text("You are handmade.")
    agents = hire.list_agents(str(tmp_path))
    by_slug = {a["slug"]: a for a in agents}
    assert by_slug["scout"]["job"] == "research"
    assert by_slug["handmade"]["display_name"] == "handmade"
    assert by_slug["handmade"]["has_soul"] is True
