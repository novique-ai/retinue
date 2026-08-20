"""Stop the in-flight room cycle without touching other rooms."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading

import pytest
from gateway.config import PlatformConfig

from . import engine
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _system_texts(store: RoomStore, room_id: str) -> list[str]:
    return [m.text for m in store.read_since(room_id, 0) if m.kind == KIND_SYSTEM]


def _kinds(store: RoomStore, room_id: str) -> list[tuple[str, str, str]]:
    return [(m.kind, m.speaker, m.text) for m in store.read_since(room_id, 0)]


async def _run_locked(adapter, room, user_message):
    """Hold the per-room lock the same way _run_cycle does, so stop sees activity."""
    async with adapter._room_lock(room.id):
        await adapter._run_cycle_workspace(room, user_message)


def test_stop_while_idle_is_a_noop(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    result = adapter.stop_cycle("r-1", "Mark")
    assert result == {"stopped": False, "idle": True}
    assert _system_texts(adapter.store, "r-1") == []


def test_stop_missing_room_is_404_shape(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        adapter.stop_cycle("ghost", "Mark")


def test_stop_aborts_before_the_next_speaker(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="@scout @editor go"),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_turn(_room, member):
        if member == "scout":
            started.set()
            await release.wait()
            return True, "this reply must not land"
        raise AssertionError(f"editor should not start after stop, got {member}")

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)

    async def scenario():
        task = asyncio.create_task(_run_locked(adapter, room, user_message))
        await started.wait()
        result = await adapter._stop_cycle(room.id, "Mark")
        release.set()
        await task
        return result

    result = asyncio.run(scenario())
    assert result["stopped"] is True
    assert result["already"] is False
    assert result["notice"].startswith(engine.CYCLE_STOPPED_PREFIX)
    assert "Mark stopped this turn." in result["notice"]
    kinds = _kinds(adapter.store, room.id)
    assert (KIND_USER, "Mark", "@scout @editor go") in kinds
    assert any(
        kind == KIND_SYSTEM and text.startswith(engine.CYCLE_STOPPED_PREFIX)
        for kind, _speaker, text in kinds
    )
    assert not any(kind == KIND_AGENT for kind, _speaker, _text in kinds)
    assert "editor is on it." not in _system_texts(adapter.store, room.id)


def test_stop_does_not_post_a_fallback_reply(tmp_path, monkeypatch):
    """A cancelled _agent_turn must not become the usual fallback line."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_turn(_room, member):
        started.set()
        await release.wait()
        return False, "stopped"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)

    async def scenario():
        task = asyncio.create_task(_run_locked(adapter, room, user_message))
        await started.wait()
        await adapter._stop_cycle(room.id, "Mark")
        release.set()
        await task

    asyncio.run(scenario())
    assert not any(m.kind == KIND_AGENT for m in adapter.store.read_since(room.id, 0))


def test_second_stop_does_not_duplicate_the_notice(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_turn(_room, member):
        started.set()
        await release.wait()
        return True, "nope"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)

    async def scenario():
        task = asyncio.create_task(_run_locked(adapter, room, user_message))
        await started.wait()
        first = await adapter._stop_cycle(room.id, "Mark")
        second = await adapter._stop_cycle(room.id, "Mark")
        release.set()
        await task
        return first, second

    first, second = asyncio.run(scenario())
    assert first["stopped"] is True and first["already"] is False
    assert second["stopped"] is True and second["already"] is True
    notices = [
        text
        for text in _system_texts(adapter.store, room.id)
        if text.startswith(engine.CYCLE_STOPPED_PREFIX)
    ]
    assert len(notices) == 1


def test_new_cycle_after_stop_clears_the_flag(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    first = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="first"),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_turn(_room, member):
        if not release.is_set():
            started.set()
            await release.wait()
            return True, "dropped"
        return True, "second take"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)

    async def scenario():
        task = asyncio.create_task(_run_locked(adapter, room, first))
        await started.wait()
        await adapter._stop_cycle(room.id, "Mark")
        release.set()
        await task
        later = adapter.store.append(
            room.id,
            RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="@scout again"),
        )
        await adapter._run_cycle_workspace(adapter.store.get(room.id) or room, later)

    asyncio.run(scenario())
    agents = [m.text for m in adapter.store.read_since(room.id, 0) if m.kind == KIND_AGENT]
    assert agents == ["second take"]


def test_stop_http_idle_and_missing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]

        def call(path, body):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            payload = json.dumps(body)
            conn.request(
                "POST",
                path,
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            status = resp.status
            conn.close()
            return status, data

        status, data = call("/rooms/r-1/stop", {"from": "Mark"})
        assert status == 200
        assert data == {"stopped": False, "idle": True}
        status, data = call("/rooms/ghost/stop", {"from": "Mark"})
        assert status == 404
        assert data["error"] == "no such room"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_no_op_turn_is_never_spoken_as_the_member(tmp_path, monkeypatch):
    """A member queued with nothing unseen must not apologise for it.

    Regression for novique-ai/retinue#132: the room posted
    FALLBACK_GENERIC as a KIND_AGENT line from the member, so a pure
    scheduling no-op read as the retainer failing at the work.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )
    # scout has already read everything, which is what makes the turn a no-op.
    adapter.store.touch_last_seen(room.id, "scout", user_message.seq)
    room = adapter.store.get(room.id)

    asyncio.run(_run_locked(adapter, room, user_message))

    kinds = _kinds(adapter.store, room.id)
    assert not any(kind == KIND_AGENT for kind, _speaker, _text in kinds)
    assert engine.FALLBACK_GENERIC not in [text for _k, _s, text in kinds]
    # Nothing was attempted, so the room does not announce a turn either.
    assert "scout is on it." not in _system_texts(adapter.store, room.id)


def test_timed_out_turn_speaks_in_the_member_voice(tmp_path, monkeypatch):
    """A turn that ran and never answered still has to speak.

    #133 made this a system-only notice so it would not be FALLBACK_GENERIC.
    That left Speak Replies silent and the human thinking the retainer
    ghosted. The retainer now posts TIMEOUT_REPLY (distinct from the
    empty-answer apology) and the room still records the exact reason.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"),
    )

    async def fake_turn(_room, member):
        return False, "no reply within 300s"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    kinds = _kinds(adapter.store, room.id)
    assert (KIND_AGENT, "scout", engine.TIMEOUT_REPLY) in kinds
    assert engine.FALLBACK_GENERIC not in [text for _k, _s, text in kinds]
    notice = engine.did_not_reply_notice("scout", "no reply within 300s")
    assert notice in _system_texts(adapter.store, room.id)
    assert engine.turn_concludes_waiter(
        RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker="scout", text=engine.TIMEOUT_REPLY),
        "scout",
    )
    assert engine.turn_concludes_waiter(
        RoomMessage(seq=0, ts=0, kind=KIND_SYSTEM, speaker="room", text=notice), "scout"
    )


