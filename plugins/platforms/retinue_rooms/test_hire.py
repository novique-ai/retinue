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


def test_soul_template_teaches_at_handoff():
    soul = hire.soul_template("Sheila", "make images", "")
    assert "say so briefly" not in soul
    assert "@ them by the name the user would type" in soul
    assert "then stop" in soul


def test_soul_template_teaches_lead_itinerary():
    soul = hire.soul_template("Dave", "implement", "")
    assert "you own the itinerary" in soul.lower()
    assert "```itinerary" in soul
    assert "If you are not the lead, do not write that block." in soul


def test_soul_template_teaches_work_stays_in_the_room():
    soul = hire.soul_template("Sheila", "make images", "")
    assert "Write files under /workspace" in soul
    assert "never go silent" in soul


def test_soul_template_pins_identity_against_engine_bleed():
    """Agents were introducing themselves with the engine's identity
    ("Claude Code" phrasing) instead of their persona — the SOUL must pin
    the persona name explicitly and name the engine as not-who-you-are."""
    soul = hire.soul_template("Scout", "find facts fast", "")
    assert "Identity: your name is Scout" in soul
    assert "never introduce or describe yourself by an engine name" in soul


def test_scaffold_creates_profile(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: claude-haiku-4-5\n  provider: anthropic\nagent:\n  tool_choice: auto\n",
    encoding="utf-8",
    )
    (tmp_path / ".env").write_text("PROVIDER_KEY=abc\n", encoding="utf-8")
    meta = hire.scaffold_profile(str(tmp_path), "Data Scout", "research things", "be terse")
    assert meta["slug"] == "data-scout"

    pdir = tmp_path / "profiles" / "data-scout"
    assert "You are Data Scout." in (pdir / "SOUL.md").read_text(encoding="utf-8")
    config = (pdir / "config.yaml").read_text(encoding="utf-8")
    assert "claude-haiku-4-5" in config and "provider: anthropic" in config
    assert (pdir / ".env").read_text(encoding="utf-8") == "PROVIDER_KEY=abc\n"
    saved = json.loads((pdir / hire.AGENT_META_FILENAME).read_text(encoding="utf-8"))
    assert saved["job"] == "research things"
    assert saved["archived"] is False


