"""Routines + workspace-status unit tests (no gateway)."""

from __future__ import annotations

import pytest

from . import cronjobs, routines, skilldraft, workspace
from .engine import KIND_AGENT, KIND_USER, RoomMessage


def test_user_prompts_from_messages_skips_agents_and_bounds():
    msgs = [
        RoomMessage(1, 1, KIND_USER, "Mark", "first"),
        RoomMessage(2, 2, KIND_AGENT, "scout", "ok"),
        RoomMessage(3, 3, KIND_USER, "Mark", "second"),
        RoomMessage(4, 4, KIND_USER, "Mark", "third"),
    ]
    assert routines.user_prompts_from_messages(msgs) == ["first", "second", "third"]
    assert routines.user_prompts_from_messages(msgs, since=1, until=3) == ["second"]


def test_save_list_get_delete_routine(tmp_path):
    meta = routines.save_routine(str(tmp_path), "Daily standup", ["what's open?", "summarize"])
    assert meta["slug"] == "daily-standup"
    assert [r["slug"] for r in routines.list_routines(str(tmp_path))] == ["daily-standup"]
    got = routines.get_routine(str(tmp_path), "daily-standup")
    assert got["messages"] == ["what's open?", "summarize"]
    with pytest.raises(FileExistsError):
        routines.save_routine(str(tmp_path), "Daily standup", ["x"])
    with pytest.raises(ValueError):
        routines.save_routine(str(tmp_path), "Empty", [])
    assert routines.delete_routine(str(tmp_path), "daily-standup") is True
    assert routines.get_routine(str(tmp_path), "daily-standup") is None
    assert routines.delete_routine(str(tmp_path), "daily-standup") is False


def test_http_lists_routines_for_source_room(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
    from .engine import Room
    from .store import RoomStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(Room(id="room-a", name="A", members=["sally"], lead="sally"))
    adapter.store.create(Room(id="room-b", name="B", members=["sally"], lead="sally"))
    routines.save_routine(str(tmp_path), "From A", ["hi"], source_room="room-a")
    routines.save_routine(str(tmp_path), "From B", ["yo"], source_room="room-b")
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        conn.request("GET", "/rooms/room-a/routines")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert [r["slug"] for r in payload["routines"]] == ["from-a"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_workspace_status_disabled_without_key(monkeypatch):
    monkeypatch.delenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", raising=False)
    status = workspace.workspace_status()
    assert status["enabled"] is False
    assert status["running"] is False
    assert "TERMINAL_DOCKER_SHARED_CONTAINER_KEY" in (status["detail"] or "")


def test_workspace_status_enabled_but_no_runtime(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "demo")
    monkeypatch.setenv("HERMES_DOCKER_BINARY", "/no/such/runtime")
    monkeypatch.setattr(workspace.shutil, "which", lambda *_a, **_k: None)
    status = workspace.workspace_status()
    assert status["enabled"] is True
    assert status["key"] == "demo"
    # binary is forced, inspect will fail loudly — still not running
    assert status["running"] is False


def _routine_adapter(tmp_path, monkeypatch, *, owners=("sally",), lead="sally", members=None):
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter
    from .engine import Room
    from .store import RoomStore

    members = list(members or owners)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pairs = []
    for owner in owners:
        if owner == "default":
            path = tmp_path
        else:
            path = tmp_path / "profiles" / owner
            path.mkdir(parents=True, exist_ok=True)
        pairs.append((owner, str(path)))
    monkeypatch.setattr(cronjobs, "_gateway_config", lambda: object())
    monkeypatch.setattr(cronjobs, "_served_pairs", lambda _cfg: pairs)
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(Room(id="room-a", name="Room A", members=members, lead=lead))
    adapter.store.append(
        "room-a", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="Prepare it")
    )
    adapter.store.append(
        "room-a", RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=lead, text="Done")
    )
    return adapter


def test_save_routine_writes_schema2_skill_and_expected_output(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch)
    meta = adapter.save_routine_from_room("Daily brief", "room-a")
    assert meta["schema"] == 2
    assert meta["owner"] == "sally"
    assert meta["skill"] == "daily-brief"
    assert meta["steps"] == ["Prepare it"]
    assert meta["expected_output"] == "Done"
    assert (tmp_path / "profiles" / "sally" / "skills" / "daily-brief" / "SKILL.md").is_file()


def test_owner_defaults_to_the_room_lead_when_served(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch, owners=("sally", "editor"))
    assert adapter.save_routine_from_room("Demo", "room-a")["owner"] == "sally"


