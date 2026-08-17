"""Incremental invite / remove: last_seen window, surviving cursor, notices."""

from __future__ import annotations

import json

import pytest

from . import engine
from .engine import KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _fill(store: RoomStore, room_id: str, n: int) -> None:
    for i in range(n):
        store.append(
            room_id,
            RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text=f"line-{i + 1}"),
        )


def _unseen(store: RoomStore, room: Room, member: str) -> list[RoomMessage]:
    return store.read_since(room.id, room.last_seen.get(member, 0))


def _system_texts(store: RoomStore, room_id: str) -> list[str]:
    return [m.text for m in store.read_since(room_id, 0) if m.kind == KIND_SYSTEM]


def test_fresh_invitee_into_long_room_receives_only_last_window(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    _fill(adapter.store, "r-1", 25)
    payload = adapter.add_room_member("r-1", "critic")
    room = adapter.store.get("r-1")
    unseen = _unseen(adapter.store, room, "critic")
    assert payload["members"] == ["scout", "editor", "critic"]
    assert len(unseen) == engine.INVITE_TRANSCRIPT_WINDOW
    assert unseen[0].text == "line-7"
    assert unseen[-1].text == engine.member_joined_notice("critic")
    assert all(m.text != "line-1" for m in unseen)


def test_fresh_invitee_into_short_room_receives_everything(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    _fill(adapter.store, "r-1", 5)
    adapter.add_room_member("r-1", "critic")
    room = adapter.store.get("r-1")
    unseen = _unseen(adapter.store, room, "critic")
    assert room.last_seen["critic"] == 0
    assert [m.text for m in unseen] == [
        "line-1",
        "line-2",
        "line-3",
        "line-4",
        "line-5",
        engine.member_joined_notice("critic"),
    ]


def test_fresh_invitee_into_empty_room_gets_non_negative_cursor(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    adapter.add_room_member("r-1", "critic")
    room = adapter.store.get("r-1")
    unseen = _unseen(adapter.store, room, "critic")
    assert room.last_seen["critic"] == 0
    assert [m.text for m in unseen] == [engine.member_joined_notice("critic")]


def test_reinvite_resumes_old_last_seen_instead_of_reset(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout", "editor", "critic"], lead="scout")
    room.last_seen["critic"] = 4
    adapter.store.create(room)
    _fill(adapter.store, "r-1", 10)
    adapter.remove_room_member("r-1", "critic")
    _fill(adapter.store, "r-1", 5)
    adapter.add_room_member("r-1", "critic")
    again = adapter.store.get("r-1")
    unseen = _unseen(adapter.store, again, "critic")
    texts = [m.text for m in unseen]
    assert again.last_seen["critic"] == 4
    assert unseen[0].seq == 5 and unseen[0].text == "line-5"
    assert engine.member_left_notice("critic") in texts
    assert texts[-1] == engine.member_joined_notice("critic")
    assert all(m.seq > 4 for m in unseen)


def test_removal_preserves_last_seen(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    room.last_seen["editor"] = 3
    adapter.store.create(room)
    adapter.remove_room_member("r-1", "editor")
    loaded = adapter.store.get("r-1")
    assert "editor" not in loaded.members
    assert loaded.last_seen.get("editor") == 3
    assert loaded.lead == "scout"


def test_join_posts_exactly_one_system_message(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    adapter.add_room_member("r-1", "critic")
    texts = _system_texts(adapter.store, "r-1")
    assert texts == [engine.member_joined_notice("critic")]


def test_removal_posts_exactly_one_system_message(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    adapter.remove_room_member("r-1", "editor")
    texts = _system_texts(adapter.store, "r-1")
    assert texts == [engine.member_left_notice("editor")]


def test_full_array_patch_seeds_and_announces_like_an_invite(tmp_path, monkeypatch):
    """The Edit Room panel restaffs with a full array — same door, same rules.

    Seeding only the incremental endpoint would leave the whole-transcript-on-
    first-turn bug alive behind the path most people actually use.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    _fill(adapter.store, "r-1", 25)
    adapter.store.touch_last_seen("r-1", "editor", 11)
    payload = adapter.patch_room("r-1", {"members": ["scout", "critic"]})
    assert payload["members"] == ["scout", "critic"]
    room = adapter.store.get("r-1")

    # editor left, critic joined — one notice each, and nothing else.
    assert _system_texts(adapter.store, "r-1") == [
        engine.member_left_notice("editor"),
        engine.member_joined_notice("critic"),
    ]
    # The newcomer is windowed, not handed all 25.
    assert len(_unseen(adapter.store, room, "critic")) == engine.INVITE_TRANSCRIPT_WINDOW
    # The departed member's cursor survives untouched for a later re-invite.
    assert room.last_seen["editor"] == 11


def test_full_array_patch_does_not_reseed_existing_members(tmp_path, monkeypatch):
    """A restaff that only reorders must not move anyone's cursor."""
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    _fill(adapter.store, "r-1", 25)
    adapter.store.touch_last_seen("r-1", "editor", 4)
    adapter.patch_room("r-1", {"members": ["editor", "scout"]})
    room = adapter.store.get("r-1")
    assert room.last_seen["editor"] == 4
    assert _system_texts(adapter.store, "r-1") == []


def test_add_rejects_duplicate_empty_and_missing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    with pytest.raises(ValueError, match="already a member"):
        adapter.add_room_member("r-1", "scout")
    with pytest.raises(ValueError, match="required"):
        adapter.add_room_member("r-1", "  ")
    with pytest.raises(KeyError):
        adapter.add_room_member("ghost", "critic")


def test_remove_rejects_last_member_and_stranger(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(members=["scout"], lead="scout"))
    with pytest.raises(ValueError, match="at least one"):
        adapter.remove_room_member("r-1", "scout")
    adapter.store.create(_room(id="r-2"))
    with pytest.raises(ValueError, match="not a member"):
        adapter.remove_room_member("r-2", "critic")
    with pytest.raises(KeyError):
        adapter.remove_room_member("ghost", "scout")


def test_http_invite_and_remove_are_incremental(tmp_path, monkeypatch):
    import http.client
    import threading

    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    _fill(adapter.store, "r-1", 25)
    from .adapter import _RoomsRequestHandler, _RoomsServer

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
        status, payload = call("POST", "/rooms/r-1/members", {"member": "critic"})
        assert status == 201
        assert payload["members"] == ["scout", "editor", "critic"]
        room = adapter.store.get("r-1")
        unseen = _unseen(adapter.store, room, "critic")
        assert len(unseen) == engine.INVITE_TRANSCRIPT_WINDOW
        assert _system_texts(adapter.store, "r-1") == [engine.member_joined_notice("critic")]

        status, payload = call("DELETE", "/rooms/r-1/members/critic")
        assert status == 200
        assert "critic" not in payload["members"]
        assert adapter.store.get("r-1").last_seen["critic"] == room.last_seen["critic"]
        assert _system_texts(adapter.store, "r-1") == [
            engine.member_joined_notice("critic"),
            engine.member_left_notice("critic"),
        ]

        status, payload = call("POST", "/rooms/r-1/members", {"member": "scout"})
        assert status == 400
        status, payload = call("DELETE", "/rooms/ghost/members/scout")
        assert status == 404
        status, payload = call("POST", "/rooms/ghost/members", {"member": "critic"})
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