def test_scaffold_without_root_config_uses_fallback_model(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Solo", "do things", "")
    config = (tmp_path / "profiles" / "solo" / "config.yaml").read_text(encoding="utf-8")
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
    (tmp_path / "profiles" / "handmade" / "SOUL.md").write_text("You are handmade.", encoding="utf-8")
    agents = hire.list_agents(str(tmp_path))
    by_slug = {a["slug"]: a for a in agents}
    assert by_slug["scout"]["job"] == "research"
    assert by_slug["handmade"]["display_name"] == "handmade"
    assert by_slug["handmade"]["has_soul"] is True
    assert "local_llm" in by_slug["scout"]
    assert "turn_timeout" in by_slug["scout"]


# ── model presets (per-hire model selection) ─────────────────────────────


def _write_presets(tmp_path):
    d = tmp_path / hire.MODELS_DIRNAME
    d.mkdir()
    (d / "local.yaml").write_text(
        "model:\n  provider: custom\n  model: local/auto\n  base_url: http://llm:8091/v1\n  api_key: \"none\"\n",
    encoding="utf-8",
    )
    (d / "grok.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n",
    encoding="utf-8",
    )
    (d / "grok-4.5.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n",
    encoding="utf-8",
    )
    (d / "grok-4.6.yaml").write_text(
        "model:\n  default: grok-4.6\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n",
    encoding="utf-8",
    )
    (d / "broken.yaml").write_text("not_a_model_block: true\n", encoding="utf-8")
    (d / "README.txt").write_text("ignored, wrong extension\n", encoding="utf-8")

def test_list_model_presets(tmp_path):
    assert hire.list_model_presets(str(tmp_path)) == []  # no dir yet
    _write_presets(tmp_path)
    presets = hire.list_model_presets(str(tmp_path))
    assert [p["name"] for p in presets] == ["grok-4.5", "grok-4.6", "local"]
    by_name = {p["name"]: p for p in presets}
    assert by_name["grok-4.5"]["summary"] == "xai-oauth · grok-4.5"
    assert by_name["grok-4.6"]["summary"] == "xai-oauth · grok-4.6"
    assert by_name["grok-4.6"]["local"] is False
    assert by_name["local"]["summary"] == "custom · local/auto"
    assert by_name["local"]["local"] is True
    aliases = hire.list_model_presets(str(tmp_path), include_aliases=True)
    assert [p["name"] for p in aliases] == ["grok", "grok-4.5", "grok-4.6", "local"]


def test_scaffold_with_model_preset(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: root-model\n  provider: anthropic\n", encoding="utf-8")
    _write_presets(tmp_path)
    meta = hire.scaffold_profile(str(tmp_path), "Boss", "lead", "", model_preset="grok-4.6")
    assert meta["model_preset"] == "grok-4.6"
    config = (tmp_path / "profiles" / "boss" / "config.yaml").read_text(encoding="utf-8")
    assert "provider: xai-oauth" in config and "grok-4.6" in config
    assert "root-model" not in config
    # Legacy bucket name still resolves so old clients keep working.
    hire.scaffold_profile(str(tmp_path), "Herald", "announce", "", model_preset="grok")
    assert "grok-4.5" in (tmp_path / "profiles" / "herald" / "config.yaml").read_text(encoding="utf-8")


def test_scaffold_unknown_preset_creates_nothing(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: root-model\n  provider: anthropic\n", encoding="utf-8")
    _write_presets(tmp_path)
    with pytest.raises(ValueError, match="unknown model preset 'nope'"):
        hire.scaffold_profile(str(tmp_path), "Ghost", "haunt", "", model_preset="nope")
    assert not (tmp_path / "profiles" / "ghost").exists()
    with pytest.raises(ValueError, match="no 'model:' block"):
        hire.scaffold_profile(str(tmp_path), "Ghost", "haunt", "", model_preset="broken")


def test_model_block_is_local_detects_custom_lan_and_local_alias():
    local = (
        "model:\n  provider: custom\n  model: local/auto\n"
        "  base_url: http://10.44.0.13:8091/v1\n"
    )
    grok = "model:\n  default: grok-4.5\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n"
    ollama = "model:\n  provider: ollama\n  model: llama3\n"
    assert hire.model_block_is_local(local) is True
    assert hire.model_block_is_local(grok) is False
    assert hire.model_block_is_local(ollama) is True
    assert hire.model_block_is_local("model:\n  provider: anthropic\n  default: claude-haiku-4-5\n") is False


def test_profile_uses_local_llm_reads_profile_then_root(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: custom\n  model: local/auto\n  base_url: http://127.0.0.1:8091/v1\n",
    encoding="utf-8",
    )
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    hire.scaffold_profile(str(tmp_path), "Boss", "lead", "")
    # overwrite boss onto grok after scaffold
    (tmp_path / "profiles" / "boss" / "config.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n",
    encoding="utf-8",
    )
    assert hire.profile_uses_local_llm(str(tmp_path), "scout") is True
    assert hire.profile_uses_local_llm(str(tmp_path), "boss") is False
    assert hire.profile_uses_local_llm(str(tmp_path), "default") is True
    assert hire.profile_uses_local_llm(str(tmp_path), "missing") is True  # fail-safe


def test_turn_timeout_for_is_longer_for_local(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_ROOMS_TURN_TIMEOUT", raising=False)
    monkeypatch.delenv("RETINUE_ROOMS_LOCAL_TURN_TIMEOUT", raising=False)
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: custom\n  model: local/auto\n  base_url: http://10.0.0.2:8091/v1\n",
    encoding="utf-8",
    )
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    (tmp_path / "profiles" / "scout" / "config.yaml").write_text(
        "model:\n  provider: custom\n  model: local/auto\n  base_url: http://10.0.0.2:8091/v1\n",
    encoding="utf-8",
    )
    (tmp_path / "profiles" / "boss").mkdir()
    (tmp_path / "profiles" / "boss" / "config.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n",
    encoding="utf-8",
    )
    local_t = hire.turn_timeout_for(str(tmp_path), "scout")
    cloud_t = hire.turn_timeout_for(str(tmp_path), "boss")
    assert cloud_t == 300
    assert local_t >= 1800
    assert local_t > cloud_t


def test_ensure_bundled_cloud_presets_promotes_legacy_grok(tmp_path):
    d = tmp_path / hire.MODELS_DIRNAME
    d.mkdir()
    (d / "grok.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n  # operator pin\n",
    encoding="utf-8",
    )
    written = hire.ensure_bundled_cloud_presets(str(tmp_path))
    assert "grok-4.5" in written
    assert "grok-4.6" in written
    assert "operator pin" in (d / "grok-4.5.yaml").read_text(encoding="utf-8")
    assert "grok-4.6" in (d / "grok-4.6.yaml").read_text(encoding="utf-8")
    # never overwrite a live pin
    (d / "grok-4.6.yaml").write_text("model:\n  default: grok-4.6\n  provider: xai-oauth\n  # leave me\n", encoding="utf-8")
    again = hire.ensure_bundled_cloud_presets(str(tmp_path))
    assert again == []
    assert "leave me" in (d / "grok-4.6.yaml").read_text(encoding="utf-8")


def test_apply_model_preset_rewrites_only_the_model_block(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_ROOMS_TURN_TIMEOUT", raising=False)
    monkeypatch.delenv("RETINUE_ROOMS_LOCAL_TURN_TIMEOUT", raising=False)
    _write_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Admin", "lead", "delegate", model_preset="grok-4.5")
    pdir = tmp_path / "profiles" / "admin"
    (pdir / "config.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\nagent:\n  tool_choice: auto\n",
    encoding="utf-8",
    )
    updated = hire.apply_model_preset(str(tmp_path), "admin", "grok-4.6")
    assert updated["slug"] == "admin"
    assert updated["model_preset"] == "grok-4.6"
    assert updated["local_llm"] is False
    assert updated["turn_timeout"] == 300
    config = (pdir / "config.yaml").read_text(encoding="utf-8")
    assert "default: grok-4.6" in config
    assert "tool_choice: auto" in config
    assert "grok-4.5" not in config
    saved = json.loads((pdir / hire.AGENT_META_FILENAME).read_text(encoding="utf-8"))
    assert saved["model_preset"] == "grok-4.6"
    assert saved["job"] == "lead"


def test_apply_model_preset_rejects_unknown_and_missing(tmp_path):
    _write_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Admin", "lead", "")
    with pytest.raises(ValueError, match="unknown model preset"):
        hire.apply_model_preset(str(tmp_path), "admin", "grok-9.9")
    with pytest.raises(KeyError):
        hire.apply_model_preset(str(tmp_path), "ghost", "grok-4.6")
    with pytest.raises(ValueError, match="required"):
        hire.apply_model_preset(str(tmp_path), "admin", "")


def test_list_agents_infers_versioned_preset(tmp_path):
    _write_presets(tmp_path)
    (tmp_path / "profiles" / "admin").mkdir(parents=True)
    (tmp_path / "profiles" / "admin" / "config.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n",
    encoding="utf-8",
    )
    agents = hire.list_agents(str(tmp_path))
    admin = {a["slug"]: a for a in agents}["admin"]
    assert admin["model_preset"] == "grok-4.5"
    assert admin["model_summary"] == "xai-oauth · grok-4.5"
    assert admin["local_llm"] is False


def test_list_agents_promotes_legacy_grok_preset_label(tmp_path):
    _write_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Envoy", "draft", "", model_preset="grok")
    envoy = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}["envoy"]
    assert envoy["model_preset"] == "grok-4.5"


def test_evict_profile_agent_cache_matches_thread_id_keys():
    class _Runner:
        def __init__(self):
            self._agent_cache = {
                "agent:main:retinue_rooms:group:room:admin": object(),
                "agent:main:retinue_rooms:group:room:envoy": object(),
                "agent:admin:retinue_rooms:group:room:x": object(),
            }
            self.evicted = []

        def _evict_cached_agent(self, key):
            self.evicted.append(key)
            self._agent_cache.pop(key, None)

    runner = _Runner()
    n = hire.evict_profile_agent_cache(runner, "admin")
    assert n == 2
    assert "envoy" not in "".join(runner.evicted)
    assert all("admin" in k for k in runner.evicted)
    assert hire.evict_profile_agent_cache(None, "admin") == 0


def test_switch_agent_model_evicts_live_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    _write_presets(tmp_path)
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: custom\n", encoding="utf-8")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    runner = _FakeRunner()
    runner._agent_cache = {
        "agent:main:retinue_rooms:group:ops:admin": object(),
        "agent:main:retinue_rooms:group:ops:scout": object(),
    }
    adapter.gateway_runner = runner
    hire.scaffold_profile(str(tmp_path), "Admin", "lead", "", model_preset="grok-4.5")
    meta = adapter.switch_agent_model("admin", "grok-4.6")
    assert meta["model_preset"] == "grok-4.6"
    assert meta["cache_evicted"] == 1
    assert "admin" not in "".join(runner._agent_cache)
    assert any("scout" in k for k in runner._agent_cache)


def test_scaffold_seeds_root_auth_store(tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: anthropic\n", encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"providers": {"xai-oauth": {}}}', encoding="utf-8")
    hire.scaffold_profile(str(tmp_path), "Keys", "hold credentials", "")
    seeded = tmp_path / "profiles" / "keys" / "auth.json"
    assert seeded.read_text(encoding="utf-8") == '{"providers": {"xai-oauth": {}}}'
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
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: custom\n", encoding="utf-8")
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
    (tmp_path / "config.yaml").write_text("model:\n  default: m\n  provider: custom\n", encoding="utf-8")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    meta = adapter.hire_agent("Solo", "do things", "")
    assert meta["online"] is False
    assert "restart" not in meta["activation"].lower()


def test_send_resolves_only_the_matching_member():
    """Parallel speakers share a chat_id; notify must not steal the sibling."""
    import asyncio
    from concurrent.futures import Future

    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _PendingTurn

    adapter = RetinueRoomsAdapter(PlatformConfig())
    scout_f, editor_f = Future(), Future()
    adapter._pending[("r", "scout")] = _PendingTurn("t1", "r", "scout", scout_f)
    adapter._pending[("r", "editor")] = _PendingTurn("t2", "r", "editor", editor_f)
    asyncio.run(
        adapter.send(
            "r", "from scout", metadata={"notify": True, "retinue_member": "scout"}
        )
    )
    assert scout_f.result() == (True, "from scout")
    assert not editor_f.done()


def test_member_thread_id_splits_shared_room_session_keys():
    """BasePlatformAdapter keys sessions without the multiplex profile
    namespace. Two members in one room must still get distinct keys or
    they share _active_sessions and clobber each other's model."""
    from gateway.config import PlatformConfig
    from gateway.session import build_session_key

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    scout = adapter.build_source(chat_id="room-1", chat_type="group", thread_id="scout")
    editor = adapter.build_source(chat_id="room-1", chat_type="group", thread_id="editor")
    ks = build_session_key(scout, group_sessions_per_user=False)
    ke = build_session_key(editor, group_sessions_per_user=False)
    assert ks != ke
    assert "scout" in ks and "editor" in ke


def test_send_uses_turn_contextvar_when_gateway_metadata_has_no_member():
    """The gateway's notify metadata is thread_meta + notify only.
    Member identity has to ride a ContextVar set around handle_message."""
    import asyncio
    from concurrent.futures import Future

    from gateway.config import PlatformConfig

    from . import adapter as adapter_mod
    from .adapter import RetinueRoomsAdapter, _PendingTurn

    rooms = RetinueRoomsAdapter(PlatformConfig())
    scout_f, editor_f = Future(), Future()
    rooms._pending[("r", "scout")] = _PendingTurn("t1", "r", "scout", scout_f)
    rooms._pending[("r", "editor")] = _PendingTurn("t2", "r", "editor", editor_f)

    async def fire():
        token = adapter_mod._turn_member.set("editor")
        try:
            await rooms.send("r", "from editor", metadata={"notify": True})
        finally:
            adapter_mod._turn_member.reset(token)

    asyncio.run(fire())
    assert editor_f.result() == (True, "from editor")
    assert not scout_f.done()


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
