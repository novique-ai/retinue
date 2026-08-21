"""workspace=sandbox|ide — option A bind-mount (no gateway required)."""

from __future__ import annotations

import json
import os

import pytest

from tools import workspace_context

from . import ide
from .engine import Room, room_briefing
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout"])
    defaults.update(kwargs)
    return Room(**defaults)


def test_old_room_json_defaults_to_sandbox():
    room = Room.from_dict({"id": "old", "name": "Old", "members": ["scout"]})
    assert room.workspace == "sandbox"
    assert room.ide_path is None
    assert room.shared_mode is None


def test_parse_workspace_defaults_and_rejects():
    assert ide.parse_workspace(None) == "sandbox"
    assert ide.parse_workspace("") == "sandbox"
    assert ide.parse_workspace("IDE") == "ide"
    with pytest.raises(ValueError, match="sandbox"):
        ide.parse_workspace("host")


def test_resolve_ide_path_requires_existing_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_IDE_ROOT", raising=False)
    with pytest.raises(ValueError, match="ide_path or set RETINUE_IDE_ROOT"):
        ide.resolve_ide_path(None)
    with pytest.raises(ValueError, match="not a directory"):
        ide.resolve_ide_path(str(tmp_path / "missing"))
    got = ide.resolve_ide_path(str(tmp_path))
    assert got == str(tmp_path)


def test_resolve_ide_path_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(tmp_path))
    assert ide.resolve_ide_path(None) == str(tmp_path)


def test_list_folders_under_root(tmp_path, monkeypatch):
    # Own the root outright rather than listing bare tmp_path: this asserts an
    # EXACT folder listing, and tmp_path is shared with whatever else the
    # active fixtures put there (the repo-wide hermetic fixture parks a
    # sandboxed HERMES_HOME beside it). Anchoring to a private subdirectory
    # keeps the assertion about ide.list_folders instead of about who else
    # wrote to tmp_path.
    root = tmp_path / "root"
    (root / "projects" / "retinue").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(root))
    listing = ide.list_folders(None)
    assert listing["path"] == str(root)
    assert listing["parent"] is None
    assert [f["name"] for f in listing["folders"]] == ["projects"]
    child = ide.list_folders(str(root / "projects"))
    assert child["parent"] == str(root)
    assert [f["name"] for f in child["folders"]] == ["retinue"]
    with pytest.raises(ValueError, match="under the configured IDE root"):
        ide.list_folders("/tmp")


