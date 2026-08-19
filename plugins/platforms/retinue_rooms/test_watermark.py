"""Watermarked delta: failed turns re-see unread; pass/speak stick; cap+elide.

Issue #142. Run:
  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_watermark.py -q
"""

from __future__ import annotations

import asyncio

from gateway.config import PlatformConfig

from . import engine, hire
from .adapter import RetinueRoomsAdapter
from .engine import KIND_AGENT, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _adapter(tmp_path, monkeypatch) -> RetinueRoomsAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _fill(store: RoomStore, room_id: str, n: int, start: int = 1) -> None:
    for i in range(start, start + n):
        store.append(
            room_id,
            RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text=f"line-{i}"),
        )


async def _run_cycle(adapter, room, user_message):
    async with adapter._room_lock(room.id):
        await adapter._run_cycle_workspace(room, user_message)


def _resolve(adapter, room, member, ok, text):
    adapter._resolve_pending(room.id, ok=ok, text=text, member=member)


def test_failed_turn_restores_watermark_and_resees_delta(tmp_path, monkeypatch):
    """A timeout/dispatch failure must not eat the unread slice.

    The next cycle injects the same trigger (plus whatever landed after
    the failed attempt) — the member is not waiting for a new post.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(max_followup_rounds=0))
    first = adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="please look"),
    )
    room = adapter.store.get("r-1")
    assert room.last_seen.get("scout", 0) == 0

    events: list = []
    calls = {"n": 0}

    async def fake_handle(event):
        events.append(event)
        calls["n"] += 1
        _resolve(adapter, room, "scout", False, "turn timed out")

    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    asyncio.run(_run_cycle(adapter, room, first))

    after_fail = adapter.store.get("r-1")
    assert after_fail.last_seen.get("scout", 0) == 0
    assert events[0].text == "please look"

    second = adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="still waiting"),
    )
    room = adapter.store.get("r-1")
    asyncio.run(_run_cycle(adapter, room, second))

    assert calls["n"] == 2
    retry = events[1]
    assert retry.text == "still waiting"
    context = retry.channel_context or ""
    assert "[You] please look" in context


def test_timeout_inside_agent_turn_restores_cursor(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="hello"),
    )
    room = adapter.store.get("r-1")
    monkeypatch.setattr(hire, "turn_timeout_for", lambda *_a, **_k: 0.05)

    async def hang(_event):
        await asyncio.sleep(2)

    monkeypatch.setattr(adapter, "handle_message", hang)
    ok, text = asyncio.run(adapter._agent_turn(room, "scout"))
    assert ok is False
    assert "no reply within" in text
    stored = adapter.store.get("r-1")
    assert stored.last_seen.get("scout", 0) == 0


def test_completed_pass_advances_watermark(tmp_path, monkeypatch):
    """An explicit pass is a completed turn — the cursor sticks."""
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(max_followup_rounds=0))
    user_message = adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="status?"),
    )
    room = adapter.store.get("r-1")

    async def fake_handle(event):
        _resolve(adapter, room, "scout", True, engine.pass_payload_text())

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    asyncio.run(_run_cycle(adapter, room, user_message))

    stored = adapter.store.get("r-1")
    head = max(m.seq for m in adapter.store.read_since("r-1", 0))
    assert stored.last_seen["scout"] == head
    room = stored
    assert adapter._unseen_delta(room, "scout") == []


def test_completed_speak_advances_watermark(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(max_followup_rounds=0))
    user_message = adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="hello"),
    )
    room = adapter.store.get("r-1")

    async def fake_handle(event):
        _resolve(adapter, room, "scout", True, "hi back")

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    asyncio.run(_run_cycle(adapter, room, user_message))

    stored = adapter.store.get("r-1")
    # The cursor covers what was shown, not the member's own later reply
    # (own lines are filtered from _unseen anyway).
    shown = [
        m
        for m in adapter.store.read_since("r-1", 0)
        if not (m.kind == KIND_AGENT and m.speaker == "scout")
    ]
    assert stored.last_seen["scout"] == shown[-1].seq
    assert adapter._unseen_delta(stored, "scout") == []


def test_delta_cap_elides_with_notice_on_injection(tmp_path, monkeypatch):
    """A long idle injects the newest window plus a compact omit line."""
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room())
    extra = 5
    total = engine.DELTA_TRANSCRIPT_WINDOW + extra
    _fill(adapter.store, "r-1", total)
    room = adapter.store.get("r-1")
    captured = {}

    async def fake_handle(event):
        captured["text"] = event.text
        captured["context"] = event.channel_context or ""
        _resolve(adapter, room, "scout", True, "caught up")

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    ok, _text = asyncio.run(adapter._agent_turn(room, "scout"))
    assert ok is True
    assert captured["text"] == f"line-{total}"
    context = captured["context"]
    notice = f"[room] {engine.omitted_delta_notice(extra)}"
    ctx_lines = context.splitlines()
    assert ctx_lines[0] == notice
    assert f"[You] line-1" not in ctx_lines
    assert f"[You] line-{extra}" not in ctx_lines
    assert f"[You] line-{extra + 1}" in ctx_lines
    assert f"[You] line-{total - 1}" in ctx_lines
    # Trigger is event.text, not repeated as the last context line.
    assert f"[You] line-{total}" not in ctx_lines


def test_invite_seeding_unchanged_no_elision_on_first_turn(tmp_path, monkeypatch):
    """seed_invite_last_seen still windows; first turn does not double-elide."""
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(_room(members=["scout", "editor"], lead="scout"))
    _fill(adapter.store, "r-1", 25)
    adapter.add_room_member("r-1", "critic")
    room = adapter.store.get("r-1")
    unseen = adapter.store.read_since("r-1", room.last_seen["critic"])
    assert len(unseen) == engine.INVITE_TRANSCRIPT_WINDOW
    assert room.last_seen["critic"] == max(0, unseen[-1].seq - engine.INVITE_TRANSCRIPT_WINDOW)

    captured = {}

    async def fake_handle(event):
        captured["text"] = event.text
        captured["context"] = event.channel_context or ""
        _resolve(adapter, room, "critic", True, engine.pass_payload_text())

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    ok, _text = asyncio.run(adapter._agent_turn(room, "critic"))
    assert ok is True
    assert "earlier messages omitted" not in captured["context"]
    assert captured["text"] == engine.member_joined_notice("critic")
