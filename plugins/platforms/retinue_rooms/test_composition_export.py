"""Composition-projection export (issues #229, #240).

When RETINUE_ROOM_TEMPLATES_DIR is set, the store mirrors each room's
operator-meaningful composition into that directory on its own — no
out-of-band capture tool. Unset, nothing new is written anywhere.

Constructing a store is read/additive only. It backfills rooms it can
read and never removes a projection. Only RoomStore.delete does that.
An empty, unreadable, or mis-rooted room directory is not a deletion.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from . import store as room_store
from .engine import Room, RoomMessage
from .store import COMPOSITION_FIELDS, RoomStore, composition_projection


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _store(tmp_path, templates=True) -> RoomStore:
    return RoomStore(
        base_dir=str(tmp_path / "rooms"),
        templates_dir=str(tmp_path / "templates") if templates else None,
    )


def _read(tmp_path, room_id="r-1") -> dict:
    with open(tmp_path / "templates" / f"{room_id}.json", encoding="utf-8") as f:
        return json.load(f)


def _template_bytes(directory) -> dict:
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def _warning_text(caplog) -> str:
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name == room_store.__name__
    )


def test_create_exports_composition_only(tmp_path):
    store = _store(tmp_path)
    store.create(_room(last_seen={"scout": 7}))
    proj = _read(tmp_path)
    assert proj["id"] == "r-1"
    assert proj["members"] == ["scout", "editor"]
    assert set(proj) <= set(COMPOSITION_FIELDS)
    for volatile in ("created_at", "last_seen", "needs_user"):
        assert volatile not in proj


def test_ide_path_is_home_portable(tmp_path):
    home = os.path.expanduser("~")
    store = _store(tmp_path)
    store.create(_room(workspace="ide", ide_path=os.path.join(home, "IDE")))
    assert _read(tmp_path)["ide_path"] == "$HOME/IDE"


def test_volatile_writes_do_not_rewrite_projection(tmp_path):
    store = _store(tmp_path)
    store.create(_room())
    path = tmp_path / "templates" / "r-1.json"
    before = path.stat().st_mtime_ns
    store.touch_last_seen("r-1", "scout", 5)
    store.append("r-1", RoomMessage(seq=0, ts=0, kind="user", speaker="u", text="hi"))
    store.touch_last_seen("r-1", "editor", 9)
    assert path.stat().st_mtime_ns == before


def test_composition_change_updates_projection(tmp_path):
    store = _store(tmp_path)
    store.create(_room())
    store.mutate("r-1", lambda room: room.members.append("junior"))
    assert _read(tmp_path)["members"] == ["scout", "editor", "junior"]


def test_delete_removes_projection(tmp_path):
    """Explicit delete removes that room's projection and nothing else."""
    store = _store(tmp_path)
    store.create(_room())
    store.create(_room(id="r-2", name="Other"))
    ghost = tmp_path / "templates" / "ghost.json"
    ghost.write_text(json.dumps({"id": "ghost"}), encoding="utf-8")

    store.delete("r-1")

    assert not (tmp_path / "templates" / "r-1.json").exists()
    assert _read(tmp_path, "r-2")["id"] == "r-2"
    assert ghost.exists()
    # A later construction must not reap the orphan or resurrect r-1.
    _store(tmp_path)
    assert not (tmp_path / "templates" / "r-1.json").exists()
    assert _read(tmp_path, "r-2")["id"] == "r-2"
    assert ghost.read_text(encoding="utf-8") == json.dumps({"id": "ghost"})


