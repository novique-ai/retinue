"""Composer attachments (issue #38)."""

from __future__ import annotations

import json
import os
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


def test_harvest_publishes_profile_image_cache(tmp_path):
    cache = tmp_path / "profiles" / "sheila" / "image_cache"
    cache.mkdir(parents=True)
    src = cache / "graphics_test_midnight_monolith.png"
    src.write_bytes(b"png-bytes")
    os.utime(src, (2_000, 2_000))
    got = attachments.harvest(str(tmp_path), "r-1", "sheila", since=1_000, reply="")
    assert [g["path"] for g in got] == [
        "/workspace/uploads/graphics_test_midnight_monolith.png"
    ]
    listed = attachments.list_uploads(str(tmp_path), "r-1")
    assert listed[0]["name"] == "graphics_test_midnight_monolith.png"
    # Second harvest is a no-op — already in the catalog.
    assert attachments.harvest(str(tmp_path), "r-1", "sheila", since=1_000, reply="") == []


def test_harvest_skips_older_unrelated_files(tmp_path):
    cache = tmp_path / "profiles" / "sheila" / "image_cache"
    cache.mkdir(parents=True)
    old = cache / "retinue-intro-16x9.png"
    old.write_bytes(b"old")
    os.utime(old, (10, 10))
    assert attachments.harvest(str(tmp_path), "r-1", "sheila", since=1_000, reply="") == []


def test_harvest_recalls_older_file_by_name(tmp_path):
    cache = tmp_path / "profiles" / "sheila" / "image_cache"
    cache.mkdir(parents=True)
    src = cache / "graphics_test_midnight_monolith.png"
    src.write_bytes(b"png-bytes")
    os.utime(src, (10, 10))
    got = attachments.harvest(
        str(tmp_path),
        "r-1",
        "sheila",
        since=1_000,
        reply="@Sheila show me that Midnight Monolith image again",
    )
    assert got[0]["path"].endswith("graphics_test_midnight_monolith.png")


def test_host_media_for_attached_image(tmp_path):
    meta = attachments.save(str(tmp_path), "r-1", "FLUX_00001_.png", b"png-bytes")
    urls, types = attachments.host_media_for_text(
        str(tmp_path),
        "r-1",
        "@Lucy describe the image I just attached\n" + meta["path"],
    )
    assert urls == [attachments.host_path(str(tmp_path), "r-1", "FLUX_00001_.png")]
    assert types == ["image/png"]


def test_sync_uploads_into_ide_host_tree(tmp_path):
    from .engine import Room

    attachments.save(str(tmp_path), "r-1", "notes.txt", b"hello")
    ide_root = tmp_path / "project"
    ide_root.mkdir()
    room = Room(
        id="r-1",
        name="Lab",
        members=["lucy"],
        workspace="ide",
        ide_path=str(ide_root),
    )
    attachments.sync_uploads_into_room(str(tmp_path), room)
    dest = ide_root / "uploads" / "notes.txt"
    assert dest.is_file()
    assert dest.read_bytes() == b"hello"


def test_matching_uploads_and_path_append(tmp_path):
    attachments.save(str(tmp_path), "r-1", "graphics_test_midnight_monolith.png", b"png")
    hits = attachments.matching_uploads(
        str(tmp_path), "r-1", "show me the Midnight Monolith again"
    )
    assert hits[0]["path"] == "/workspace/uploads/graphics_test_midnight_monolith.png"
    text = attachments.with_published_paths("Here it is.", hits)
    assert "/workspace/uploads/graphics_test_midnight_monolith.png" in text
    assert attachments.with_published_paths("", hits).startswith("/workspace/uploads/")


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
