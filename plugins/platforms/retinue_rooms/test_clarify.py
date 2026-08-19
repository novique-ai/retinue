"""Clarify prompts land on the room transcript and resolve from the next line.

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_clarify.py -q
"""

from __future__ import annotations

import asyncio

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult

from . import clarify as room_clarify
from . import engine
from .adapter import RetinueRoomsAdapter
from .engine import KIND_AGENT, KIND_USER, Room, RoomMessage
from .store import RoomStore
from .test_stop import _kinds, _run_locked, _system_texts


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter._loop = object()
    return adapter


def test_format_prompt_is_numbered_and_answerable():
    text = room_clarify.format_prompt(
        "Which .6?",
        ["Close infrastructure-5ta4.6", "Wrong bead"],
    )
    assert "❓ Which .6?" in text
    assert "1. Close infrastructure-5ta4.6" in text
    assert "2. Wrong bead" in text
    assert "Reply with the number" in text


def test_send_clarify_posts_as_the_retainer(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)

    result = asyncio.run(
        adapter.send_clarify(
            chat_id=room.id,
            question="Which .6?",
            choices=["Close it", "Wrong bead"],
            clarify_id="abc123",
            session_key="agent:scout:retinue_rooms:group:r-1:scout",
            metadata={"retinue_member": "scout"},
        )
    )
    assert isinstance(result, SendResult) and result.success
    kinds = _kinds(adapter.store, room.id)
    assert any(kind == KIND_AGENT and speaker == "scout" for kind, speaker, _ in kinds)
    posted = [text for kind, speaker, text in kinds if kind == KIND_AGENT]
    assert posted and "Which .6?" in posted[0]
    assert "1. Close it" in posted[0]


def test_typed_reply_resolves_clarify_and_does_not_start_a_cycle(tmp_path, monkeypatch):
    from tools.clarify_gateway import register, wait_for_response

    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    key = room_clarify.session_keys(room.id, "scout")[0]
    cid = "cid-rooms-1"
    register(cid, key, "Which .6?", ["Close it", "Wrong bead"])

    started = []

    def fake_schedule(*_a, **_k):
        started.append(True)
        raise AssertionError("must not start a cycle while answering clarify")

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_schedule)
    result = adapter.post_user_message(room.id, "2", "Mark")
    assert result.get("clarify") is True
    assert result.get("planned") == []
    assert started == []
    assert wait_for_response(cid, timeout=0.1) == "Wrong bead"
    kinds = _kinds(adapter.store, room.id)
    assert any(kind == KIND_USER and "2" in text for kind, _s, text in kinds)


def test_timeout_speaks_the_waiting_yes_no(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="close .6"),
    )

    class _Entry:
        question = "Is this really infrastructure-5ta4.6?"

    monkeypatch.setattr(
        room_clarify, "pending_for_room", lambda _room: ("scout", _Entry())
    )
    released = []
    monkeypatch.setattr(room_clarify, "release_room", lambda r: released.append(r.id))

    async def fake_turn(_room, member):
        return False, "no reply within 900s"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    kinds = _kinds(adapter.store, room.id)
    spoken = [text for kind, speaker, text in kinds if kind == KIND_AGENT and speaker == "scout"]
    assert spoken
    assert "infrastructure-5ta4.6" in spoken[0]
    assert engine.FALLBACK_GENERIC not in spoken
    assert "ask me to continue" not in spoken[0].lower()
    assert released == [room.id]
    assert any("did not reply" in text for text in _system_texts(adapter.store, room.id))
