"""Projects sit above rooms: create/patch/delete, room.project_id, Unfiled."""

from __future__ import annotations

import pytest

from . import projects
from .engine import Room
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout"])
    defaults.update(kwargs)
    return Room(**defaults)


# ── projects.py (store layer) ────────────────────────────────────────────


def test_projects_save_load_roundtrip(tmp_path):
    assert projects.load(str(tmp_path)) == {"projects": [], "order": []}
    saved = projects.save(
        str(tmp_path),
        {
            "projects": [
                {"id": "ops", "name": "Ops", "archived": False},
                {"id": "ops", "name": "Ops dup", "archived": False},
                {"id": "lab", "name": "Lab", "archived": True},
            ],
            "order": ["lab", "ops", "ghost"],
        },
    )
    assert [p["id"] for p in saved["projects"]] == ["lab", "ops"]
    assert saved["order"] == ["lab", "ops"]
    assert projects.load(str(tmp_path)) == saved


def test_projects_normalize_rejects_blank_and_dedupes():
    cleaned = projects.normalize(
        {
            "projects": [
                {"id": "x", "name": "  "},
                {"id": "", "name": "No id"},
                {"id": "y", "name": "Kept"},
            ]
        }
    )
    assert cleaned["projects"] == [{"id": "y", "name": "Kept", "archived": False}]


def test_new_project_id_dedupes_against_existing():
    first = projects.new_project_id("Ops Room")
    assert first == "ops-room"
    second = projects.new_project_id("Ops Room", existing=[first])
    assert second != first
    assert second.startswith("ops-room-")


def test_create_patch_delete_project(tmp_path):
    home = str(tmp_path)
    created = projects.create_project(home, "  Launch  ")
    assert created["name"] == "Launch"
    assert created["archived"] is False
    pid = created["id"]

    patched = projects.patch_project(home, pid, {"name": "Launch v2", "archived": True})
    assert patched["name"] == "Launch v2"
    assert patched["archived"] is True
    assert projects.load(home)["projects"][0]["name"] == "Launch v2"

    with pytest.raises(ValueError, match="nothing to update"):
        projects.patch_project(home, pid, {})
    with pytest.raises(KeyError):
        projects.patch_project(home, "ghost", {"name": "x"})
    with pytest.raises(ValueError, match="name is required"):
        projects.create_project(home, "   ")

    assert projects.delete_project(home, pid) is True
    assert projects.load(home)["projects"] == []
    assert projects.delete_project(home, pid) is False


def test_reorder_persists_and_drops_unknowns(tmp_path):
    home = str(tmp_path)
    a = projects.create_project(home, "Alpha")
    b = projects.create_project(home, "Beta")
    c = projects.create_project(home, "Gamma")
    assert projects.load(home)["order"] == [a["id"], b["id"], c["id"]]

    saved = projects.reorder(home, [c["id"], a["id"], "ghost", b["id"]])
    assert saved["order"] == [c["id"], a["id"], b["id"]]
    assert [p["id"] for p in saved["projects"]] == [c["id"], a["id"], b["id"]]
    assert projects.load(home)["order"] == [c["id"], a["id"], b["id"]]


def test_reorder_keeps_archived_in_saved_slot(tmp_path):
    home = str(tmp_path)
    a = projects.create_project(home, "Alpha")
    b = projects.create_project(home, "Beta")
    c = projects.create_project(home, "Gamma")
    projects.patch_project(home, b["id"], {"archived": True})
    saved = projects.reorder(home, [c["id"], b["id"], a["id"]])
    assert saved["order"] == [c["id"], b["id"], a["id"]]
    archived = [p for p in saved["projects"] if p["archived"]]
    assert [p["id"] for p in archived] == [b["id"]]


def test_reorder_appends_ids_missing_from_the_payload(tmp_path):
    home = str(tmp_path)
    a = projects.create_project(home, "Alpha")
    b = projects.create_project(home, "Beta")
    saved = projects.reorder(home, [b["id"]])
    assert saved["order"] == [b["id"], a["id"]]


# ── adapter integration ──────────────────────────────────────────────────


