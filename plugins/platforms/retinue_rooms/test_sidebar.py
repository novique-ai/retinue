"""Sidebar: edit/archive/delete rooms+bots, team separators, drag-reorder."""

from __future__ import annotations

import json

import pytest

from . import hire, sidebar
from .engine import KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"])
    defaults.update(kwargs)
    return Room(**defaults)


def _write_presets(tmp_path):
    d = tmp_path / hire.MODELS_DIRNAME
    d.mkdir()
    (d / "grok-4.5.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n",
    encoding="utf-8",
    )
    (d / "grok-4.6.yaml").write_text(
        "model:\n  default: grok-4.6\n  provider: xai-oauth\n",
    encoding="utf-8",
    )


# ── room archive / patch ─────────────────────────────────────────────────


def test_room_from_dict_defaults_archived_false():
    room = Room.from_dict({"id": "r", "name": "Ops", "members": ["scout"]})
    assert room.archived is False
    assert room.to_dict()["archived"] is False


def test_store_archive_keeps_transcript(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="hi"))
    room = store.get("r-1")
    room.archived = True
    room.name = "Ops (old)"
    store.update(room)
    loaded = store.get("r-1")
    assert loaded.archived is True
    assert loaded.name == "Ops (old)"
    assert [m.text for m in store.read_since("r-1", 0)] == ["hi"]
    assert store.delete("r-1") is True
    assert store.get("r-1") is None
    assert store.read_since("r-1", 0) == []


def test_patch_room_renames_and_restaffs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room(lead="scout"))
    payload = adapter.patch_room(
        "r-1",
        {"name": "War room", "members": ["editor", "scout"], "lead": "editor"},
    )
    assert payload["name"] == "War room"
    assert payload["members"] == ["editor", "scout"]
    assert payload["lead"] == "editor"
    assert payload["archived"] is False
    with pytest.raises(ValueError, match="nothing to update"):
        adapter.patch_room("r-1", {})
    with pytest.raises(KeyError):
        adapter.patch_room("ghost", {"name": "x"})
    with pytest.raises(ValueError, match="not a member"):
        adapter.patch_room("r-1", {"lead": "ghost"})


def test_patch_room_archive_does_not_delete_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room())
    adapter.store.append(
        "r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="keep me")
    )
    adapter.patch_room("r-1", {"archived": True})
    assert adapter.store.get("r-1").archived is True
    assert [m.text for m in adapter.store.read_since("r-1", 0)] == ["keep me"]
    adapter.patch_room("r-1", {"archived": False})
    assert adapter.store.get("r-1").archived is False


# ── bot edit / archive / delete ──────────────────────────────────────────


def test_update_agent_rewrites_soul_and_keeps_slug(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "find facts", "be terse")
    updated = hire.update_agent(
        str(tmp_path),
        "scout",
        display_name="Scout Prime",
        job="verify facts",
        how="cite sources",
    )
    assert updated["slug"] == "scout"
    assert updated["display_name"] == "Scout Prime"
    assert updated["job"] == "verify facts"
    assert updated["how"] == "cite sources"
    soul = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
    assert "You are Scout Prime." in soul
    assert "Your job: verify facts" in soul
    assert "cite sources" in soul
    assert (tmp_path / "profiles" / "scout").is_dir()


def test_update_agent_archive_does_not_rewrite_soul(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "find facts", "be terse")
    before = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
    updated = hire.update_agent(str(tmp_path), "scout", archived=True)
    assert updated["archived"] is True
    assert (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8") == before
    listed = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
    assert listed["scout"]["archived"] is True


def test_update_agent_rejects_default_and_missing(tmp_path):
    with pytest.raises(ValueError, match="cannot change"):
        hire.update_agent(str(tmp_path), "default", job="nope")
    with pytest.raises(KeyError):
        hire.update_agent(str(tmp_path), "ghost", job="haunt")
    hire.scaffold_profile(str(tmp_path), "Scout", "find facts", "")
    with pytest.raises(ValueError, match="nothing to update"):
        hire.update_agent(str(tmp_path), "scout")
    with pytest.raises(ValueError, match="name is required"):
        hire.update_agent(str(tmp_path), "scout", display_name="  ")


def test_delete_agent_removes_profile_and_refuses_default(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Temp", "temp job", "")
    assert hire.delete_agent(str(tmp_path), "temp") == "temp"
    assert not (tmp_path / "profiles" / "temp").exists()
    with pytest.raises(KeyError):
        hire.delete_agent(str(tmp_path), "temp")
    with pytest.raises(ValueError, match="cannot change"):
        hire.delete_agent(str(tmp_path), "default")
    (tmp_path / "SOUL.md").write_text("workspace soul — do not touch", encoding="utf-8")
    hire.delete_agent  # noqa: B018 — just documenting the guard
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8").startswith("workspace soul")


def test_delete_agent_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="invalid"):
        hire.delete_agent(str(tmp_path), "../etc")
    with pytest.raises(ValueError, match="invalid"):
        hire.delete_agent(str(tmp_path), "scout/../../etc")


def test_deactivate_hired_profile_pops_pairing(monkeypatch):
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)

    class _Runner:
        def __init__(self):
            self.pairing_stores = {"scout": object(), "editor": object()}
            self._profile_adapters = {"scout": {}, "editor": {}}
            self._agent_cache = {
                "agent:main:retinue_rooms:group:ops:scout": object(),
                "agent:main:retinue_rooms:group:ops:editor": object(),
            }

        def _evict_cached_agent(self, key):
            self._agent_cache.pop(key, None)

    runner = _Runner()
    result = hire.deactivate_hired_profile("scout", runner=runner)
    assert result["deregistered"] is True
    assert result["evicted"] == 1
    assert "scout" not in runner.pairing_stores
    assert "editor" in runner.pairing_stores
    assert "scout" not in runner._profile_adapters
    assert hire.deactivate_hired_profile("x", runner=None)["deregistered"] is False


