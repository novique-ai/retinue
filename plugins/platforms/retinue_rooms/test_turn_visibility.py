"""Provider stalls surface on the transcript (novique-ai/retinue#166).

On 2026-08-20 a member's turn absorbed five provider kills (SSE stall
watchdog, TTFB timeout, an explicit xAI capacity rejection) over 21 minutes
while the transcript showed only "X is on it." — the retry loop lives in
agent.conversation_loop and had no way to tell the room. tools/turn_visibility
is the carried-patch seam: the rooms adapter binds a per-turn notifier
(ContextVar, same propagation contract as tools.turn_env), and the
conversation loop's retry site calls notify() with a compact summary.
Payloads never reach the transcript — summaries only.
"""

from __future__ import annotations

import os

from gateway.config import PlatformConfig
from tools import turn_visibility

from . import engine
from .adapter import RetinueRoomsAdapter
from .engine import KIND_SYSTEM, Room
from .store import RoomStore

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Vis", members=["scout"], lead="scout")
    defaults.update(kwargs)
    return Room(**defaults)


def _system_texts(store, room_id):
    return [m.text for m in store.read_since(room_id, 0) if m.kind == KIND_SYSTEM]


# --- module seam ------------------------------------------------------------


def test_notify_without_a_notifier_is_a_silent_noop():
    turn_visibility.notify("nobody is listening")  # must not raise


def test_notifier_binds_and_resets():
    seen: list[str] = []
    token = turn_visibility.set_notifier(seen.append)
    try:
        turn_visibility.notify("first")
    finally:
        turn_visibility.reset(token)
    turn_visibility.notify("after reset")
    assert seen == ["first"]


def test_a_broken_notifier_never_raises_into_the_turn():
    def explode(_msg: str) -> None:
        raise RuntimeError("boom")

    token = turn_visibility.set_notifier(explode)
    try:
        turn_visibility.notify("stall")  # must not raise
    finally:
        turn_visibility.reset(token)


# --- adapter notifier -> system line ----------------------------------------


def test_adapter_notifier_posts_a_compact_system_line(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    notify = adapter._provider_event_notifier(room.id, "scout")
    notify("provider call failed (Broken pipe); retrying (attempt 1/3)")
    texts = _system_texts(adapter.store, room.id)
    assert len(texts) == 1
    assert "Broken pipe" in texts[0]
    assert "scout" in texts[0].lower() or "Scout" in texts[0]


def test_adapter_notifier_rate_limits_per_turn(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room()
    adapter.store.create(room)
    notify = adapter._provider_event_notifier(room.id, "scout")
    notify("first stall")
    notify("second stall inside the window")
    assert len(_system_texts(adapter.store, room.id)) == 1


def test_notice_truncates_long_provider_detail():
    text = engine.provider_event_notice("Scout", "x" * 1000)
    assert len(text) < 400
    assert "Scout" in text


# --- wire-in drift guards (test_carried_patches.py precedent) ---------------


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_conversation_loop_calls_the_visibility_seam():
    src = _read(os.path.join("agent", "conversation_loop.py"))
    assert "turn_visibility" in src, (
        "agent/conversation_loop.py no longer notifies tools.turn_visibility "
        "on API retry — an upstream sync clobbered the #166 carried patch"
    )


def test_agent_turn_binds_the_notifier():
    src = _read(os.path.join("plugins", "platforms", "retinue_rooms", "adapter.py"))
    assert "turn_visibility" in src and "_provider_event_notifier" in src
