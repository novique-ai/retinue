"""Principal @mention is a needs_user scheduling barrier (issues #141, #243).

Run:
  scripts/run_tests.sh plugins/platforms/retinue_rooms/test_needs_user.py
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from gateway.config import PlatformConfig

from . import engine, ide, principal, tools
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
    assert engine.mentions_principal("@Riley this needs you", "Riley")
    assert engine.mentions_principal("@Alex the invoice is ready", "Alex Rivera")
    assert not engine.mentions_principal("@Rivera the invoice is ready", "Alex Rivera")


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


def test_briefing_leads_with_the_named_principal_handle():
    """The configured handle is how to escalate. @user and @you stay aliases."""
    room = _room()
    named = engine.room_briefing(
        room, "scout", ["Ada Lovelace"], principal_name="Ada Lovelace"
    )
    assert "flags the room as needing them" in named
    clause = named[named.index("Escalate a real judgment call with ") :]
    assert clause.startswith("Escalate a real judgment call with @Ada ")
    assert clause.index("@Ada") < clause.index("@user")
    assert "@you" in clause.split(";", 1)[0]
    generic = engine.room_briefing(room, "scout", ["You"])
    assert "Escalate a real judgment call with @user;" in generic
    assert "or @You" not in generic
    assert "@Ada" not in generic


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


def test_non_principal_user_line_does_not_clear_needs_user():
    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "ping", speaker="Room System"), name="Ada Lovelace")
    assert room.needs_user is True
    # A short signed name still counts as the principal.
    _apply(room, _msg(KIND_USER, "use the first one", speaker="Ada"), name="Ada Lovelace")
    assert room.needs_user is False


def test_cycle_blocked_when_escalation_follows_the_trigger():
    """The flag can already be clear; a later escalation still discards the cycle."""
    members = ["scout", "editor"]
    trigger = RoomMessage(
        seq=92, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="status?"
    )
    later = [
        RoomMessage(seq=94, ts=0, kind=KIND_USER, speaker="Room System", text="ping"),
        RoomMessage(
            seq=99, ts=0, kind=KIND_AGENT, speaker="scout", text="@Ada I need a decision"
        ),
        RoomMessage(
            seq=100, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="carry on"
        ),
    ]
    assert engine.cycle_blocked_by_principal(
        False, trigger, later, principal_name="Ada Lovelace", members=members
    )
    fresh = later[-1]
    assert not engine.cycle_blocked_by_principal(
        False, fresh, [], principal_name="Ada Lovelace", members=members
    )
    # Barrier still up: a non-principal line posted after the escalation
    # does not start a cycle either.
    ping = RoomMessage(
        seq=101, ts=0, kind=KIND_USER, speaker="Room System", text="later ping"
    )
    assert engine.cycle_blocked_by_principal(
        True, ping, [], principal_name="Ada Lovelace", members=members
    )
    fenced = [
        RoomMessage(
            seq=99,
            ts=0,
            kind=KIND_AGENT,
            speaker="scout",
            text="draft:\n```\nAsk @Ada in the body.\n```\n",
        )
    ]
    assert not engine.cycle_blocked_by_principal(
        False, trigger, fenced, principal_name="Ada Lovelace", members=members
    )


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


# ── scheduling barrier (issue #243) ──────────────────────────────────────


def _principal(tmp_path, name: str = "Ada Lovelace") -> None:
    principal.save(str(tmp_path), {"display_name": name, "about": ""})


def test_principal_mention_stops_later_speakers(tmp_path, monkeypatch):
    """A spoken principal mention ends the cycle; the next planned speaker waits."""
    adapter = _adapter(tmp_path, monkeypatch)
    _principal(tmp_path)
    room = _room(members=["scout", "editor"], lead="scout", max_followup_rounds=3)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(
            seq=0, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="@scout @editor go"
        ),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "@Ada I need a decision before anyone else continues."
        return True, "editor should not start"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert calls == ["scout"]
    assert adapter.store.get(room.id).needs_user is True


def test_principal_mention_skips_followup_rounds(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _principal(tmp_path)
    room = _room(members=["scout", "editor"], lead="scout", max_followup_rounds=3)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(
            seq=0, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="what do you think?"
        ),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "@user I need you to pick."
        return True, engine.pass_payload_text()

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert calls == ["scout"]
    assert adapter.store.get(room.id).needs_user is True


def test_fenced_principal_mention_does_not_stop_the_cycle(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _principal(tmp_path)
    room = _room(members=["scout", "editor"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="@scout go"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "draft:\n```\nAsk @Ada in the body.\n```\n@editor please edit."
        return True, "edited"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert calls == ["scout", "editor"]
    assert adapter.store.get(room.id).needs_user is False


def test_member_mention_still_continues_the_cycle(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _principal(tmp_path)
    room = _room(members=["scout", "editor"], lead="scout", max_followup_rounds=0)
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Ada Lovelace", text="@scout go"),
    )
    calls: list[str] = []

    async def fake_turn(_room, member):
        calls.append(member)
        if member == "scout":
            return True, "@editor please tighten this."
        return True, "tightened"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
    asyncio.run(_run_locked(adapter, room, user_message))
    assert calls == ["scout", "editor"]
    assert adapter.store.get(room.id).needs_user is False


def test_non_principal_post_does_not_clear_or_schedule(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _principal(tmp_path)
    adapter.store.create(_room(needs_user=True, members=["scout"], lead="scout"))
    loop = asyncio.new_event_loop()
    adapter._loop = loop
    scheduled: list = []

    def capture(coro, _loop):
        scheduled.append(coro)
        coro.close()

        class _Fut:
            def result(self, timeout=None):
                return None

        return _Fut()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", capture)
    try:
        adapter.post_user_message("r-1", "ping", "Room System")
        assert scheduled == []
        assert adapter.store.get("r-1").needs_user is True
        posted = adapter.store.read_since("r-1", 0)
        assert [(m.kind, m.speaker, m.text) for m in posted] == [
            (KIND_USER, "Room System", "ping")
        ]
        adapter.post_user_message("r-1", "I decided", "You")
        assert adapter.store.get("r-1").needs_user is False
        assert len(scheduled) == 1
        assert adapter.store.read_since("r-1", 0)[-1].speaker == "Ada Lovelace"
    finally:
        loop.close()


def test_queued_cycle_after_escalation_never_replays(tmp_path, monkeypatch):
    """Cycle B queued behind the lock is dropped even if the principal clears first.

    Reproduces the two-cycle failure: trigger A, a non-principal user line
    queues B, A's reply @mentions the principal, the principal replies
    before B acquires the lock. B must not start. The principal's own
    cycle may.
    """

    async def scenario():
        adapter = _adapter(tmp_path, monkeypatch)
        _principal(tmp_path)
        adapter._loop = asyncio.get_running_loop()

        @contextmanager
        def _no_workspace(*_args, **_kwargs):
            yield {}

        # The barrier decision is in front of workspace binding. Skip the
        # docker env publish so this test stays a scheduler test.
        monkeypatch.setattr(ide, "apply_room_workspace", _no_workspace)
        scheduled: list = []

        def _schedule(coro, loop):
            scheduled.append(asyncio.ensure_future(coro, loop=loop))

            class _Done:
                def result(self, timeout=None):
                    return None

            return _Done()

        # post_user_message hops to the gateway loop. On this loop, queue
        # the cycle as a task so it waits on the room lock deterministically.
        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _schedule)
        room = _room(members=["scout", "editor"], lead="scout", max_followup_rounds=0)
        adapter.store.create(room)
        calls: list[str] = []
        started: list[str] = []
        flag_around_reply: list[bool] = []

        original_workspace = adapter._run_cycle_workspace

        async def trace_workspace(room_obj, user_message):
            started.append(user_message.text)
            await original_workspace(room_obj, user_message)

        adapter._run_cycle_workspace = trace_workspace  # type: ignore[method-assign]

        original_note = adapter._note_posted

        def note(room_id, message):
            original_note(room_id, message)
            if message.kind == KIND_AGENT and "@Ada" in (message.text or ""):
                flag_around_reply.append(bool(adapter.store.get(room_id).needs_user))
                adapter.post_user_message(room_id, "carry on", "Ada Lovelace")
                flag_around_reply.append(bool(adapter.store.get(room_id).needs_user))

        adapter._note_posted = note  # type: ignore[method-assign]

        async def fake_turn(_room, member):
            calls.append(member)
            if member == "scout" and calls.count("scout") == 1:
                adapter.post_user_message(room.id, "ping", "Room System")
                await asyncio.sleep(0)
                return True, "@Ada I need a decision before anyone else continues."
            return True, "answering the principal's fresh cycle"

        monkeypatch.setattr(adapter, "_agent_turn", fake_turn)
        trigger = adapter.store.append(
            room.id,
            RoomMessage(
                seq=0,
                ts=0,
                kind=KIND_USER,
                speaker="Ada Lovelace",
                text="@scout @editor status?",
            ),
        )
        await adapter._run_cycle(room.id, trigger)
        pending = [task for task in scheduled if not task.done()]
        if pending:
            await asyncio.wait(pending)
        for task in scheduled:
            task.result()

        assert calls == ["scout", "scout"]
        assert "editor" not in calls
        assert started == ["@scout @editor status?", "carry on"]
        assert "ping" not in started
        assert flag_around_reply == [True, False]
        # "carry on" cleared the escalation, so scout's answer is owed back
        # to the principal and re-raises the flag (#246).
        assert adapter.store.get(room.id).needs_user is True
        speakers = [
            m.speaker
            for m in adapter.store.read_since(room.id, 0)
            if m.kind == KIND_AGENT
        ]
        assert speakers == ["scout", "scout"]
        user_lines = [
            (m.speaker, m.text)
            for m in adapter.store.read_since(room.id, 0)
            if m.kind == KIND_USER
        ]
        assert ("Room System", "ping") in user_lines

    asyncio.run(scenario())


# ── owed reply (#246): an answer back to the principal re-raises the flag ──


def test_answer_to_principal_follow_up_re_raises_without_a_mention():
    room = _room()
    _apply(room, _msg(KIND_AGENT, "@user which option?"))
    assert room.needs_user is True
    _apply(room, _msg(KIND_USER, "Explain the options in more detail.", speaker="Clayton"))
    assert room.needs_user is False
    _apply(room, _msg(KIND_AGENT, "Here is the detail, Clayton: option A is ..."))
    assert room.needs_user is True


def test_owed_reply_handed_to_a_member_does_not_raise():
    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "Go with A.", speaker="Clayton"))
    _apply(room, _msg(KIND_AGENT, "@editor please implement option A."))
    assert room.needs_user is False
    # The owed reply was consumed by the handoff; later lines are ordinary.
    _apply(room, _msg(KIND_AGENT, "Done: option A shipped.", speaker="editor"))
    assert room.needs_user is False


def test_principal_post_that_cleared_nothing_owes_no_reply():
    room = _room()
    _apply(room, _msg(KIND_USER, "@scout summarise the backlog", speaker="Clayton"))
    _apply(room, _msg(KIND_AGENT, "Backlog: three items."))
    assert room.needs_user is False


def test_owed_reply_is_consumed_once():
    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "More detail please.", speaker="Clayton"))
    _apply(room, _msg(KIND_AGENT, "Detail: ..."))
    assert room.needs_user is True
    _apply(room, _msg(KIND_USER, "Thanks, go ahead.", speaker="Clayton"))
    assert room.needs_user is False
    _apply(room, _msg(KIND_AGENT, "Detail again: ..."))
    # Cleared again by the principal, so the next answer is owed again.
    assert room.needs_user is True
    room.needs_user = False
    _apply(room, _msg(KIND_AGENT, "An unrelated status line."))
    assert room.needs_user is False


def test_non_principal_user_line_does_not_create_an_owed_reply():
    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "ping", speaker="Room System"))
    room.needs_user = False
    _apply(room, _msg(KIND_AGENT, "Status: fine."))
    assert room.needs_user is False


def test_owed_reply_roundtrips_and_stays_out_of_composition():
    from .store import COMPOSITION_FIELDS

    room = _room(needs_user=True)
    _apply(room, _msg(KIND_USER, "More detail please.", speaker="Clayton"))
    again = Room.from_dict(room.to_dict())
    assert again.needs_user_reply is True
    assert "needs_user_reply" not in COMPOSITION_FIELDS
