"""Workspace file serving — path rules + IDE read (no gateway)."""

from __future__ import annotations

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
