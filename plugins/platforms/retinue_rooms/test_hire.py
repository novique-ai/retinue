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


# ── model presets (per-hire model selection) ─────────────────────────────


def _write_presets(tmp_path):
    d = tmp_path / hire.MODELS_DIRNAME
    d.mkdir()
    (d / "local.yaml").write_text(
        "model:\n  provider: custom\n  model: local/auto\n  base_url: http://llm:8091/v1\n  api_key: \"none\"\n"
    )
    (d / "grok.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n"
    )
    (d / "broken.yaml").write_text("not_a_model_block: true\n")
    (d / "README.txt").write_text("ignored, wrong extension\n")


def test_list_model_presets(tmp_path):
    assert hire.list_model_presets(str(tmp_path)) == []  # no dir yet
    _write_presets(tmp_path)
    presets = hire.list_model_presets(str(tmp_path))
    assert [p["name"] for p in presets] == ["grok", "local"]
    by_name = {p["name"]: p["summary"] for p in presets}
    assert by_name["grok"] == "xai-oauth · grok-4.5"
    assert by_name["local"] == "custom · local/auto"


def test_scaffold_with_model_preset(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: root-model\n  provider: anthropic\n")
    _write_presets(tmp_path)
    meta = hire.scaffold_profile(str(tmp_path), "Boss", "lead", "", model_preset="grok")
    assert meta["model_preset"] == "grok"
    config = (tmp_path / "profiles" / "boss" / "config.yaml").read_text()
    assert "provider: xai-oauth" in config and "grok-4.5" in config
    assert "root-model" not in config


def test_scaffold_unknown_preset_creates_nothing(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: root-model\n  provider: anthropic\n")
    _write_presets(tmp_path)
    with pytest.raises(ValueError, match="unknown model preset 'nope'"):
        hire.scaffold_profile(str(tmp_path), "Ghost", "haunt", "", model_preset="nope")
    assert not (tmp_path / "profiles" / "ghost").exists()
    with pytest.raises(ValueError, match="no 'model:' block"):
        hire.scaffold_profile(str(tmp_path), "Ghost", "haunt", "", model_preset="broken")


def test_scaffold_seeds_root_auth_store(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: anthropic\n")
    (tmp_path / "auth.json").write_text('{"providers": {"xai-oauth": {}}}')
    hire.scaffold_profile(str(tmp_path), "Keys", "hold credentials", "")
    seeded = tmp_path / "profiles" / "keys" / "auth.json"
    assert seeded.read_text() == '{"providers": {"xai-oauth": {}}}'
    assert (seeded.stat().st_mode & 0o777) == 0o600


# ── hot-register a hire into a live multiplexer (no gateway restart) ─────


class _FakeRunner:
    def __init__(self):
        self.pairing_stores = {}
        self._profile_adapters = {"scout": {}}
        self.busy = []

    def _snapshot_profile_busy_modes(self, name, config):
        self.busy.append((name, config))


def test_activate_hired_profile_without_runner_is_deferred():
    result = hire.activate_hired_profile("herald", runner=None)
    assert result["online"] is False
    assert "restart" not in result["activation"].lower()
    assert "gateway" in result["activation"].lower()


def test_activate_hired_profile_registers_on_live_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    recorded = []
    monkeypatch.setattr(
        "gateway.status.write_runtime_status",
        lambda **kw: recorded.append(kw),
    )
    runner = _FakeRunner()
    result = hire.activate_hired_profile("herald", runner=runner)
    assert result["online"] is True
    assert result["activation"] == "online"
    assert "herald" in runner.pairing_stores
    assert runner._profile_adapters["herald"] == {}
    assert runner.busy == [("herald", {})]
    assert recorded and "herald" in recorded[-1]["served_profiles"]


def test_activate_hired_profile_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    runner = _FakeRunner()
    first = runner.pairing_stores["herald"] = object()
    assert hire.activate_hired_profile("herald", runner=runner)["online"] is True
    assert runner.pairing_stores["herald"] is first


def test_hire_agent_hot_registers_without_restart(tmp_path, monkeypatch):
    """The P2 known limit: POST /agents used to tell the user to restart.
    A live runner must take the new profile immediately."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: custom\n")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.gateway_runner = _FakeRunner()
    meta = adapter.hire_agent("Herald", "announce things", "be brief")
    assert meta["slug"] == "herald"
    assert meta["online"] is True
    assert meta["activation"] == "online"
    assert "restart" not in meta["activation"].lower()
    assert "herald" in adapter.gateway_runner.pairing_stores
    assert (tmp_path / "profiles" / "herald" / "SOUL.md").is_file()


def test_hire_agent_without_runner_does_not_demand_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: custom\n")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    meta = adapter.hire_agent("Solo", "do things", "")
    assert meta["online"] is False
    assert "restart" not in meta["activation"].lower()


def test_connect_rescan_picks_up_disk_profiles(tmp_path, monkeypatch):
    """Profiles hired before hot-register (or while the gateway was down)
    must be registered on connect — class-level, not just the next hire."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Janitor", "tidy up", "")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.gateway_runner = _FakeRunner()
    adapter._rescan_disk_profiles()
    assert "janitor" in adapter.gateway_runner.pairing_stores
