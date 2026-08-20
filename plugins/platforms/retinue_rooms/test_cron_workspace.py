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


# ── review follow-ups: the three bugs, each asserted both ways ────────────
#
# Every test above replaces sched.run_job with a stub BEFORE install, so the
# real run_job never sits on the path and the real keyword-only signature is
# never exercised. That is how all three of these shipped green. The cases
# below assert the refusal/fallthrough side, which the originals could not.


def _ide_room(adapter, tmp_path, **kw):
    ide_root = tmp_path / "ide"
    ide_root.mkdir(exist_ok=True)
    room = _room(workspace="ide", ide_path=str(ide_root), **kw)
    adapter.store.create(room)
    return room


def _install_probe(adapter, monkeypatch, seen):
    """Stub carrying run_job's real keyword-only parameters."""

    def fake_run_job(
        job,
        *args,
        defer_agent_teardown=None,
        extra_prompt=None,
        cancel_event=None,
        **kwargs,
    ):
        seen["volumes"] = workspace_context.getenv("TERMINAL_DOCKER_VOLUMES")
        seen["token"] = (turn_env.current() or {}).get(brokertoken.TOKEN_ENV, "")
        seen["kwargs"] = {
            "defer_agent_teardown": defer_agent_teardown,
            "extra_prompt": extra_prompt,
            "cancel_event": cancel_event,
        }
        seen["ran"] = True
        return True, "ok", "ok", None

    import cron.scheduler as sched

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)
    return sched


# --- bug 1: membership is the authorization model -------------------------


def test_non_member_owner_gets_no_mount_and_no_token(adapter, tmp_path, monkeypatch):
    # create_cron_job checks the room exists and the owner is served — never
    # that the owner is IN the room. Without this gate the wrap hands an
    # unchecked slug the room's rw bind-mount and a real broker credential.
    _ide_room(adapter, tmp_path, members=["janitor"])
    seen = {}
    sched = _install_probe(adapter, monkeypatch, seen)

    assert sched.run_job(_job(member="stranger")) == (True, "ok", "ok", None)
    assert seen["ran"] is True, "the job must still run, just unprivileged"
    assert not seen["volumes"], "a non-member was given the room mount"
    assert not seen["token"], "a non-member was minted a broker token"


def test_member_owner_still_gets_both(adapter, tmp_path, monkeypatch):
    # The gate must not simply refuse everything.
    _ide_room(adapter, tmp_path, members=["janitor"])
    seen = {}
    sched = _install_probe(adapter, monkeypatch, seen)

    sched.run_job(_job(member="janitor"))
    assert seen["volumes"], "a roster member lost the room mount"
    assert seen["token"], "a roster member lost the broker token"


def test_identity_comes_from_retinue_owner_not_a_foreign_thread_id(
    adapter, tmp_path, monkeypatch
):
    # _room_for also resolves a room from retinue.room / a deliver token, where
    # origin.thread_id belongs to another platform. Preferring thread_id would
    # mint a credential named after a Discord thread.
    _ide_room(adapter, tmp_path, members=["janitor"])
    job = _job(member="janitor")
    job["origin"] = {"platform": "discord", "chat_id": "x", "thread_id": "99887766"}
    assert cron_workspace._member_for(job) == "janitor"

    # …and with no rooms origin and no owner, there is no identity to use.
    job["retinue"] = {"kind": "routine", "room": "beads-cleanup"}
    assert cron_workspace._member_for(job) == ""


# --- bug 2: TERMINAL_CWD is process-global and cron owns the lock ---------


def test_cron_path_does_not_publish_terminal_cwd(adapter, tmp_path, monkeypatch):
    # run_job snapshots _prior_terminal_cwd AFTER we return, so publishing here
    # both writes outside _terminal_cwd_lock and poisons that restore — the
    # job's finally would put /workspace back instead of the real prior value.
    monkeypatch.setenv("TERMINAL_CWD", "/sentinel")
    _ide_room(adapter, tmp_path, members=["janitor"])
    seen = {}

    import os

    import cron.scheduler as sched

    def fake_run_job(job, *args, **kwargs):
        seen["cwd_during"] = os.environ.get("TERMINAL_CWD")
        seen["volumes"] = workspace_context.getenv("TERMINAL_DOCKER_VOLUMES")
        return True, "ok", "ok", None

    # Install exactly once, over this stub — install() captures whatever
    # run_job is bound at that moment as the inner call.
    monkeypatch.setattr(sched, "run_job", fake_run_job)
    cron_workspace.install(adapter)

    sched.run_job(_job())
    assert seen["cwd_during"] == "/sentinel", (
        "the cron path published TERMINAL_CWD process-wide"
    )
    # The overlay itself must still be bound — this is a narrowing, not a skip.
    assert seen["volumes"], "suppressing the env write also lost the overlay"


def test_gateway_path_still_publishes(adapter, tmp_path, monkeypatch):
    # The narrowing is opt-in; the gateway loop owns the process and still
    # publishes, or mention turns would lose TERMINAL_CWD.
    monkeypatch.setenv("TERMINAL_CWD", "/sentinel")
    room = _ide_room(adapter, tmp_path, members=["janitor"])
    import os

    with ide.apply_room_workspace(room, str(tmp_path)):
        assert os.environ.get("TERMINAL_CWD") == ide.CONTAINER_MOUNT


# --- bug 3: a broken overlay must not kill the job ------------------------


def test_overlay_failure_falls_through_instead_of_killing_the_job(
    adapter, tmp_path, monkeypatch
):
    # apply_room_workspace raises on a stale ide_path. It raised BEFORE inner,
    # so the job never ran at all — a previously-working routine turned into a
    # hard failure.
    room = _ide_room(adapter, tmp_path, members=["janitor"])
    room.ide_path = str(tmp_path / "gone")
    adapter.store.update(room)

    seen = {}
    sched = _install_probe(adapter, monkeypatch, seen)

    assert sched.run_job(_job()) == (True, "ok", "ok", None)
    assert seen["ran"] is True, "a broken overlay killed the job"
    assert not seen["volumes"], "a failed overlay reported a mount anyway"


# --- the signature the originals never exercised -------------------------


def test_keyword_only_arguments_reach_the_wrapped_run_job(
    adapter, tmp_path, monkeypatch
):
    _ide_room(adapter, tmp_path, members=["janitor"])
    seen = {}
    sched = _install_probe(adapter, monkeypatch, seen)

    sentinel = object()
    sched.run_job(
        _job(), defer_agent_teardown=True, extra_prompt="go", cancel_event=sentinel
    )
    assert seen["kwargs"] == {
        "defer_agent_teardown": True,
        "extra_prompt": "go",
        "cancel_event": sentinel,
    }


def test_a_job_with_no_room_is_untouched(adapter, tmp_path, monkeypatch):
    seen = {}
    sched = _install_probe(adapter, monkeypatch, seen)
    job = {"id": "job-2", "name": "plain", "deliver": "discord:123"}

    assert sched.run_job(job) == (True, "ok", "ok", None)
    assert seen["ran"] is True
    assert not seen["volumes"]
    assert not seen["token"]
