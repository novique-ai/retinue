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


def test_overlay_sandbox_clears_volumes(tmp_path):
    room = _room(workspace="sandbox")
    env = ide.overlay_env(room)
    assert env["TERMINAL_ENV"] == "docker"
    assert env["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == "retinue-sandbox-r-1"
    assert json.loads(env["TERMINAL_DOCKER_VOLUMES"]) == []
    assert env["TERMINAL_CWD"] == "/workspace"


def test_overlay_sandbox_mounts_room_uploads(tmp_path):
    room = _room(workspace="sandbox")
    env = ide.overlay_env(room, str(tmp_path))
    vols = json.loads(env["TERMINAL_DOCKER_VOLUMES"])
    assert len(vols) == 1
    assert vols[0].endswith(":/workspace/uploads:ro")
    assert os.path.isdir(vols[0].split(":")[0])


def test_overlay_ide_bind_mounts_host_path(tmp_path):
    room = _room(workspace="ide", ide_path=str(tmp_path))
    env = ide.overlay_env(room)
    assert env["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == "retinue-ide-r-1"
    assert json.loads(env["TERMINAL_DOCKER_VOLUMES"]) == [f"{tmp_path}:/workspace:rw"]
    with_home = json.loads(ide.overlay_env(room, str(tmp_path))["TERMINAL_DOCKER_VOLUMES"])
    assert with_home[0] == f"{tmp_path}:/workspace:rw"
    assert with_home[1].endswith(":/workspace/uploads:ro")


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
    room = _room(workspace="sandbox")

    with ide.apply_room_workspace(room) as overlay:
        assert overlay["TERMINAL_ENV"] == "docker"
        # in-scope readers see the room
        assert workspace_context.getenv("TERMINAL_DOCKER_VOLUMES") == "[]"
        assert workspace_context.shared_container_key() == "retinue-sandbox-r-1"
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

    assert sandbox_key == ide.container_key("r-sand", "sandbox")
    assert ide_key == ide.container_key("r-ide", "ide")
    assert sandbox_key != ide_key


def test_two_sandbox_rooms_do_not_share_one_cached_container(monkeypatch, tmp_path):
    """Isolation is per room, not merely per workspace mode."""
    a = _cache_key_during(_room(id="r-a", workspace="sandbox"), monkeypatch, tmp_path)
    b = _cache_key_during(_room(id="r-b", workspace="sandbox"), monkeypatch, tmp_path)
    assert a != b


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

    assert seen["alpha"] == "retinue-sandbox-alpha"
    assert seen["beta"] == "retinue-ide-beta"


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

    assert seen["key"] == "retinue-sandbox-threaded"


def test_no_overlay_falls_back_to_process_env(monkeypatch, tmp_path):
    """Callers outside a room are untouched: CLI, desktop, delegate_task.

    The shared-workspace knob is also a documented process-env setting, so it
    has to keep working with no overlay bound.
    """
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "set-by-operator")
    assert workspace_context.shared_container_key() == "set-by-operator"

    with ide.apply_room_workspace(_room(id="scoped"), str(tmp_path)):
        assert workspace_context.shared_container_key() == "retinue-sandbox-scoped"

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
