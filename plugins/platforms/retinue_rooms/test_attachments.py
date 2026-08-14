"""Composer attachments (issue #38)."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from gateway.config import PlatformConfig

from . import attachments
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import Room
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Attach", members=["sally"], lead="sally")
    defaults.update(kwargs)
    return Room(**defaults)


def test_safe_name_and_save(tmp_path):
    meta = attachments.save(str(tmp_path), "r-1", "../weird photo.PNG", b"png-bytes")
    assert meta["name"] == "weird_photo.PNG"
    assert meta["path"] == "/workspace/uploads/weird_photo.PNG"
    assert meta["image"] is True
    got = attachments.read_upload(str(tmp_path), "r-1", meta["path"])
    assert got is not None
    data, ctype = got
    assert data == b"png-bytes"
    assert ctype == "image/png"


def test_empty_and_oversize(tmp_path):
    with pytest.raises(ValueError):
        attachments.save(str(tmp_path), "r-1", "a.txt", b"")
    with pytest.raises(ValueError):
        attachments.save(str(tmp_path), "r-1", "a.txt", b"x" * (attachments.MAX_ATTACHMENT + 1))


def test_non_upload_path_is_none(tmp_path):
    assert attachments.read_upload(str(tmp_path), "r-1", "/workspace/other.txt") is None


@pytest.fixture
def httpd(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room())
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_http_upload_and_fetch(httpd):
    import http.client

    host, port = httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request(
        "POST",
        "/rooms/r-1/attachments?filename=" + quote("shot.jpg"),
        body=b"\xff\xd8jpeg",
        headers={"Content-Type": "image/jpeg"},
    )
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode())
    conn.close()
    assert resp.status == 201
    assert payload["path"] == "/workspace/uploads/shot.jpg"
    assert payload["image"] is True

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/rooms/r-1/files?path=" + quote(payload["path"]))
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "image/jpeg"
    assert body == b"\xff\xd8jpeg"