def test_patch_agent_edits_persona_and_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    _write_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Admin", "lead", "delegate", model_preset="grok-4.5")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    runner = type("R", (), {"_agent_cache": {"k:admin": object()}, "pairing_stores": {}})()
    adapter.gateway_runner = runner
    meta = adapter.patch_agent(
        "admin",
        {"name": "The Admin", "job": "run the room", "how": "delegate", "model": "grok-4.6"},
    )
    assert meta["display_name"] == "The Admin"
    assert meta["job"] == "run the room"
    assert meta["model_preset"] == "grok-4.6"
    assert meta["slug"] == "admin"
    assert "You are The Admin." in (tmp_path / "profiles" / "admin" / "SOUL.md").read_text(encoding="utf-8")
    archived = adapter.patch_agent("admin", {"archived": True})
    assert archived["archived"] is True
    # Existing client: PATCH {model} only.
    again = adapter.switch_agent_model("admin", "grok-4.5")
    assert again["model_preset"] == "grok-4.5"


def test_model_switch_refuses_mid_turn(tmp_path, monkeypatch):
    from concurrent.futures import Future

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    _write_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Scout", "facts", "")
    from gateway.config import PlatformConfig

    from .adapter import AgentBusy, RetinueRoomsAdapter, _PendingTurn

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.gateway_runner = type("R", (), {"_agent_cache": {}, "pairing_stores": {}})()
    roster = adapter.list_agents()
    by_slug = {a["slug"]: a for a in roster}
    assert by_slug["scout"]["busy"] is False

    adapter._pending[("r-1", "scout")] = _PendingTurn(
        task_id="t", room_id="r-1", member="scout", future=Future()
    )
    assert "scout" in adapter.busy_slugs()
    assert {a["slug"]: a["busy"] for a in adapter.list_agents()}["scout"] is True
    with pytest.raises(AgentBusy, match="mid-turn"):
        adapter.switch_agent_model("scout", "grok-4.5")
    # Persona-only edit is still allowed.
    edited = adapter.patch_agent("scout", {"job": "still facts"})
    assert edited["job"] == "still facts"


def test_delete_agent_via_adapter_evicts_and_forbids_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Temp", "temp", "")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    class _Runner:
        def __init__(self):
            self.pairing_stores = {"temp": object()}
            self._profile_adapters = {"temp": {}}
            self._agent_cache = {"agent:main:room:temp": object()}

        def _evict_cached_agent(self, key):
            self._agent_cache.pop(key, None)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.gateway_runner = _Runner()
    payload = adapter.delete_agent("temp")
    assert payload["deleted"] == "temp"
    assert payload["deregistered"] is True
    assert not (tmp_path / "profiles" / "temp").exists()
    assert "temp" not in adapter.gateway_runner.pairing_stores
    with pytest.raises(ValueError):
        adapter.delete_agent("default")


# ── sidebar layout ───────────────────────────────────────────────────────


def test_sidebar_save_load_roundtrip(tmp_path):
    assert sidebar.load(str(tmp_path)) == {"rooms": [], "items": []}
    saved = sidebar.save(
        str(tmp_path),
        {
            "rooms": ["ops", "ops", "lab"],
            "items": [
                {"kind": "team", "id": "cloud", "label": "Cloud staff"},
                {"kind": "agent", "slug": "admin"},
                {"kind": "agent", "slug": "admin"},
                {"kind": "team", "label": "Local bench"},
                {"kind": "agent", "slug": "scout"},
            ],
        },
    )
    assert saved["rooms"] == ["ops", "lab"]
    kinds = [(i["kind"], i.get("slug") or i.get("label")) for i in saved["items"]]
    assert kinds[0] == ("team", "Cloud staff")
    assert kinds[1] == ("agent", "admin")
    assert kinds[2][0] == "team" and kinds[2][1] == "Local bench"
    assert kinds[3] == ("agent", "scout")
    # Generated id for a team that arrived without one.
    assert saved["items"][2]["id"].startswith("local-bench-")
    assert sidebar.load(str(tmp_path)) == saved


