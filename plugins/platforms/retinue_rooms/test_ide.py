"""workspace=sandbox|ide — option A bind-mount (no gateway required)."""

from __future__ import annotations

import json
import os

import pytest

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
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "retinue").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(tmp_path))
    listing = ide.list_folders(None)
    assert listing["path"] == str(tmp_path)
    assert listing["parent"] is None
    assert [f["name"] for f in listing["folders"]] == ["projects"]
    child = ide.list_folders(str(tmp_path / "projects"))
    assert child["parent"] == str(tmp_path)
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


def test_overlay_ide_bind_mounts_host_path(tmp_path):
    room = _room(workspace="ide", ide_path=str(tmp_path))
    env = ide.overlay_env(room)
    assert env["TERMINAL_DOCKER_SHARED_CONTAINER_KEY"] == "retinue-ide-r-1"
    assert json.loads(env["TERMINAL_DOCKER_VOLUMES"]) == [f"{tmp_path}:/workspace:rw"]


def test_sandbox_and_ide_use_different_container_keys(tmp_path):
    sand = ide.container_key("ops-ab12", "sandbox")
    attached = ide.container_key("ops-ab12", "ide")
    assert sand != attached
    assert "sandbox" in sand and "ide" in attached


def test_apply_room_workspace_restores_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", '["/leak:/workspace:rw"]')
    monkeypatch.setenv("TERMINAL_ENV", "local")
    room = _room(workspace="sandbox")
    with ide.apply_room_workspace(room) as overlay:
        assert os.environ["TERMINAL_DOCKER_VOLUMES"] == "[]"
        assert overlay["TERMINAL_ENV"] == "docker"
    assert os.environ["TERMINAL_DOCKER_VOLUMES"] == '["/leak:/workspace:rw"]'
    assert os.environ["TERMINAL_ENV"] == "local"


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