def test_real_empty_answer_still_speaks_the_fallback(tmp_path, monkeypatch):
    """The apology keeps its real job: a turn that ran and returned nothing."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["scout"], lead="scout")
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="what is the status"),
    )

    async def fake_turn(_room, member):
        return True, "   "

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))

    assert (KIND_AGENT, "scout", engine.FALLBACK_GENERIC) in _kinds(adapter.store, room.id)


# ---------------------------------------------------------------------------
# Stop rotates the member session (#164)
# ---------------------------------------------------------------------------


class _FakeSessionStore:
    def __init__(self):
        self.reset_keys: list[str] = []

    async def reset_session(self, session_key):
        self.reset_keys.append(session_key)
        return object()


class _FakeRunner:
    def __init__(self):
        self.async_session_store = _FakeSessionStore()
        self.interrupted: list[str] = []
        self.evicted: list[str] = []

    async def _interrupt_and_clear_session(self, session_key, source, **kwargs):
        self.interrupted.append(session_key)

    def _evict_cached_agent(self, session_key):
        self.evicted.append(session_key)


def _register_pending(adapter, room, member: str, session_key: str):
    from concurrent.futures import Future

    from .adapter import _PendingTurn

    source = adapter.build_source(
        chat_id=room.id,
        chat_name=f"room:{room.name}",
        chat_type="group",
        user_id="user:test",
        user_name="Mark",
        thread_id=member,
    )
    pending = _PendingTurn(
        task_id="t-1",
        room_id=room.id,
        member=member,
        future=Future(),
        session_key=session_key,
        source=source,
    )
    with adapter._pending_lock:
        adapter._pending[(room.id, member)] = pending
    return pending


def test_stop_rotates_the_interrupted_members_session(tmp_path, monkeypatch):
    """Stop must drop the member's session history, not just the agent cache.

    The room store owns everything durable (transcript, watermark; the
    briefing is re-sent every turn), so the Hermes session's only cross-turn
    cargo is tool history. Before #164 the next turn rebuilt the agent FROM
    that history, so a poisoned 45-60k tool dump survived every Stop.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    runner = _FakeRunner()
    adapter.gateway_runner = runner
    _register_pending(adapter, room, "scout", "sess-scout-1")

    result = asyncio.run(adapter._stop_cycle(room.id, "Mark"))

    assert result["stopped"] is True
    assert runner.interrupted == ["sess-scout-1"]
    assert runner.async_session_store.reset_keys == ["sess-scout-1"]


def test_stop_rotation_failure_does_not_break_stop(tmp_path, monkeypatch):
    """A session store that cannot rotate must not turn Stop into an error."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    runner = _FakeRunner()

    async def broken_reset(session_key):
        raise RuntimeError("db down")

    runner.async_session_store.reset_session = broken_reset
    adapter.gateway_runner = runner
    _register_pending(adapter, room, "scout", "sess-scout-1")

    result = asyncio.run(adapter._stop_cycle(room.id, "Mark"))
    assert result["stopped"] is True


def test_reset_member_session_lever_while_idle(tmp_path, monkeypatch):
    """The explicit new-session lever works with no turn in flight (#164)."""
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    runner = _FakeRunner()
    adapter.gateway_runner = runner

    result = asyncio.run(adapter._reset_member_session(room.id, "scout"))

    assert result["reset"] is True
    assert result["member"] == "scout"
    assert len(runner.async_session_store.reset_keys) == 1
    # The rotated key is the same one an _agent_turn for this member derives.
    source = adapter.build_source(
        chat_id=room.id,
        chat_name=f"room:{room.name}",
        chat_type="group",
        user_id="agent:someone",
        user_name="someone (agent)",
        thread_id="scout",
    )
    source.profile = "scout"
    assert runner.async_session_store.reset_keys[0] == adapter._session_key_for(source)


def test_reset_member_session_refuses_while_busy(tmp_path, monkeypatch):
    from .adapter import AgentBusy

    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    runner = _FakeRunner()
    adapter.gateway_runner = runner
    _register_pending(adapter, room, "scout", "sess-scout-1")

    with pytest.raises(AgentBusy):
        asyncio.run(adapter._reset_member_session(room.id, "scout"))


def test_reset_member_session_unknown_room_and_member(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    adapter.gateway_runner = _FakeRunner()
    with pytest.raises(KeyError):
        asyncio.run(adapter._reset_member_session("ghost", "scout"))
    with pytest.raises(ValueError):
        asyncio.run(adapter._reset_member_session(room.id, "nobody"))