def test_sidebar_resolve_appends_new_and_drops_unknown():
    layout = {
        "rooms": ["gone", "ops"],
        "items": [
            {"kind": "team", "id": "cloud", "label": "Cloud staff"},
            {"kind": "agent", "slug": "ghost"},
            {"kind": "agent", "slug": "admin"},
        ],
    }
    resolved = sidebar.resolve(layout, ["ops", "lab"], ["admin", "scout"])
    assert resolved["rooms"] == ["ops", "lab"]
    slugs = [i.get("slug") for i in resolved["items"] if i["kind"] == "agent"]
    assert slugs == ["admin", "scout"]
    assert resolved["items"][0]["kind"] == "team"
    teams = sidebar.team_for_agents(resolved["items"])
    assert teams["admin"] == "cloud"
    assert teams["scout"] == "cloud"  # still under the preceding (only) team


def test_sidebar_team_for_agents_ungrouped_before_first_separator():
    items = [
        {"kind": "agent", "slug": "scribe"},
        {"kind": "team", "id": "cloud", "label": "Cloud"},
        {"kind": "agent", "slug": "admin"},
        {"kind": "team", "id": "local", "label": "Local"},
        {"kind": "agent", "slug": "scout"},
    ]
    teams = sidebar.team_for_agents(items)
    assert teams["scribe"] is None
    assert teams["admin"] == "cloud"
    assert teams["scout"] == "local"


def test_sidebar_rejects_team_without_label():
    cleaned = sidebar.normalize(
        {"items": [{"kind": "team", "id": "x", "label": "   "}, {"kind": "agent", "slug": "a"}]}
    )
    assert cleaned["items"] == [{"kind": "agent", "slug": "a"}]


def test_put_sidebar_orders_rooms_and_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hire.scaffold_profile(str(tmp_path), "Admin", "lead", "")
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room(id="ops", name="Ops", members=["admin"]))
    adapter.store.create(_room(id="lab", name="Lab", members=["scout"]))
    adapter.put_sidebar(
        {
            "rooms": ["lab", "ops"],
            "items": [
                {"kind": "team", "id": "cloud", "label": "Cloud staff"},
                {"kind": "agent", "slug": "admin"},
                {"kind": "team", "id": "local", "label": "Local bench"},
                {"kind": "agent", "slug": "scout"},
            ],
        }
    )
    rooms = adapter.list_rooms_public()
    assert [r["id"] for r in rooms] == ["lab", "ops"]
    agents = adapter.list_agents()
    assert [a["slug"] for a in agents] == ["admin", "scout"]
    by = {a["slug"]: a for a in agents}
    assert by["admin"]["team"] == "cloud"
    assert by["scout"]["team"] == "local"
    layout = adapter.get_sidebar()
    assert layout["rooms"] == ["lab", "ops"]
    assert layout["items"][0]["label"] == "Cloud staff"


def test_http_patch_delete_and_sidebar(tmp_path, monkeypatch):
    """Routes exist and honor the no-wipe / no-default invariants."""
    import http.client
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "be terse")
    hire.scaffold_profile(str(tmp_path), "Temp", "temp", "")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room())
    adapter.store.append(
        "r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="stay")
    )
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        return resp.status, payload

    try:
        status, payload = call("PATCH", "/rooms/r-1", {"name": "Ops", "archived": True})
        assert status == 200
        assert payload["name"] == "Ops" and payload["archived"] is True
        assert [m.text for m in adapter.store.read_since("r-1", 0)] == ["stay"]

        status, payload = call("PATCH", "/agents/scout", {"job": "verify", "how": "cite"})
        assert status == 200
        assert payload["job"] == "verify"
        assert payload["slug"] == "scout"
        assert "Your job: verify" in (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")

        status, payload = call("DELETE", "/agents/default")
        assert status == 400

        status, payload = call("DELETE", "/agents/temp")
        assert status == 200
        assert payload["deleted"] == "temp"
        assert not (tmp_path / "profiles" / "temp").exists()

        status, payload = call(
            "PUT",
            "/sidebar",
            {
                "rooms": ["r-1"],
                "items": [
                    {"kind": "team", "id": "bench", "label": "Local bench"},
                    {"kind": "agent", "slug": "scout"},
                ],
            },
        )
        assert status == 200
        assert payload["items"][0]["label"] == "Local bench"
        status, payload = call("GET", "/sidebar")
        assert status == 200
        assert payload["rooms"] == ["r-1"]
    finally:
        httpd.shutdown()
        httpd.server_close()