def test_list_folders_requires_a_root_or_path(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_IDE_ROOT", raising=False)
    with pytest.raises(ValueError, match="RETINUE_IDE_ROOT"):
        ide.list_folders(None)
    listing = ide.list_folders(str(tmp_path))
    assert listing["path"] == str(tmp_path)
    assert listing["root"] is None


def _shared_specs(vols: list[str]) -> list[str]:
    return [v for v in vols if ":/shared:" in v or v.endswith(":/shared")]


def test_overlay_sandbox_clears_volumes(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    room = _room(workspace="sandbox")
    env = ide.overlay_env(room)
    assert env["TERMINAL_ENV"] == "docker"
    assert env["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == ide.container_key_for_room(room)
    assert json.loads(env["TERMINAL_DOCKER_VOLUMES"]) == []
    assert env["TERMINAL_CWD"] == "/workspace"


def test_overlay_sandbox_mounts_room_uploads(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    room = _room(workspace="sandbox")
    env = ide.overlay_env(room, str(tmp_path))
    vols = json.loads(env["TERMINAL_DOCKER_VOLUMES"])
    assert len(vols) == 1
    assert vols[0].endswith(":/workspace/uploads:ro")
    assert os.path.isdir(vols[0].split(":")[0])


def test_overlay_ide_bind_mounts_host_path(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    room = _room(workspace="ide", ide_path=str(tmp_path))
    env = ide.overlay_env(room)
    assert env["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == ide.container_key_for_room(room)
    assert json.loads(env["TERMINAL_DOCKER_VOLUMES"]) == [f"{tmp_path}:/workspace:rw"]
    with_home_overlay = ide.overlay_env(room, str(tmp_path))
    with_home = json.loads(with_home_overlay["TERMINAL_DOCKER_VOLUMES"])
    assert with_home_overlay["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == env[
        "TERMINAL_DOCKER_SHARED_CONTAINER_KEY"
    ]
    assert with_home[0] == f"{tmp_path}:/workspace:rw"
    assert with_home[1].endswith(":/workspace/uploads:ro")


def test_overlay_no_shared_when_unset_sandbox(monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    assert ide.configured_shared_dir() is None
    vols = json.loads(ide.overlay_env(_room(workspace="sandbox"))["TERMINAL_DOCKER_VOLUMES"])
    assert _shared_specs(vols) == []


def test_overlay_no_shared_when_unset_ide(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    assert ide.configured_shared_dir() is None
    assert ide.SHARED_MOUNT == "/shared"
    room = _room(workspace="ide", ide_path=str(tmp_path))
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert _shared_specs(vols) == []
    assert f"{tmp_path}:/workspace:rw" in vols


def test_overlay_shared_writable_by_default(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    host = os.path.abspath(str(shared))
    sand_vols = json.loads(ide.overlay_env(_room(workspace="sandbox"))["TERMINAL_DOCKER_VOLUMES"])
    ide_vols = json.loads(
        ide.overlay_env(_room(workspace="ide", ide_path=str(tmp_path)))["TERMINAL_DOCKER_VOLUMES"]
    )
    assert f"{host}:/shared:rw" in sand_vols
    assert f"{host}:/shared:rw" in ide_vols
    assert _shared_specs(sand_vols) == [f"{host}:/shared:rw"]
    assert _shared_specs(ide_vols) == [f"{host}:/shared:rw"]


def test_overlay_shared_rw_when_opted_in(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    host = os.path.abspath(str(shared))
    room = _room(workspace="sandbox", shared_mode="rw")
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert f"{host}:/shared:rw" in vols
    assert _shared_specs(vols) == [f"{host}:/shared:rw"]


def test_overlay_shared_ro_when_explicit(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    host = os.path.abspath(str(shared))
    for room in (
        _room(workspace="sandbox", shared_mode="ro"),
        _room(workspace="ide", ide_path=str(tmp_path), shared_mode="ro"),
    ):
        vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
        assert _shared_specs(vols) == [f"{host}:/shared:ro"]


def test_overlay_shared_unknown_mode_on_record_is_readonly(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    host = os.path.abspath(str(shared))
    room = Room.from_dict(
        {"id": "old", "name": "Old", "members": ["scout"], "shared_mode": "bogus"}
    )
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert _shared_specs(vols) == [f"{host}:/shared:ro"]


def test_ensure_share_layout_creates_inbox_and_room_dir(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    room = _room(id="lab-1", workspace="sandbox")
    path = ide.ensure_share_layout(room)
    assert path == str(shared / "rooms" / "lab-1")
    assert (shared / "inbox").is_dir()
    assert (shared / "rooms" / "lab-1").is_dir()


def test_overlay_shared_missing_dir_raises(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-shared"
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(missing))
    with pytest.raises(ValueError, match="not a directory"):
        ide.overlay_env(_room(workspace="sandbox"))
    with pytest.raises(ValueError, match="not a directory"):
        ide.overlay_env(_room(workspace="ide", ide_path=str(tmp_path)))


def test_parse_shared_mode_defaults_and_rejects():
    assert ide.parse_shared_mode(None) == "rw"
    assert ide.parse_shared_mode("") == "rw"
    assert ide.parse_shared_mode("RW") == "rw"
    assert ide.parse_shared_mode("ro") == "ro"
    with pytest.raises(ValueError, match="shared_mode"):
        ide.parse_shared_mode("readwrite")
    with pytest.raises(ValueError, match="shared_mode"):
        ide.parse_shared_mode("yes")


def test_sandbox_and_ide_use_different_container_keys(tmp_path):
    sand = ide.container_key("ops-ab12", "sandbox")
    attached = ide.container_key("ops-ab12", "ide")
    assert sand != attached
    assert "sandbox" in sand and "ide" in attached


def test_apply_room_workspace_does_not_leak_the_room_into_process_env(tmp_path, monkeypatch):
    """The per-room values are scoped to the cycle and never touch os.environ.

    This replaces an older assertion that the cycle *set and restored*
    os.environ. Save/restore was only ever safe while cycles ran one at a
    time; the values now ride a ContextVar so rooms can overlap, and the
    property worth pinning is the stronger one — a room's key and mounts are
    invisible outside its own scope, so an operator's process-wide setting
    survives a cycle untouched.
    """
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", '["/leak:/workspace:rw"]')
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    room = _room(workspace="sandbox")

    with ide.apply_room_workspace(room) as overlay:
        assert overlay["TERMINAL_ENV"] == "docker"
        # in-scope readers see the room
        assert workspace_context.getenv("TERMINAL_DOCKER_VOLUMES") == "[]"
        assert workspace_context.shared_container_key() == ide.container_key_for_room(room)
        # process env is NOT rewritten with the room's values
        assert os.environ["TERMINAL_DOCKER_VOLUMES"] == '["/leak:/workspace:rw"]'

    # and nothing survives the scope
    assert workspace_context.current() is None
    assert workspace_context.getenv("TERMINAL_DOCKER_VOLUMES") == '["/leak:/workspace:rw"]'
    assert workspace_context.shared_container_key() == ""
    assert os.environ["TERMINAL_DOCKER_VOLUMES"] == '["/leak:/workspace:rw"]'


def test_briefing_names_the_workspace_kind(tmp_path):
    sand = room_briefing(_room(workspace="sandbox"), "scout", ["You"])
    attached = room_briefing(_room(workspace="ide", ide_path=str(tmp_path)), "scout", ["You"])
    assert "sandboxed" in sand.lower()
    assert "bind-mount" in attached


def test_create_and_patch_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room("Lab", ["scout"], None, None)
    assert created["workspace"] == "sandbox"
    assert created["ide_path"] is None
    assert created["shared_mode"] == "rw"

    with pytest.raises(ValueError, match="IDE rooms need a host path"):
        adapter.create_room("Code", ["scout"], None, None, workspace="ide")

    attached = adapter.create_room(
        "Code", ["scout"], None, None, workspace="ide", ide_path=str(tmp_path)
    )
    assert attached["workspace"] == "ide"
    assert attached["ide_path"] == str(tmp_path)

    patched = adapter.patch_room(created["id"], {"workspace": "ide", "ide_path": str(tmp_path)})
    assert patched["workspace"] == "ide"
    back = adapter.patch_room(created["id"], {"workspace": "sandbox"})
    assert back["workspace"] == "sandbox"
    assert back["ide_path"] is None


def test_shared_mode_create_patch_and_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))

    with pytest.raises(ValueError, match="shared_mode"):
        adapter.create_room("Nope", ["scout"], None, None, shared_mode="readwrite")

    created = adapter.create_room("Lab", ["scout"], None, None, shared_mode="rw")
    assert created["shared_mode"] == "rw"

    with pytest.raises(ValueError, match="shared_mode"):
        adapter.patch_room(created["id"], {"shared_mode": "yes"})
    still = adapter.store.get(created["id"])
    assert still is not None
    assert still.shared_mode == "rw", "invalid patch must not coerce or overwrite shared_mode"

    patched = adapter.patch_room(created["id"], {"shared_mode": "ro"})
    assert patched["shared_mode"] == "ro"


def test_http_create_ide_room(tmp_path, monkeypatch):
    import http.client
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        return resp.status, payload

    try:
        status, payload = call(
            "POST",
            "/rooms",
            {
                "name": "IDE lab",
                "members": ["scout"],
                "workspace": "ide",
                "ide_path": str(tmp_path),
            },
        )
        assert status == 201
        assert payload["workspace"] == "ide"
        assert payload["ide_path"] == str(tmp_path)

        status, payload = call("GET", "/workspace")
        assert status == 200
        assert "ide_root" in payload

        status, payload = call(
            "POST",
            "/rooms",
            {"name": "Nope", "members": ["scout"], "workspace": "ide"},
        )
        assert status == 400
        assert "ide_path" in payload["error"] or "RETINUE_IDE_ROOT" in payload["error"]

        (tmp_path / "projects").mkdir()
        monkeypatch.setenv("RETINUE_IDE_ROOT", str(tmp_path))
        status, payload = call("GET", "/workspace/folders")
        assert status == 200
        assert payload["path"] == str(tmp_path)
        assert any(f["name"] == "projects" for f in payload["folders"])
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(
    not __import__("shutil").which("podman"),
    reason="podman not on PATH",
)
def test_podman_ide_mount_is_isolated_from_sandbox(tmp_path):
    """Smoke option A: the generated volume spec is a real bind-mount.

    IDE container sees the host tree. Sandbox container with [] volumes
    does not. No gateway — just the runtime contract we overlay onto Hermes.
    """
    import shutil
    import subprocess
    import uuid

    runtime = shutil.which("podman")
    assert runtime
    host = tmp_path / "ide"
    host.mkdir()
    token = f"retinue-ide-{uuid.uuid4().hex[:8]}"
    (host / "HOST.txt").write_text(token, encoding="utf-8")
    image = "docker.io/library/python:3.12-slim"
    name_ide = f"retinue-smoke-ide-{uuid.uuid4().hex[:8]}"
    name_sand = f"retinue-smoke-sand-{uuid.uuid4().hex[:8]}"
    room = _room(id="smoke", workspace="ide", ide_path=str(host))
    volume = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])[0]

    def run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )

    try:
        created = run(
            [
                runtime,
                "run",
                "-d",
                "--name",
                name_ide,
                "-v",
                volume,
                "-w",
                "/workspace",
                image,
                "sleep",
                "60",
            ]
        )
        if created.returncode != 0:
            pytest.skip(f"podman run failed (image pull?): {created.stderr[-400:]}")
        saw = run([runtime, "exec", name_ide, "cat", "/workspace/HOST.txt"])
        assert saw.returncode == 0, saw.stderr
        assert saw.stdout.strip() == token
        wrote = run(
            [runtime, "exec", name_ide, "sh", "-c", "echo from-container > /workspace/OUT.txt"]
        )
        assert wrote.returncode == 0, wrote.stderr
        assert (host / "OUT.txt").read_text(encoding="utf-8").strip() == "from-container"

        # Hermes sandbox computers get an isolated /workspace (tmpfs or
        # sandbox dir), not a missing cwd. Mirror that so isolation is
        # the mount source, not "directory does not exist."
        sand_ws = tmp_path / "sandbox-workspace"
        sand_ws.mkdir()
        sand = run(
            [
                runtime,
                "run",
                "-d",
                "--name",
                name_sand,
                "-v",
                f"{sand_ws}:/workspace:rw",
                "-w",
                "/workspace",
                image,
                "sleep",
                "60",
            ]
        )
        if sand.returncode != 0:
            pytest.skip(f"sandbox podman run failed: {sand.stderr[-400:]}")
        listing = run([runtime, "exec", name_sand, "ls", "/workspace"])
        assert listing.returncode == 0, listing.stderr
        assert "HOST.txt" not in listing.stdout
        missing = run([runtime, "exec", name_sand, "cat", "/workspace/HOST.txt"])
        assert missing.returncode != 0
    finally:
        run([runtime, "rm", "-f", name_ide, name_sand], timeout=30)


# ── #16: the env cache must be keyed by the room's container, not "default" ──


def _cache_key_during(room, monkeypatch, tmp_path):
    """The env-cache key terminal_tool resolves while *room*'s turn is live."""
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    with ide.apply_room_workspace(room, str(tmp_path)):
        # The top-level agent passes task_id=None for every room turn.
        return terminal_tool._resolve_container_task_id(None)


class _FakeCachedEnv:
    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


def test_room_turn_keys_the_env_cache_by_container(monkeypatch, tmp_path):
    """A sandbox turn and an IDE turn must not resolve to the same cache key.

    They previously both collapsed to "default", so the environment created
    for whichever room spoke first stayed cached and every later turn — in
    any room — reused that container. Sandbox writes landed in the IDE
    bind-mount and vice versa.
    """
    ide_root = tmp_path / "code"
    ide_root.mkdir()
    sandbox_room = _room(id="r-sand", workspace="sandbox")
    ide_room = _room(id="r-ide", workspace="ide", ide_path=str(ide_root))

    sandbox_key = _cache_key_during(sandbox_room, monkeypatch, tmp_path)
    ide_key = _cache_key_during(ide_room, monkeypatch, tmp_path)

    assert sandbox_key == ide.container_key_for_room(sandbox_room)
    assert ide_key == ide.container_key_for_room(ide_room)
    assert sandbox_key != ide_key


def test_two_sandbox_rooms_do_not_share_one_cached_container(monkeypatch, tmp_path):
    """Isolation is per room, not merely per workspace mode."""
    a = _cache_key_during(_room(id="r-a", workspace="sandbox"), monkeypatch, tmp_path)
    b = _cache_key_during(_room(id="r-b", workspace="sandbox"), monkeypatch, tmp_path)
    assert a != b


def test_shared_mode_patch_changes_cache_key_and_evicts_old_env(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from tools import terminal_tool

    from .adapter import RetinueRoomsAdapter

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room("Lab", ["scout"], None, None, workspace="sandbox")
    room_before = adapter.store.get(created["id"])
    assert room_before is not None
    old_overlay = ide.overlay_env(room_before)
    old_key = old_overlay["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"]
    assert _shared_specs(json.loads(old_overlay["TERMINAL_DOCKER_VOLUMES"])) == [
        f"{os.path.abspath(str(shared))}:/shared:rw"
    ]

    stale_env = _FakeCachedEnv()
    monkeypatch.setattr(terminal_tool, "_active_environments", {old_key: stale_env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {old_key: 1.0})

    patched = adapter.patch_room(created["id"], {"shared_mode": "ro"})
    assert patched["shared_mode"] == "ro"
    room_after = adapter.store.get(created["id"])
    assert room_after is not None
    new_overlay = ide.overlay_env(room_after)
    assert _shared_specs(json.loads(new_overlay["TERMINAL_DOCKER_VOLUMES"])) == [
        f"{os.path.abspath(str(shared))}:/shared:ro"
    ]

    new_key = _cache_key_during(room_after, monkeypatch, tmp_path)
    assert new_key == new_overlay["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"]
    assert new_key != old_key
    assert old_key not in terminal_tool._active_environments
    assert stale_env.cleaned is True


def test_ide_path_patch_changes_cache_key_and_evicts_old_env(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from tools import terminal_tool

    from .adapter import RetinueRoomsAdapter

    old_root = tmp_path / "old-root"
    new_root = tmp_path / "new-root"
    old_root.mkdir()
    new_root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    created = adapter.create_room(
        "Code", ["scout"], None, None, workspace="ide", ide_path=str(old_root)
    )
    room_before = adapter.store.get(created["id"])
    assert room_before is not None
    old_overlay = ide.overlay_env(room_before)
    old_key = old_overlay["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"]
    assert json.loads(old_overlay["TERMINAL_DOCKER_VOLUMES"]) == [f"{old_root}:/workspace:rw"]

    stale_env = _FakeCachedEnv()
    monkeypatch.setattr(terminal_tool, "_active_environments", {old_key: stale_env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {old_key: 1.0})

    patched = adapter.patch_room(created["id"], {"ide_path": str(new_root)})
    assert patched["ide_path"] == str(new_root)
    room_after = adapter.store.get(created["id"])
    assert room_after is not None
    new_overlay = ide.overlay_env(room_after)
    assert json.loads(new_overlay["TERMINAL_DOCKER_VOLUMES"]) == [f"{new_root}:/workspace:rw"]

    new_key = _cache_key_during(room_after, monkeypatch, tmp_path)
    assert new_key == new_overlay["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"]
    assert new_key != old_key
    assert old_key not in terminal_tool._active_environments
    assert stale_env.cleaned is True


def test_cache_key_falls_back_to_default_outside_a_room(monkeypatch):
    """No room overlay -> unchanged upstream behavior for the CLI/desktop."""
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.delenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id(None) == "default"
    assert terminal_tool._resolve_container_task_id("some-session") == "default"


# ── cross-room concurrency (#67) ─────────────────────────────────────────
#
# The workspace values used to travel through os.environ, which is
# process-global, so the adapter serialized every cycle behind one lock and a
# single turn blocked every other room. These tests pin the property that
# replaced the lock: a cycle's workspace is visible ONLY to that cycle.


@pytest.mark.asyncio
async def test_concurrent_rooms_never_see_each_others_container_key(tmp_path):
    """Two cycles interleaved on one loop each keep their own key.

    Deliberately forces the interleaving rather than hoping for it: each task
    binds its workspace, waits for the other to have bound its own, and only
    then reads back. With a process-global carrier both reads return whichever
    room bound last, which is the leak this replaced the lock to prevent.
    """
    import asyncio

    bound = asyncio.Event()
    seen: dict[str, str] = {}

    async def cycle(room: Room, tag: str, *, first: bool) -> None:
        with ide.apply_room_workspace(room, str(tmp_path)):
            if first:
                bound.set()
            else:
                await bound.wait()
            await asyncio.sleep(0)  # yield: let the other task run inside its own scope
            seen[tag] = workspace_context.shared_container_key()

    await asyncio.gather(
        cycle(_room(id="alpha", workspace="sandbox"), "alpha", first=True),
        cycle(_room(id="beta", workspace="ide", ide_path=str(tmp_path)), "beta", first=False),
    )

    assert seen["alpha"] == ide.container_key_for_room(_room(id="alpha", workspace="sandbox"))
    assert seen["beta"] == ide.container_key_for_room(
        _room(id="beta", workspace="ide", ide_path=str(tmp_path))
    )


@pytest.mark.asyncio
async def test_concurrent_rooms_never_see_each_others_volumes(tmp_path):
    """Same isolation for the mount list, which decides what a room can reach.

    A sandbox room must not inherit an IDE room's host bind — that is the
    boundary novique-ai/retinue#16 was about, reached here by a second route.
    """
    import asyncio

    ide_root = tmp_path / "ide-root"
    ide_root.mkdir()
    bound = asyncio.Event()
    seen: dict[str, str] = {}

    async def cycle(room: Room, tag: str, *, first: bool) -> None:
        with ide.apply_room_workspace(room, str(tmp_path)):
            if first:
                bound.set()
            else:
                await bound.wait()
            await asyncio.sleep(0)
            seen[tag] = workspace_context.getenv("TERMINAL_DOCKER_VOLUMES", "[]")

    await asyncio.gather(
        cycle(_room(id="box", workspace="sandbox"), "box", first=True),
        cycle(_room(id="work", workspace="ide", ide_path=str(ide_root)), "work", first=False),
    )

    assert str(ide_root) in seen["work"]
    assert str(ide_root) not in seen["box"], "sandbox room inherited the IDE bind-mount"


def test_workspace_key_reaches_a_tool_dispatch_thread(tmp_path):
    """The container is created in a worker thread, not on the loop.

    Tool dispatch is fanned onto threads via
    ``tools.thread_context.propagate_context_to_thread``. If the workspace did
    not ride along, the environment would be built against whatever the
    process env happened to hold — so this is the load-bearing assumption
    behind dropping the lock, and it gets its own test.
    """
    import threading

    from tools.thread_context import propagate_context_to_thread

    seen: dict[str, str] = {}

    def worker() -> None:
        seen["key"] = workspace_context.shared_container_key()

    with ide.apply_room_workspace(_room(id="threaded"), str(tmp_path)):
        t = threading.Thread(target=propagate_context_to_thread(worker))
        t.start()
        t.join()

    assert seen["key"] == ide.container_key_for_room(_room(id="threaded"))


def test_no_overlay_falls_back_to_process_env(monkeypatch, tmp_path):
    """Callers outside a room are untouched: CLI, desktop, delegate_task.

    The shared-workspace knob is also a documented process-env setting, so it
    has to keep working with no overlay bound.
    """
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "set-by-operator")
    assert workspace_context.shared_container_key() == "set-by-operator"

    room = _room(id="scoped")
    with ide.apply_room_workspace(room, str(tmp_path)):
        assert workspace_context.shared_container_key() == ide.container_key_for_room(room)

    assert workspace_context.shared_container_key() == "set-by-operator"


def test_invariant_values_stay_in_process_env(tmp_path):
    """The non-varying room values still reach tools that read os.environ.

    TERMINAL_CWD and the mount flag are read straight from the environment by
    other tools (the code-execution tool among them), so they are published
    process-wide. They are identical for every room, so that is race-free.
    """
    with ide.apply_room_workspace(_room(id="envcheck"), str(tmp_path)):
        for key in ide.INVARIANT_ENV:
            assert os.environ.get(key), f"{key} must be visible to os.environ readers"
        assert os.environ["TERMINAL_CWD"] == ide.CONTAINER_MOUNT


def test_terminal_env_is_never_written_by_a_cycle(monkeypatch, tmp_path):
    """A room cycle must not move the whole process onto the docker backend.

    TERMINAL_ENV selects the terminal backend for every platform in the
    gateway, at ~30 direct read sites. Writing it here would silently put a
    Discord or Telegram agent's shell in a container. The requirement is
    checked at connect() instead (see docker_backend_error).
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
    with ide.apply_room_workspace(_room(id="nowrite"), str(tmp_path)) as overlay:
        assert overlay["TERMINAL_ENV"] == "docker"  # in-scope readers still see intent
        assert os.environ["TERMINAL_ENV"] == "local", (
            "a room cycle rewrote the process-wide terminal backend"
        )
    assert os.environ["TERMINAL_ENV"] == "local"
    assert "TERMINAL_ENV" not in ide.INVARIANT_ENV


def test_docker_backend_is_a_checked_precondition(monkeypatch):
    """Rooms report a misconfigured gateway instead of repairing it."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert ide.docker_backend_error() is None

    monkeypatch.setenv("TERMINAL_ENV", "local")
    problem = ide.docker_backend_error()
    assert problem and "TERMINAL_ENV" in problem and "docker" in problem

    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    assert ide.docker_backend_error(), "an unset backend is the default 'local', not docker"


# ── host-broker reach from a scoped room (infra-90xc) ────────────────────────
#
# A room scoped below the IDE root is the supported shape, not a mistake: it
# is how a retainer is confined to one product repo. Before these mounts, that
# confinement silently removed bd, gh, flag-defect, host-git, cr, crm and
# rm.sh, because every shim reached the socket and the client through
# /workspace — a path that IS the IDE root only for a room mounted there.


def _broker_specs(vols: list[str]) -> list[str]:
    return [v for v in vols if ide.BROKER_MOUNT in v or ide.BROKER_CLIENT_MOUNT in v]


def _ide_root_with_broker(tmp_path):
    root = tmp_path / "IDE"
    (root / "data" / "broker").mkdir(parents=True)
    (root / "infra" / "scripts").mkdir(parents=True)
    (root / "infra" / "scripts" / "room-broker-client.py").write_text("# client\n", encoding="utf-8")
    return root


def test_scoped_ide_room_still_reaches_the_broker(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    root = _ide_root_with_broker(tmp_path)
    scoped = root / "projects" / "labelwatch"
    scoped.mkdir(parents=True)
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(root))
    room = _room(workspace="ide", ide_path=str(scoped))
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert f"{scoped}:/workspace:rw" in vols
    assert _broker_specs(vols) == [
        f"{root}/data/broker:{ide.BROKER_MOUNT}:rw",
        f"{root}/infra/scripts/room-broker-client.py:{ide.BROKER_CLIENT_MOUNT}:ro",
    ]


def test_broker_mounts_skipped_without_an_ide_root(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.delenv("RETINUE_IDE_ROOT", raising=False)
    room = _room(workspace="ide", ide_path=str(tmp_path))
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert _broker_specs(vols) == []


def test_broker_mounts_skipped_when_the_socket_dir_is_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    root = tmp_path / "IDE"
    root.mkdir()
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(root))
    room = _room(workspace="ide", ide_path=str(root))
    vols = json.loads(ide.overlay_env(room)["TERMINAL_DOCKER_VOLUMES"])
    assert _broker_specs(vols) == []


def test_sandbox_room_gets_no_broker_mounts(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(_ide_root_with_broker(tmp_path)))
    vols = json.loads(ide.overlay_env(_room(workspace="sandbox"))["TERMINAL_DOCKER_VOLUMES"])
    assert _broker_specs(vols) == []


def test_mount_map_names_the_rooms_real_mount(tmp_path, monkeypatch):
    root = _ide_root_with_broker(tmp_path)
    scoped = root / "projects" / "labelwatch"
    scoped.mkdir(parents=True)
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(root))
    room = _room(workspace="ide", ide_path=str(scoped))
    # Not [["/workspace", str(root)]] — that assumption is the defect.
    assert ide.workspace_mount_map(room) == [["/workspace", str(scoped)]]
    assert ide.workspace_mount_map(_room(workspace="sandbox")) == []


def test_mount_map_puts_worktree_binds_before_the_workspace(tmp_path, monkeypatch):
    root = _ide_root_with_broker(tmp_path)
    (root / "infra").mkdir(exist_ok=True)
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(root))
    monkeypatch.setenv("RETINUE_WORKTREE_ROOT", str(tmp_path / "wt"))
    room = _room(id="r-wt", workspace="ide", ide_path=str(root), worktree_repos=["infra"])
    pairs = ide.workspace_mount_map(room)
    assert [p[0] for p in pairs] == ["/workspace/infra", "/workspace"]
    assert pairs[0][1].startswith(str(tmp_path / "wt"))
    assert pairs[1][1] == str(root)
