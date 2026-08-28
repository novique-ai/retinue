"""Composition-projection export (issue #229).

When RETINUE_ROOM_TEMPLATES_DIR is set, the store mirrors each room's
operator-meaningful composition into that directory on its own — no
out-of-band capture tool. Unset, nothing new is written anywhere.
"""

from __future__ import annotations

import json
import os

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
    store = _store(tmp_path)
    store.create(_room())
    store.delete("r-1")
    assert not (tmp_path / "templates" / "r-1.json").exists()


def test_startup_backfills_and_prunes(tmp_path):
    seeded = _store(tmp_path, templates=False)
    seeded.create(_room())
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "ghost.json").write_text(json.dumps({"id": "ghost"}), encoding="utf-8")
    (templates / "not-ours.json").write_text(json.dumps({"kind": "unrelated"}), encoding="utf-8")

    _store(tmp_path)  # same base_dir, templates now configured

    assert _read(tmp_path)["id"] == "r-1"
    assert not (templates / "ghost.json").exists()
    assert (templates / "not-ours.json").exists()


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
