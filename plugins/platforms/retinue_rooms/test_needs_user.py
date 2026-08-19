"""@user escalation surfaces as a durable needs_user flag (issue #141).

Run:
  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_needs_user.py -q
"""

from __future__ import annotations

import asyncio

import pytest
from gateway.config import PlatformConfig

from . import engine, principal, tools
from .adapter import RetinueRoomsAdapter
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch) -> RetinueRoomsAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _msg(kind: str, text: str, speaker: str = "scout") -> RoomMessage:
    return RoomMessage(seq=1, ts=0, kind=kind, speaker=speaker, text=text)


def _apply(room: Room, message: RoomMessage, name: str = "Clayton") -> Room:
    engine.apply_needs_user(room, message, principal_name=name)
    return room


# ── mention forms ─────────────────────────────────────────────────────────


def test_generic_user_and_you_mentions_count():
    assert engine.mentions_principal("@user please decide")
    assert engine.mentions_principal("Need a call from @You.")
    assert engine.mentions_principal("cc @USER")
    assert engine.mentions_principal("hey @you — this is yours")


def test_principal_display_name_and_first_name_count():
    assert engine.mentions_principal("@Clayton this needs you", "Clayton")
    assert engine.mentions_principal("@Mark the invoice is ready", "Mark Howell")
    assert not engine.mentions_principal("@Howell the invoice is ready", "Mark Howell")


def test_ordinary_prose_and_fenced_mentions_do_not_count():
    assert not engine.mentions_principal("the user already signed off")
    assert not engine.mentions_principal("see you tomorrow")
    fenced = "draft copy:\n```\nAsk @user in the blog body.\n```\n"
    assert not engine.mentions_principal(fenced)


def test_named_alias_does_not_steal_a_member_mention():
    """If the principal and a retainer share a first name, @Name is the retainer."""
    members = ["clayton-ops"]
    names = {"clayton-ops": "Clayton"}
    assert not engine.mentions_principal(
        "@Clayton please file this",
        "Clayton",
        members=members,
        display_names=names,
    )
    assert engine.mentions_principal(
        "@user please file this",
        "Clayton",
        members=members,
        display_names=names,
    )


def test_briefing_teaches_user_escalation():
    room = _room()
    named = engine.room_briefing(room, "scout", ["Clayton"], principal_name="Clayton")
    assert "@user" in named and "@Clayton" in named
    assert "flags the room as needing them" in named
    generic = engine.room_briefing(room, "scout", ["You"])
    assert "@user" in generic
    assert "or @You" not in generic


# ── set / not-set / clear ─────────────────────────────────────────────────


def test_agent_mention_sets_needs_user():
    room = _room()
    assert room.needs_user is False
    _apply(room, _msg(KIND_AGENT, "@user I need a decision"))
    assert room.needs_user is True


def test_ordinary_agent_message_does_not_set_needs_user():
    room = _room()
    _apply(room, _msg(KIND_AGENT, "Filed the invoice. Standing by."))
    assert room.needs_user is False
    room.needs_user = True
    _apply(room, _msg(KIND_AGENT, "Still waiting on the vendor."))
    assert room.needs_user is True


def test_principal_post_clears_needs_user():
    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "Got it — I'll look.", speaker="Clayton"))
    assert room.needs_user is False
    _apply(room, _msg(KIND_USER, "@scout carry on", speaker="Clayton"))
    assert room.needs_user is False


def test_system_notice_neither_sets_nor_clears():
    room = _room()
    _apply(room, _msg(KIND_SYSTEM, "@user joined the room", speaker="room"))
    assert room.needs_user is False
    room.needs_user = True
    _apply(room, _msg(KIND_SYSTEM, "Stopped.", speaker="room"))
    assert room.needs_user is True


def test_needs_user_roundtrips_on_room_meta():
    room = _room(needs_user=True)
    loaded = Room.from_dict(room.to_dict())
    assert loaded.needs_user is True
    missing = Room.from_dict({"id": "r-1", "name": "Test", "members": ["scout"]})
    assert missing.needs_user is False


# ── persistence ───────────────────────────────────────────────────────────


def test_needs_user_survives_store_reload(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    room = store.get("r-1")
    engine.apply_needs_user(
        room, _msg(KIND_AGENT, "@you this is blocked"), principal_name="Clayton"
    )
    store.update(room)

    reopened = RoomStore(base_dir=str(tmp_path))
    loaded = reopened.get("r-1")
    assert loaded is not None
    assert loaded.needs_user is True

    engine.apply_needs_user(
        loaded, _msg(KIND_USER, "I'm here", speaker="Clayton"), principal_name="Clayton"
    )
    reopened.update(loaded)
    assert RoomStore(base_dir=str(tmp_path)).get("r-1").needs_user is False


# ── adapter / API ─────────────────────────────────────────────────────────


def test_list_and_room_payloads_expose_needs_user(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(needs_user=True))
    adapter.store.create(_room(id="r-2", name="Quiet", members=["scout"]))
    by_id = {row["id"]: row for row in adapter.list_rooms_public()}
    assert by_id["r-1"]["needs_user"] is True
    assert by_id["r-2"]["needs_user"] is False
    payload = adapter._room_payload(adapter.store.get("r-1"))
    assert payload["needs_user"] is True


async def _run_locked(adapter, room, user_message):
    async with adapter._room_lock(room.id):
        await adapter._run_cycle_workspace(room, user_message)


def test_member_turn_sets_needs_user_on_principal_mention(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    room = _room(members=["scout"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Clayton", text="status?"),
    )

    async def fake_turn(_room, member):
        return True, "@Clayton I need you to pick a vendor."

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert adapter.store.get(room.id).needs_user is True


def test_ordinary_member_turn_does_not_set_needs_user(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="status?"),
    )

    async def fake_turn(_room, member):
        return True, "All clear. Nothing to escalate."

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert adapter.store.get(room.id).needs_user is False


def test_post_user_message_clears_needs_user(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(needs_user=True, members=["scout"], lead="scout"))
    loop = asyncio.new_event_loop()
    adapter._loop = loop

    def fake_cycle(*_a, **_k):
        return None

    monkeypatch.setattr(adapter, "_run_cycle", fake_cycle)

    class _Fut:
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe", lambda *_a, **_k: _Fut()
    )
    try:
        adapter.post_user_message("r-1", "I'm back", "You")
        assert adapter.store.get("r-1").needs_user is False
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_cron_origin_mention_sets_needs_user(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    result = await adapter.send(
        "r-1",
        "@user the walkthrough is ready.",
        metadata={"job_id": "job-1", "thread_id": "scout"},
    )
    assert result.success is True
    assert adapter.store.get("r-1").needs_user is True


def test_cross_room_post_mention_sets_needs_user_on_destination(
    tmp_path, monkeypatch
):
    from . import crossroom

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "scout"))
    (tmp_path / "profiles" / "scout").mkdir(parents=True)
    store = RoomStore(base_dir=str(tmp_path / "retinue_rooms"))
    store.create(_room(id="r-a", name="Alpha", members=["scout", "editor"]))
    store.create(_room(id="r-b", name="Beta", members=["scout"]))
    with crossroom.in_room("r-a"):
        out = tools.rooms_post(
            {"room": "Beta", "message": "@you this needs a human call"}
        )
    assert "Beta" in out
    dest = store.get("r-b")
    assert dest is not None
    assert dest.needs_user is True
    assert store.get("r-a").needs_user is False