def test_room_project_id_defaults_null_and_round_trips():
    room = Room.from_dict({"id": "r", "name": "Ops", "members": ["scout"]})
    assert room.project_id is None
    assert room.to_dict()["project_id"] is None
    room2 = Room.from_dict({"id": "r2", "name": "Ops", "members": ["s"], "project_id": "launch"})
    assert room2.project_id == "launch"


def test_adapter_create_room_with_project_and_patch_move(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))

    proj = adapter.create_project("Launch")
    payload = adapter.create_room(
        name="Kickoff",
        members=["scout"],
        lead=None,
        max_agent_turns=None,
        project_id=proj["id"],
    )
    assert payload["project_id"] == proj["id"]

    # Unfiled by default when no project_id is given.
    unfiled_payload = adapter.create_room(
        name="Scratch", members=["scout"], lead=None, max_agent_turns=None
    )
    assert unfiled_payload["project_id"] is None

    moved = adapter.patch_room(unfiled_payload["id"], {"project_id": proj["id"]})
    assert moved["project_id"] == proj["id"]

    back_to_unfiled = adapter.patch_room(unfiled_payload["id"], {"project_id": None})
    assert back_to_unfiled["project_id"] is None

    with pytest.raises(ValueError, match="no such project"):
        adapter.create_room(
            name="Bad", members=["scout"], lead=None, max_agent_turns=None, project_id="ghost"
        )
    with pytest.raises(ValueError, match="no such project"):
        adapter.patch_room(unfiled_payload["id"], {"project_id": "ghost"})


def test_delete_project_unfiles_rooms_but_keeps_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    proj = adapter.create_project("Launch")
    adapter.store.create(_room(id="r-1", project_id=proj["id"]))
    adapter.store.create(_room(id="r-2", project_id=None))

    result = adapter.delete_project(proj["id"])
    assert result["deleted"] == proj["id"]
    assert result["unfiled_rooms"] == ["r-1"]
    assert adapter.store.get("r-1").project_id is None
    # transcript/meta file for the room still exists
    assert adapter.store.get("r-1") is not None

    assert adapter.list_projects() == []
    with pytest.raises(KeyError):
        adapter.delete_project(proj["id"])


def test_list_rooms_unfiled_are_rooms_without_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    proj = adapter.create_project("Launch")
    adapter.store.create(_room(id="filed", project_id=proj["id"]))
    adapter.store.create(_room(id="unfiled", project_id=None))

    rooms = adapter.list_rooms_public()
    by_id = {r["id"]: r for r in rooms}
    assert by_id["filed"]["project_id"] == proj["id"]
    assert by_id["unfiled"]["project_id"] is None


def test_http_projects_routes(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
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
        status, payload = call("POST", "/projects", {"name": "Launch"})
        assert status == 201
        pid = payload["id"]
        assert payload["name"] == "Launch"

        status, payload = call("GET", "/projects")
        assert status == 200
        assert [p["id"] for p in payload["projects"]] == [pid]
        assert payload["order"] == [pid]

        other = call("POST", "/projects", {"name": "Ops"})[1]
        status, payload = call("PUT", "/projects", {"order": [other["id"], pid]})
        assert status == 200
        assert payload["order"] == [other["id"], pid]
        assert [p["id"] for p in payload["projects"]] == [other["id"], pid]
        on_disk = projects.load(str(tmp_path))
        assert on_disk["order"] == [other["id"], pid]

        status, payload = call("PUT", "/projects", {})
        assert status == 400

        status, payload = call("PATCH", f"/projects/{pid}", {"name": "Launch v2"})
        assert status == 200
        assert payload["name"] == "Launch v2"

        status, payload = call("PATCH", "/projects/ghost", {"name": "x"})
        assert status == 404

        status, payload = call("DELETE", f"/projects/{pid}")
        assert status == 200
        assert payload["deleted"] == pid

        status, payload = call("DELETE", f"/projects/{pid}")
        assert status == 404

        status, payload = call("DELETE", f"/projects/{other['id']}")
        assert status == 200

        status, payload = call("GET", "/projects")
        assert status == 200
        assert payload["projects"] == []
        assert payload["order"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()