def test_owner_falls_back_to_the_first_served_member(tmp_path, monkeypatch):
    adapter = _routine_adapter(
        tmp_path, monkeypatch, owners=("default", "editor"),
        lead="nobody", members=["nobody", "editor"],
    )
    assert adapter.save_routine_from_room("Demo", "room-a")["owner"] == "editor"


def test_no_served_member_is_a_404_condition(tmp_path, monkeypatch):
    adapter = _routine_adapter(
        tmp_path, monkeypatch, owners=("default",), lead="nobody", members=["nobody"]
    )
    with pytest.raises(cronjobs.UnknownOwner):
        adapter.save_routine_from_room("Demo", "room-a")


def test_legacy_schema1_routine_still_runs(tmp_path, monkeypatch):
    import json

    adapter = _routine_adapter(tmp_path, monkeypatch)
    target = tmp_path / "retinue_rooms" / "routines"
    target.mkdir(parents=True)
    path = target / "legacy.json"
    original = {
        "name": "Legacy", "slug": "legacy", "source_room": "room-a",
        "messages": ["first", "second"], "created_at": 1,
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        adapter, "post_user_message",
        lambda room, prompt, speaker, wait=False: calls.append((room, prompt, speaker, wait)) or {},
    )
    adapter.run_routine("legacy", "room-a")
    assert calls == [
        ("room-a", "first", "routine:legacy", True),
        ("room-a", "second", "routine:legacy", True),
    ]
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_post_routines_without_new_keys_keeps_status_and_fields(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    from .adapter import _RoomsRequestHandler, _RoomsServer

    adapter = _routine_adapter(tmp_path, monkeypatch, owners=("default",), lead="default")
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*server.server_address[:2], timeout=10)
        conn.request(
            "POST", "/routines", body=json.dumps({"name": "Demo", "room": "room-a"}),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode())
        conn.close()
        assert response.status == 201
        assert payload["messages"] == ["Prepare it"]
        assert all(key in payload for key in ("name", "slug", "source_room", "created_at"))
        assert payload["schema"] == 2
        assert (tmp_path / "skills" / "demo" / "SKILL.md").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_duplicate_routine_name_leaves_no_skill_draft(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch)
    adapter.save_routine_from_room("Demo", "room-a")
    with pytest.raises(FileExistsError):
        adapter.save_routine_from_room("Demo", "room-a")
    assert len(list((tmp_path / "profiles" / "sally" / "skills").iterdir())) == 1


def test_skill_draft_failure_removes_the_routine_file(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(skilldraft, "write_skill_draft", lambda *_a, **_k: (_ for _ in ()).throw(OSError("draft")))
    with pytest.raises(OSError):
        adapter.save_routine_from_room("Demo", "room-a")
    assert routines.get_routine(str(tmp_path), "demo") is None


def test_pre_persistence_cron_failure_rolls_back_draft_and_routine(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(cronjobs, "create_job", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        adapter.save_routine_from_room("Demo", "room-a", schedule="every 1d")
    assert routines.get_routine(str(tmp_path), "demo") is None
    assert not (tmp_path / "profiles" / "sally" / "skills" / "demo").exists()


class _FailingRegistrationProvider:
    name = "test"

    def register_job(self, _job):
        raise RuntimeError("registration failed")

    def on_jobs_changed(self):
        raise AssertionError("partial create must not redrive")


def test_registration_failure_keeps_routine_skill_and_job(tmp_path, monkeypatch):
    adapter = _routine_adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: _FailingRegistrationProvider(),
    )
    payload = adapter.save_routine_from_room("Demo", "room-a", schedule="every 1d")
    assert payload["job_id"] == payload["job"]["id"]
    assert payload["job"]["registration_error"]
    assert routines.get_routine(str(tmp_path), "demo") is not None
    assert (tmp_path / "profiles" / "sally" / "skills" / "demo" / "SKILL.md").is_file()
    assert cronjobs.get_job(str(tmp_path), payload["job_id"])


def test_metadata_stamp_failure_rolls_back_the_whole_save(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    adapter = _routine_adapter(tmp_path, monkeypatch)
    original = cron_jobs.update_job
    monkeypatch.setattr(cron_jobs, "update_job", lambda *_a, **_k: (_ for _ in ()).throw(OSError("stamp")))
    with pytest.raises(OSError):
        adapter.save_routine_from_room("Demo", "room-a", schedule="every 1d")
    monkeypatch.setattr(cron_jobs, "update_job", original)
    assert routines.get_routine(str(tmp_path), "demo") is None
    assert not (tmp_path / "profiles" / "sally" / "skills" / "demo").exists()
    assert cronjobs.list_jobs(str(tmp_path)) == []
