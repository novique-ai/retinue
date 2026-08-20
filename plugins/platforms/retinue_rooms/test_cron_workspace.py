"""Scheduled jobs targeting a room inherit that room's workspace (#171)."""

from __future__ import annotations

import json

import pytest
from gateway.config import PlatformConfig

from tools import turn_env, workspace_context

from . import brokertoken, cron_workspace, ide
from .adapter import RetinueRoomsAdapter
from .engine import Room
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="beads-cleanup", name="Beads", members=["janitor"], lead="janitor")
    defaults.update(kwargs)
    return Room(**defaults)


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    inst = RetinueRoomsAdapter(PlatformConfig())
    inst.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    yield inst
    cron_workspace.uninstall()


def _job(room_id="beads-cleanup", member="janitor"):
    return {
        "id": "job-1",
        "name": "patrol",
        "deliver": "origin",
        "origin": {
            "platform": "retinue_rooms",
            "chat_id": room_id,
            "thread_id": member,
        },
        "retinue": {"kind": "routine", "room": room_id, "owner": member},
    }


def test_ide_job_sees_room_mount_and_broker_token(adapter, tmp_path, monkeypatch):
    ide_root = tmp_path / "ide"
    ide_root.mkdir()
    room = _room(workspace="ide", ide_path=str(ide_root))
    adapter.store.create(room)

    seen = {}

    def fake_run_job(job, *args, **kwargs):
        seen["volumes"] = workspace_context.getenv("TERMINAL_DOCKER_VOLUMES")
        seen["key"] = workspace_context.shared_container_key()
        mapping = turn_env.current() or {}
        seen["token"] = mapping.get(brokertoken.TOKEN_ENV, "")
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)

    assert sched.run_job(_job()) == (True, "ok", "ok", None)

    vols = json.loads(seen["volumes"])
    assert f"{ide_root}:/workspace:rw" in vols
    assert seen["key"] == ide.container_key_for_room(room)
    assert brokertoken.verify(str(tmp_path), seen["token"]) == "janitor"
    assert workspace_context.current() is None
    assert turn_env.current() is None


def test_job_with_no_room_does_not_bind_overlay(adapter, monkeypatch):
    adapter.store.create(_room(workspace="ide", ide_path=str(adapter.store.base_dir)))
    seen = {}

    def fake_run_job(job, *args, **kwargs):
        seen["overlay"] = workspace_context.current()
        seen["token"] = (turn_env.current() or {}).get(brokertoken.TOKEN_ENV, "")
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)

    assert sched.run_job({"id": "x", "name": "bare"}) == (True, "ok", "ok", None)
    assert seen["overlay"] is None
    assert seen["token"] == ""


def test_unknown_room_does_not_invent_a_workspace(adapter, monkeypatch):
    seen = {}

    def fake_run_job(job, *args, **kwargs):
        seen["overlay"] = workspace_context.current()
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)

    assert sched.run_job(_job(room_id="missing-room")) == (True, "ok", "ok", None)
    assert seen["overlay"] is None


def test_sandbox_job_stays_off_the_host_tree(adapter, tmp_path, monkeypatch):
    host = tmp_path / "host-ide"
    host.mkdir()
    room = _room(workspace="sandbox")
    adapter.store.create(room)
    seen = {}

    def fake_run_job(job, *args, **kwargs):
        seen["volumes"] = json.loads(workspace_context.getenv("TERMINAL_DOCKER_VOLUMES") or "[]")
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)

    assert sched.run_job(_job()) == (True, "ok", "ok", None)
    assert not any(v.startswith(str(host)) for v in seen["volumes"])
    assert not any(v.endswith(":/workspace:rw") for v in seen["volumes"])


def test_uninstall_restores_original(adapter, monkeypatch):
    calls = []

    def fake_run_job(job, *args, **kwargs):
        calls.append("inner")
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)
    cron_workspace.uninstall()

    sched.run_job({"id": "x"})
    assert calls == ["inner"]
    assert cron_workspace._original_run_job is None
