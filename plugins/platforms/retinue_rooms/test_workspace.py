"""Workspace file serving — path rules + IDE read (no gateway)."""

from __future__ import annotations

import os

import pytest

from . import workspace
from .engine import Room


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout"])
    defaults.update(kwargs)
    return Room(**defaults)


def test_normalize_accepts_workspace_paths():
    assert workspace.normalize_workspace_path(
        "/workspace/retinue-intro/assets/retinue-intro-16x9.png"
    ) == "/workspace/retinue-intro/assets/retinue-intro-16x9.png"
    assert workspace.normalize_workspace_path(
        "workspace/foo.png"
    ) == "/workspace/foo.png"


def test_normalize_rejects_escape_and_host_paths():
    with pytest.raises(workspace.WorkspaceFileError) as bad:
        workspace.normalize_workspace_path("/etc/passwd")
    assert bad.value.status == 400
    with pytest.raises(workspace.WorkspaceFileError):
        workspace.normalize_workspace_path("/workspace/../etc/passwd")
    with pytest.raises(workspace.WorkspaceFileError):
        workspace.normalize_workspace_path("/workspace/foo/../../etc/passwd")
    with pytest.raises(workspace.WorkspaceFileError):
        workspace.normalize_workspace_path("/workspace")
    with pytest.raises(workspace.WorkspaceFileError):
        workspace.normalize_workspace_path("/workspace/has space.png")


def test_read_ide_file_from_host_tree(tmp_path):
    assets = tmp_path / "retinue-intro" / "assets"
    assets.mkdir(parents=True)
    png = assets / "card.png"
    png.write_bytes(b"\x89PNG\r\n" + b"x" * 20)
    room = _room(workspace="ide", ide_path=str(tmp_path))
    data, ctype = workspace.read_workspace_file(
        room, "/workspace/retinue-intro/assets/card.png"
    )
    assert data.startswith(b"\x89PNG")
    assert ctype == "image/png"


def test_read_ide_missing_is_404(tmp_path):
    room = _room(workspace="ide", ide_path=str(tmp_path))
    with pytest.raises(workspace.WorkspaceFileError) as missing:
        workspace.read_workspace_file(room, "/workspace/nope.png")
    assert missing.value.status == 404


def test_read_ide_rejects_escape(tmp_path):
    room = _room(workspace="ide", ide_path=str(tmp_path))
    with pytest.raises(workspace.WorkspaceFileError) as bad:
        workspace.read_workspace_file(room, "/workspace/../secret.png")
    assert bad.value.status == 400


def test_content_type_for_known_and_unknown():
    assert workspace.content_type_for("/workspace/a.PNG") == "image/png"
    assert workspace.content_type_for("/workspace/a.bin") == "application/octet-stream"


def test_workspace_status_shared_unset(monkeypatch):
    monkeypatch.delenv("RETINUE_SHARED_DIR", raising=False)
    status = workspace.workspace_status()
    assert status["shared_dir"] is None
    assert status["shared_mount"] is None
    assert status["shared_error"] is None


def test_workspace_status_shared_configured(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(shared))
    status = workspace.workspace_status()
    assert status["shared_dir"] == os.path.abspath(str(shared))
    assert status["shared_mount"] == "/shared"
    assert status["shared_error"] is None


def test_workspace_status_shared_missing_path_surfaces_error(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-shared"
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(missing))
    status = workspace.workspace_status()
    assert status["shared_dir"] == os.path.abspath(str(missing))
    assert status["shared_mount"] == "/shared"
    assert status["shared_error"]
    assert "not a directory" in status["shared_error"]
    # Must not look configured-and-fine: an error is the whole point.
    assert status["shared_error"] is not None


def test_http_workspace_reports_shared_error(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    missing = tmp_path / "missing-shared"
    monkeypatch.setenv("RETINUE_SHARED_DIR", str(missing))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
    from .store import RoomStore

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/workspace")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert payload["shared_dir"] == os.path.abspath(str(missing))
        assert payload["shared_mount"] == "/shared"
        assert payload["shared_error"]
        assert "not a directory" in payload["shared_error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_serves_ide_workspace_image(tmp_path, monkeypatch):
    import http.client
    import json
    import threading
    from urllib.parse import quote

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG-bytes")
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
    from .store import RoomStore

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room(workspace="ide", ide_path=str(tmp_path)))
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/rooms/r-1/files?path=" + quote("/workspace/card.png"))
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/png"
        assert resp.read() == b"\x89PNG-bytes"
        conn.close()
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/rooms/r-1/files?path=" + quote("/etc/passwd"))
        resp = conn.getresponse()
        assert resp.status == 400
        assert "workspace" in json.loads(resp.read()).get("error", "")
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_container_ids_prefer_running_then_newest(monkeypatch):
    """#209: serve the CURRENT epoch — running before stopped, newest first
    within each bucket — never whatever order ``ps`` happened to print."""
    from . import workspace as ws

    rows = "\n".join(
        [
            "old-run\tUp 2 hours\t2026-08-22 16:06:43 -0500 CDT",
            "exited-new\tExited (143) About an hour ago\t2026-08-22 17:00:00 -0500 CDT",
            "new-run\tUp 50 minutes\t2026-08-22 17:38:00 -0500 CDT",
            "exited-old\tExited (143) 2 hours ago\t2026-08-22 15:00:00 -0500 CDT",
        ]
    )

    class _Proc:
        stdout = rows

    monkeypatch.setattr(ws, "_runtime", lambda: "podman")
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: _Proc())
    room = ws.Room(id="r-1", name="T", members=["scout"], lead="scout")
    monkeypatch.setattr(ws.ide, "container_key_for_room", lambda _r: "key-1")
    assert ws._container_ids_for_room(room) == [
        "new-run",
        "old-run",
        "exited-new",
        "exited-old",
    ]
