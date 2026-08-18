"""HTTP contracts for the Retinue scheduled-jobs API."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from gateway.config import PlatformConfig

from . import cronjobs
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import KIND_USER, Room, RoomMessage
from .store import RoomStore


class _Provider:
    name = "test"

    def register_job(self, _job):
        return None

    def on_jobs_changed(self):
        return None


def _config(multiplex=False, allowlist=None):
    return SimpleNamespace(
        multiplex_profiles=multiplex,
        multiplex_profile_allowlist=allowlist,
    )


@contextmanager
def _running(adapter):
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[:2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(address, method, path, body=None, key=None):
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    connection = http.client.HTTPConnection(*address, timeout=30)
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read().decode())
    status = response.status
    connection.close()
    return status, data


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(False))
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: _Provider()
    )
    instance = RetinueRoomsAdapter(PlatformConfig())
    instance.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    instance.store.create(Room(id="room-a", name="Room A", members=["default"], lead="default"))
    return instance


def _create(address, **overrides):
    body = {
        "owner": "default",
        "name": "Brief",
        "schedule": "every 1h",
        "room": "room-a",
        "prompt": "Prepare it",
    }
    body.update(overrides)
    return _request(address, "POST", "/cron/jobs", body)


def test_cron_http_lifecycle(adapter):
    with _running(adapter) as address:
        status, created = _create(address)
        assert status == 201
        job_id = created["id"]
        status, listing = _request(address, "GET", "/cron/jobs")
        assert status == 200 and listing["owners"] == ["default"]
        assert listing["jobs"][0]["id"] == job_id
        assert "next_run_at" in listing["jobs"][0]
        assert "last_run_at" in listing["jobs"][0]
        assert _request(address, "GET", "/cron/jobs?owner=default")[0] == 200
        assert _request(address, "GET", "/cron/jobs?room=room-a")[1]["jobs"]
        assert _request(address, "GET", "/rooms/room-a/cron/jobs")[0] == 200
        assert _request(address, "GET", "/rooms/missing/cron/jobs")[0] == 404
        status, patched = _request(
            address, "PATCH", f"/cron/jobs/{job_id}", {"name": "Changed", "schedule": "every 2h"}
        )
        assert status == 200 and patched["schedule_display"] == "every 120m"
        assert _request(address, "POST", f"/cron/jobs/{job_id}/pause", {})[1]["state"] == "paused"
        assert _request(address, "POST", f"/cron/jobs/{job_id}/resume", {})[1]["state"] == "scheduled"
        assert _request(address, "POST", f"/cron/jobs/{job_id}/run", {})[0] == 200
        status, deleted = _request(address, "DELETE", f"/cron/jobs/{job_id}")
        assert status == 200 and deleted == {"deleted": job_id, "routine_slug": None}
        assert _request(address, "DELETE", f"/cron/jobs/{job_id}")[0] == 404


def test_cron_create_validation(adapter):
    with _running(adapter) as address:
        assert _create(address, prompt="", skill="")[0] == 400
        assert _create(address, room="")[0] == 400
        assert _create(address, room="missing")[0] == 404
        assert _create(address, owner="missing")[0] == 404
        assert _request(address, "GET", "/cron/nope")[0] == 404


def test_patch_a_roomless_job_over_http(adapter):
    from cron import jobs as cron_jobs

    with cronjobs.scoped(adapter._home_dir(), "default"):
        job = cron_jobs.create_job(
            prompt="hello", schedule="every 1h", name="Roomless",
            deliver="local", origin={"platform": "cli"},
        )
    with _running(adapter) as address:
        status, row = _request(
            address, "PATCH", f"/cron/jobs/{job['id']}",
            {"name": "Changed", "schedule": "every 2h"},
        )
    assert status == 200
    assert row["room"] is None


def test_patch_with_empty_room_is_400(adapter):
    with _running(adapter) as address:
        _, row = _create(address)
        status, body = _request(address, "PATCH", f"/cron/jobs/{row['id']}", {"room": ""})
    assert status == 400
    assert body == {"error": "room must be a room id"}


def test_patch_clears_prompt_over_http(adapter):
    with _running(adapter) as address:
        _, row = _create(address, skill="brief")
        status, cleared = _request(address, "PATCH", f"/cron/jobs/{row['id']}", {"prompt": ""})
        assert status == 200 and cleared["prompt"] == ""
        assert _request(address, "GET", "/cron/jobs")[1]["jobs"][0]["prompt"] == ""
        assert _request(address, "PATCH", f"/cron/jobs/{row['id']}", {"prompt": "restored"})[0] == 200
        status, cleared = _request(address, "PATCH", f"/cron/jobs/{row['id']}", {"skill": ""})
        assert status == 200 and cleared["skill"] is None
        status, body = _request(
            address, "PATCH", f"/cron/jobs/{row['id']}", {"prompt": "", "skill": ""}
        )
        assert status == 400
        assert body == {"error": "a scheduled job needs a skill, a prompt or a script"}


def test_multiplex_named_home_over_http(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    janitor = tmp_path / "profiles" / "janitor"
    home.mkdir(parents=True)
    janitor.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True))
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: _Provider()
    )
    instance = RetinueRoomsAdapter(PlatformConfig())
    instance.store = RoomStore(base_dir=str(home / "rooms"))
    instance.store.create(Room(id="room-a", name="Room A", members=["sally", "janitor"], lead="sally"))
    with _running(instance) as address:
        assert _request(address, "GET", "/cron/jobs")[1]["owners"] == ["default", "janitor", "sally"]
        status, row = _create(address, owner="janitor")
        assert status == 201 and row["owner"] == "janitor"
        assert _request(address, "PATCH", f"/cron/jobs/{row['id']}", {"name": "J"})[0] == 200
        assert all(j["owner"] == "janitor" for j in _request(address, "GET", "/cron/jobs?owner=janitor")[1]["jobs"])
        assert _request(address, "GET", "/cron/jobs?owner=default")[1]["jobs"] == []
    assert (janitor / "cron" / "jobs.json").exists()
    assert not (home / "cron" / "jobs.json").exists()


def test_named_profile_topology_lists_and_saves_under_the_real_slug(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "sally"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(False))
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler", lambda: _Provider()
    )
    instance = RetinueRoomsAdapter(PlatformConfig())
    instance.store = RoomStore(base_dir=str(home / "rooms"))
    instance.store.create(Room(id="room-a", name="Room A", members=["sally"], lead="sally"))
    instance.store.append(
        "room-a", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="Prepare it")
    )
    with _running(instance) as address:
        assert _request(address, "GET", "/cron/jobs")[1]["owners"] == ["sally"]
        status, payload = _request(
            address, "POST", "/routines",
            {"name": "Daily brief", "room": "room-a", "schedule": "every 1d"},
        )
        assert status == 201
        assert payload["owner"] == "sally" and payload["job"]["owner"] == "sally"
        assert _request(address, "GET", "/cron/jobs")[1]["jobs"][0]["owner"] == "sally"
    assert (home / "retinue_rooms" / "routines" / "daily-brief.json").exists()
    assert (home / "skills" / "daily-brief" / "SKILL.md").exists()
    with open(home / "cron" / "jobs.json", encoding="utf-8") as handle:
        stored = json.load(handle)["jobs"][0]
    assert stored["retinue"]["owner"] == "sally"
    assert stored["origin"]["thread_id"] == "sally"


def test_empty_served_set_lists_nothing_and_404s_every_mutation(tmp_path, monkeypatch):
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(True))
    monkeypatch.setattr(cronjobs, "_served_pairs", lambda _cfg: [("elsewhere", str(elsewhere))])
    instance = RetinueRoomsAdapter(PlatformConfig())
    instance.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    instance.store.create(Room(id="room-a", name="Room A", members=["default"], lead="default"))
    with _running(instance) as address:
        status, body = _request(address, "GET", "/cron/jobs")
        assert status == 200 and body["jobs"] == [] and body["owners"] == []
        assert _create(address)[0] == 404
        for method, suffix, body in (
            ("PATCH", "", {"name": "x"}),
            ("POST", "/pause", {}),
            ("POST", "/resume", {}),
            ("POST", "/run", {}),
            ("DELETE", "", None),
        ):
            assert _request(address, method, f"/cron/jobs/not-a-job{suffix}", body)[0] == 404


def test_cron_routes_require_bearer_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RETINUE_ROOMS_API_KEY", "secret")
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: _config(False))
    instance = RetinueRoomsAdapter(PlatformConfig())
    instance.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    with _running(instance) as address:
        assert _request(address, "GET", "/cron/jobs")[0] == 401
        assert _request(address, "GET", "/cron/jobs", key="secret")[0] == 200
