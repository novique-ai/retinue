"""Cron deliver=origin must land on the room transcript (issue #36)."""

from __future__ import annotations

import asyncio
import threading

import pytest
from gateway.config import GatewayConfig, Platform, PlatformConfig

from .adapter import RetinueRoomsAdapter
from .engine import KIND_AGENT, Room
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="novique-demo", name="Demo", members=["sally", "editor"], lead="sally")
    defaults.update(kwargs)
    return Room(**defaults)


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    inst = RetinueRoomsAdapter(PlatformConfig())
    inst.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    inst.store.create(_room())
    return inst


@pytest.fixture
def gateway_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        loop.close()


@pytest.mark.asyncio
async def test_cron_job_send_appends_member_line(adapter):
    result = await adapter.send(
        "novique-demo",
        "FB/IG walkthrough is ready.",
        metadata={"job_id": "868ee7b5b3f6", "thread_id": "sally"},
    )
    assert result.success is True
    lines = adapter.store.read_since("novique-demo", 0)
    assert len(lines) == 1
    assert lines[0].kind == KIND_AGENT
    assert lines[0].speaker == "sally"
    assert lines[0].text == "FB/IG walkthrough is ready."


@pytest.mark.asyncio
async def test_progress_send_without_job_id_stays_off_transcript(adapter):
    result = await adapter.send(
        "novique-demo",
        "thinking…",
        metadata={"thread_id": "sally"},
    )
    assert result.success is True
    assert adapter.store.read_since("novique-demo", 0) == []


@pytest.mark.asyncio
async def test_cron_send_unknown_room_fails(adapter):
    result = await adapter.send(
        "missing-room",
        "hello",
        metadata={"job_id": "job-1", "thread_id": "sally"},
    )
    assert result.success is False
    assert adapter.store.read_since("novique-demo", 0) == []


@pytest.mark.asyncio
async def test_notify_still_resolves_pending_and_does_not_double_append(adapter):
    from concurrent.futures import Future

    from .adapter import _PendingTurn

    fut: Future = Future()
    with adapter._pending_lock:
        adapter._pending[("novique-demo", "sally")] = _PendingTurn(
            task_id="t1",
            room_id="novique-demo",
            member="sally",
            future=fut,
        )
    result = await adapter.send(
        "novique-demo",
        "turn reply",
        metadata={"notify": True, "thread_id": "sally"},
    )
    assert result.success is True
    assert fut.done()
    assert fut.result() == (True, "turn reply")
    assert adapter.store.read_since("novique-demo", 0) == []


def test_origin_delivery_lands_when_profile_has_no_platform_config(
    adapter, gateway_loop, monkeypatch
):
    from cron.scheduler import _deliver_result

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: GatewayConfig())
    job = {
        "id": "job-1",
        "name": "Morning brief",
        "deliver": "origin",
        "origin": {
            "platform": "retinue_rooms",
            "chat_id": "novique-demo",
            "thread_id": "sally",
        },
    }

    error = _deliver_result(
        job,
        "The walkthrough is ready.",
        adapters={Platform("retinue_rooms"): adapter},
        loop=gateway_loop,
    )

    assert error is None, (
        "a live rooms adapter without a profile platform block was rejected as "
        f"platform 'retinue_rooms' not configured/enabled: {error}"
    )
    lines = adapter.store.read_since("novique-demo", 0)
    assert len(lines) == 1
    assert lines[0].kind == KIND_AGENT
    assert lines[0].speaker == "sally"
    assert "The walkthrough is ready." in lines[0].text


def test_origin_delivery_still_blocked_when_platform_explicitly_disabled(
    adapter, monkeypatch
):
    from cron.scheduler import _deliver_result

    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: GatewayConfig(
            platforms={
                Platform("retinue_rooms"): PlatformConfig(enabled=False),
            }
        ),
    )
    job = {
        "id": "job-1",
        "name": "Morning brief",
        "deliver": "origin",
        "origin": {
            "platform": "retinue_rooms",
            "chat_id": "novique-demo",
            "thread_id": "sally",
        },
    }

    error = _deliver_result(job, "blocked", adapters={}, loop=None)

    assert "not configured/enabled" in (error or "")
    assert adapter.store.read_since("novique-demo", 0) == []
