"""Speak-or-pass turns and follow-up round settling (issue #140).

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_speak_or_pass.py -q
"""

from __future__ import annotations

import asyncio

from gateway.config import PlatformConfig

from . import engine
from .adapter import RetinueRoomsAdapter
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(
        id="r-1",
        name="Test",
        members=["scout", "editor", "critic"],
        lead="scout",
    )
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _kinds(store: RoomStore, room_id: str) -> list[tuple[str, str, str]]:
    return [(m.kind, m.speaker, m.text) for m in store.read_since(room_id, 0)]


def _system_texts(store: RoomStore, room_id: str) -> list[str]:
    return [m.text for m in store.read_since(room_id, 0) if m.kind == KIND_SYSTEM]


def _agent_texts(store: RoomStore, room_id: str) -> list[tuple[str, str]]:
    return [
        (m.speaker, m.text)
        for m in store.read_since(room_id, 0)
        if m.kind == KIND_AGENT
    ]


async def _run_locked(adapter, room, user_message):
    async with adapter._room_lock(room.id):
        await adapter._run_cycle_workspace(room, user_message)


def test_pass_posts_nothing_failed_turn_still_does(tmp_path, monkeypatch):
    """A pass is silent. A failed turn still posts did-not-reply. They are
    not the same path."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )

    async def fake_turn(_room, member):
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert _agent_texts(adapter.store, room.id) == []
    assert engine.FALLBACK_GENERIC not in [text for _k, _s, text in _kinds(adapter.store, room.id)]
    assert not any(
        engine.DID_NOT_REPLY_INFIX in text for text in _system_texts(adapter.store, room.id)
    )


def test_failed_turn_is_still_did_not_reply_not_a_pass(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )

    async def fake_turn(_room, member):
        return False, "no reply within 300s"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    notice = engine.did_not_reply_notice("scout", "no reply within 300s")
    assert notice in _system_texts(adapter.store, room.id)
    assert _agent_texts(adapter.store, room.id) == []


def test_prose_that_says_pass_is_spoken(tmp_path, monkeypatch):
    """The structured contract is the whole reply. '(pass)' in prose lands."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(
            seq=0,
            ts=0,
            kind=KIND_USER,
            speaker="Mark",
            text="@scout please take the image pass",
        ),
    )

    async def fake_turn(_room, member):
        return True, "I'll take the image pass."

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert _agent_texts(adapter.store, room.id) == [("scout", "I'll take the image pass.")]


def test_unmentioned_post_still_starts_with_the_lead(tmp_path, monkeypatch):
    """First-round routing is unchanged: no @mention → lead, then follow-ups."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="what do you think?"),
    )
    order: list[str] = []

    async def fake_turn(_room, member):
        order.append(member)
        if member == "scout":
            return True, "lead take"
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert order[0] == "scout"
    assert _agent_texts(adapter.store, room.id) == [("scout", "lead take")]
    assert "editor" in order and "critic" in order


def test_followup_round_settles_when_everyone_passes(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "noted"
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    # Lead spoke; editor and critic each got one follow-up and passed.
    # Nobody is asked a second time — the round settled.
    assert calls == ["scout", "editor", "critic"]
    assert _agent_texts(adapter.store, room.id) == [("scout", "noted")]
    assert not any(
        engine.DID_NOT_REPLY_INFIX in text for text in _system_texts(adapter.store, room.id)
    )


def test_a_followup_speech_opens_another_round(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    calls: list[str] = []
    editor_speeches = 0

    async def fake_turn(_room, member):
        nonlocal editor_speeches
        calls.append(member)
        if member == "scout" and calls.count("scout") == 1:
            return True, "lead take"
        if member == "editor" and editor_speeches == 0:
            editor_speeches += 1
            return True, "editor adds a point"
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert calls[0] == "scout"
    assert "editor" in calls[:4]
    # Editor's speech re-opens a round: scout and critic get another chance.
    assert calls.count("scout") == 2
    assert calls.count("critic") == 2
    assert ("editor", "editor adds a point") in _agent_texts(adapter.store, room.id)
    # The extra round all passed — no third lap.
    assert calls.count("editor") == 1


def test_followup_round_cap_stops_even_when_someone_keeps_speaking(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(max_followup_rounds=1)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        return True, f"{member} has more to say"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    # First wave: scout. One follow-up round: editor, critic. Cap stops
    # a second follow-up even though everyone spoke.
    assert calls == ["scout", "editor", "critic"]
    assert [speaker for speaker, _text in _agent_texts(adapter.store, room.id)] == [
        "scout",
        "editor",
        "critic",
    ]


def test_budget_is_the_hard_ceiling_over_followup_rounds(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(max_agent_turns=2, max_followup_rounds=3)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        return True, f"{member} here"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert calls == ["scout", "editor"]
    assert len(_agent_texts(adapter.store, room.id)) == 2
    budget_lines = [
        text
        for text in _system_texts(adapter.store, room.id)
        if text.startswith(engine.CYCLE_BUDGET_PREFIX)
    ]
    assert len(budget_lines) == 1
    assert "critic" in budget_lines[0]


def test_zero_followup_rounds_is_first_wave_only(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        return True, "only me"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert calls == ["scout"]
    assert _agent_texts(adapter.store, room.id) == [("scout", "only me")]


def test_mention_followups_in_the_first_wave_still_run(tmp_path, monkeypatch):
    """First-round @mention handoff is unchanged; remaining members then
    get a settle round."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(
            seq=0,
            ts=0,
            kind=KIND_USER,
            speaker="Mark",
            text="@scout please",
        ),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "draft here. @editor please tighten."
        if member == "editor":
            return True, "tightened."
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert calls[0] == "scout"
    assert calls[1] == "editor"
    assert "critic" in calls
    assert _agent_texts(adapter.store, room.id) == [
        ("scout", "draft here. @editor please tighten."),
        ("editor", "tightened."),
    ]