def test_startup_backfills_without_deleting_orphans(tmp_path, caplog):
    """Enabling the feature exports live rooms and keeps unknown projections."""
    seeded = _store(tmp_path, templates=False)
    seeded.create(_room())
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "ghost.json").write_text(json.dumps({"id": "ghost"}), encoding="utf-8")
    (templates / "not-ours.json").write_text(json.dumps({"kind": "unrelated"}), encoding="utf-8")
    (templates / "broken.json").write_text("{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        _store(tmp_path)  # same base_dir, templates now configured

    assert _read(tmp_path)["id"] == "r-1"
    assert _read(tmp_path)["members"] == ["scout", "editor"]
    assert (templates / "ghost.json").read_text(encoding="utf-8") == json.dumps({"id": "ghost"})
    assert json.loads((templates / "not-ours.json").read_text(encoding="utf-8")) == {"kind": "unrelated"}
    assert (templates / "broken.json").read_text(encoding="utf-8") == "{"
    warnings = _warning_text(caplog)
    assert "ghost" in warnings
    assert "leaving it in place" in warnings
    assert "not-ours" not in warnings
    assert "broken" not in warnings


def test_unset_templates_dir_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_ROOM_TEMPLATES_DIR", raising=False)
    store = _store(tmp_path, templates=False)
    store.create(_room())
    assert not (tmp_path / "templates").exists()
    assert store.templates_dir is None


def test_env_var_enables_export(tmp_path, monkeypatch):
    monkeypatch.setenv("RETINUE_ROOM_TEMPLATES_DIR", str(tmp_path / "templates"))
    store = RoomStore(base_dir=str(tmp_path / "rooms"))
    store.create(_room())
    assert _read(tmp_path)["id"] == "r-1"


def test_projection_matches_capture_tool_format(tmp_path):
    """Field order and $HOME portability must match what an external capture
    of GET /rooms would produce, so both writers converge byte-identically."""
    room = _room(workspace="ide", ide_path=os.path.expanduser("~/IDE"))
    proj = composition_projection(room.to_dict())
    assert list(proj) == [k for k in COMPOSITION_FIELDS if k in room.to_dict()]


def test_startup_backfill_refreshes_a_drifted_live_projection(tmp_path):
    store = _store(tmp_path)
    store.create(_room())
    drifted = tmp_path / "templates" / "r-1.json"
    drifted.write_text(json.dumps({"id": "r-1", "members": ["stale"]}) + "\n", encoding="utf-8")

    _store(tmp_path)

    assert _read(tmp_path)["members"] == ["scout", "editor"]
    assert "last_seen" not in _read(tmp_path)


def test_unreadable_room_meta_preserves_projection(tmp_path, caplog):
    """Corrupt live meta is a warning, not a reason to drop the projection."""
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    (rooms / "r-1.json").write_text("{", encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    payload = json.dumps({"id": "r-1", "name": "Kept"}, indent=2) + "\n"
    (templates / "r-1.json").write_text(payload, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        store = RoomStore(base_dir=str(rooms), templates_dir=str(templates))

    assert (templates / "r-1.json").read_text(encoding="utf-8") == payload
    assert store.get("r-1") is None
    assert store.list_rooms() == []
    warnings = _warning_text(caplog)
    assert "r-1" in warnings
    assert "unreadable or corrupt" in warnings
    assert "leaving its composition projection in place" in warnings


def test_non_object_room_meta_does_not_abort_construction(tmp_path, caplog):
    """A room file that is JSON but not an object must not crash startup."""
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    (rooms / "r-1.json").write_text("[]", encoding="utf-8")
    (rooms / "r-ok.json").write_text(
        json.dumps(_room(id="r-ok", name="Ok").to_dict()), encoding="utf-8"
    )
    templates = tmp_path / "templates"
    templates.mkdir()
    kept = json.dumps({"id": "r-1", "name": "Kept"}) + "\n"
    (templates / "r-1.json").write_text(kept, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        store = RoomStore(base_dir=str(rooms), templates_dir=str(templates))

    assert (templates / "r-1.json").read_text(encoding="utf-8") == kept
    assert _read(tmp_path, "r-ok")["id"] == "r-ok"
    assert [room.id for room in store.list_rooms()] == ["r-ok"]
    assert "r-1" in _warning_text(caplog)


def test_non_room_json_is_not_reported_corrupt(tmp_path, caplog):
    """Runtime JSON beside rooms is not room metadata and must not warn."""
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    (rooms / "grok_sessions.json").write_text(
        json.dumps({"r-1|scout": {"session_id": "session-1"}}), encoding="utf-8"
    )
    (rooms / "runtime_state.json").write_text(
        json.dumps({"kind": "runtime-state"}), encoding="utf-8"
    )
    (rooms / "r-1.json").write_text("{", encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    kept = json.dumps({"id": "r-1", "name": "Kept"}) + "\n"
    (templates / "r-1.json").write_text(kept, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        RoomStore(base_dir=str(rooms), templates_dir=str(templates))

    assert (templates / "r-1.json").read_text(encoding="utf-8") == kept
    warnings = _warning_text(caplog)
    assert "r-1" in warnings
    assert "grok_sessions" not in warnings
    assert "runtime_state" not in warnings


def test_repeated_construction_does_not_rewrite_or_delete(tmp_path):
    store = _store(tmp_path)
    store.create(_room())
    templates = tmp_path / "templates"
    (templates / "ghost.json").write_text(json.dumps({"id": "ghost"}) + "\n", encoding="utf-8")
    before = _template_bytes(templates)
    mtimes = {path.name: path.stat().st_mtime_ns for path in templates.iterdir()}

    _store(tmp_path)
    _store(tmp_path)

    assert _template_bytes(templates) == before
    assert {path.name: path.stat().st_mtime_ns for path in templates.iterdir()} == mtimes


def test_empty_room_dir_does_not_touch_projections(tmp_path, caplog):
    """A mis-rooted empty room directory must not be read as 'rooms were deleted'."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "r-1.json").write_text(
        json.dumps({"id": "r-1", "members": ["scout"]}, indent=2) + "\n", encoding="utf-8"
    )
    (templates / "notes.txt").write_text("keep", encoding="utf-8")
    before = _template_bytes(templates)

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        RoomStore(base_dir=str(tmp_path / "empty-rooms"), templates_dir=str(templates))
        RoomStore(base_dir=str(tmp_path / "empty-rooms"), templates_dir=str(templates))

    assert _template_bytes(templates) == before
    warnings = _warning_text(caplog)
    assert "r-1" in warnings
    assert "leaving it in place" in warnings


def test_unreadable_room_directory_leaves_projections(tmp_path, caplog):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read a mode-000 directory")
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    templates = tmp_path / "templates"
    templates.mkdir()
    payload = json.dumps({"id": "r-1", "name": "Kept"}) + "\n"
    (templates / "r-1.json").write_text(payload, encoding="utf-8")
    rooms.chmod(0)
    try:
        with caplog.at_level(logging.WARNING, logger=room_store.__name__):
            RoomStore(base_dir=str(rooms), templates_dir=str(templates))
        assert (templates / "r-1.json").read_text(encoding="utf-8") == payload
        warnings = _warning_text(caplog)
        assert "leaving composition projections untouched" in warnings
        assert "leaving it in place" not in warnings
    finally:
        rooms.chmod(0o700)


def test_tools_store_under_empty_home_does_not_touch_projections(
    tmp_path, monkeypatch, caplog
):
    """Room-tool store construction must not reconcile projections destructively.

    A member turn points HERMES_HOME at a profile. The tool collapses that
    to the workspace home and builds a RoomStore. An empty workspace is not
    evidence that every composition projection should disappear.
    """
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "r-1.json").write_text(
        json.dumps({"id": "r-1", "name": "Alpha", "members": ["scout"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (templates / "not-ours.json").write_text(json.dumps({"kind": "note"}), encoding="utf-8")
    before = _template_bytes(templates)
    profile = tmp_path / "wrong" / "profiles" / "scout"
    profile.mkdir(parents=True)
    rooms = tmp_path / "wrong" / "retinue_rooms"
    rooms.mkdir()
    (rooms / "r-1.json").write_text(
        json.dumps(_room(members=["stale"]).to_dict()), encoding="utf-8"
    )
    (rooms / "r-2.json").write_text(
        json.dumps(_room(id="r-2", name="Unprojected").to_dict()), encoding="utf-8"
    )
    (rooms / "grok_sessions.json").write_text(
        json.dumps({"r-1|scout": {"session_id": "session-1"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("RETINUE_ROOM_TEMPLATES_DIR", str(templates))

    from .tools import _store as tools_store

    with caplog.at_level(logging.WARNING, logger=room_store.__name__):
        tools_store()
        tools_store()

    assert _template_bytes(templates) == before
    assert not (templates / "r-2.json").exists()
    assert _warning_text(caplog) == ""
