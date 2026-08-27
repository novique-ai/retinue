"""Web UI static serving.

Two contributor issues share this code path:

  - #9  web_dist_dir()'s search order (env override -> source tree ->
        well-known XDG prefix), so a pip-only install with no git checkout
        can still find a built UI.
  - #1  a helpful HTML page instead of a bare JSON 404 when no dist/ was
        ever built anywhere.
"""

from __future__ import annotations

import http.client
import os
import threading

import pytest

from gateway.config import PlatformConfig

from . import adapter as adapter_module
from .adapter import (
    RetinueRoomsAdapter,
    _RoomsRequestHandler,
    _RoomsServer,
    _WEB_UI_NOT_BUILT_HTML,
)
from .store import RoomStore


def _real_source_tree_dist() -> str:
    """Recompute step 2 of web_dist_dir()'s search order (retinue-web/dist
    resolved relative to adapter.py) the same way the implementation does.
    Lets tests force that branch true/false deterministically without
    touching the real retinue-web/ tree, which is owned by other work."""
    here = os.path.abspath(adapter_module.__file__)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    return os.path.join(repo_root, "retinue-web", "dist")


def _patch_source_tree(monkeypatch, *, exists: bool) -> None:
    source_tree = _real_source_tree_dist()
    real_isdir = os.path.isdir

    def fake_isdir(path):
        if os.path.abspath(path) == source_tree:
            return exists
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", fake_isdir)


# ── web_dist_dir() search order (issue #9) ─────────────────────────────────


def test_env_override_wins_over_source_tree_and_well_known(tmp_path, monkeypatch):
    _patch_source_tree(monkeypatch, exists=True)  # simulate a built source-tree dist
    xdg = tmp_path / "xdg"
    well_known = xdg / "retinue" / "web-dist"
    well_known.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    override = tmp_path / "override-dist"
    override.mkdir()
    monkeypatch.setenv("RETINUE_ROOMS_WEB_DIST", str(override))

    assert RetinueRoomsAdapter.web_dist_dir() == str(override)


def test_env_override_ignored_when_directory_missing(tmp_path, monkeypatch):
    _patch_source_tree(monkeypatch, exists=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.setenv("RETINUE_ROOMS_WEB_DIST", str(tmp_path / "does-not-exist"))

    assert RetinueRoomsAdapter.web_dist_dir() is None


def test_source_tree_used_when_no_override(tmp_path, monkeypatch):
    _patch_source_tree(monkeypatch, exists=True)
    monkeypatch.delenv("RETINUE_ROOMS_WEB_DIST", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-unused"))

    assert RetinueRoomsAdapter.web_dist_dir() == _real_source_tree_dist()


def test_well_known_prefix_used_as_last_resort_with_fake_prefix(tmp_path, monkeypatch):
    """Acceptance criterion: 'a test with a fake prefix.' Neither the env
    override nor the source tree resolves; a directory dropped at the
    documented $XDG_DATA_HOME/retinue/web-dist prefix is picked up."""
    _patch_source_tree(monkeypatch, exists=False)
    monkeypatch.delenv("RETINUE_ROOMS_WEB_DIST", raising=False)
    fake_prefix = tmp_path / "fake-xdg-data-home"
    well_known = fake_prefix / "retinue" / "web-dist"
    well_known.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_prefix))

    assert RetinueRoomsAdapter.web_dist_dir() == str(well_known)


def test_well_known_prefix_falls_back_to_local_share_without_xdg_env(tmp_path, monkeypatch):
    _patch_source_tree(monkeypatch, exists=False)
    monkeypatch.delenv("RETINUE_ROOMS_WEB_DIST", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    well_known = tmp_path / ".local" / "share" / "retinue" / "web-dist"
    well_known.mkdir(parents=True)
    real_expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else real_expanduser(p)
    )

    assert RetinueRoomsAdapter.web_dist_dir() == str(well_known)


def test_none_when_nothing_found_anywhere(tmp_path, monkeypatch):
    _patch_source_tree(monkeypatch, exists=False)
    monkeypatch.delenv("RETINUE_ROOMS_WEB_DIST", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-empty"))  # no retinue/web-dist under it

    assert RetinueRoomsAdapter.web_dist_dir() is None


# ── helpful HTML page when dist is entirely missing (issue #1) ─────────────


@pytest.fixture
def httpd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, adapter
    server.shutdown()
    server.server_close()


def _raw_get(httpd, path):
    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    content_type = resp.getheader("Content-Type")
    conn.close()
    return resp.status, content_type, body


def test_root_serves_setup_html_when_dist_missing(httpd):
    server, adapter = httpd
    adapter.web_dist_dir = lambda: None  # no dist anywhere, deterministically

    status, content_type, body = _raw_get(server, "/")

    assert status == 404
    assert content_type is not None and content_type.startswith("text/html")
    text = body.decode("utf-8")
    assert text == _WEB_UI_NOT_BUILT_HTML
    assert "npm run build" in text
    assert "retinue-dev-setup.sh" in text
    # This must not be the old bare JSON error a fresh contributor used to see.
    assert "error" not in text.lower()


def test_non_api_path_also_serves_setup_html_when_dist_missing(httpd):
    server, adapter = httpd
    adapter.web_dist_dir = lambda: None

    status, content_type, body = _raw_get(server, "/some/deep/spa/route")

    assert status == 404
    assert content_type is not None and content_type.startswith("text/html")
    assert b"npm run build" in body


def test_spa_served_unchanged_when_dist_present(tmp_path, httpd):
    server, adapter = httpd
    dist = tmp_path / "fake-dist"
    dist.mkdir()
    index_html = "<!doctype html><html><body>real spa shell</body></html>"
    (dist / "index.html").write_text(index_html, encoding="utf-8")
    adapter.web_dist_dir = lambda: str(dist)

    status, content_type, body = _raw_get(server, "/")

    assert status == 200
    assert content_type is not None and content_type.startswith("text/html")
    assert body.decode("utf-8") == index_html
    assert body.decode("utf-8") != _WEB_UI_NOT_BUILT_HTML


def test_json_404_preserved_for_missing_asset_within_existing_dist(tmp_path, httpd):
    """Regression guard: the terse JSON 404 branch in do_GET is only for a
    dist/ that exists but can't resolve this specific request (e.g. a path
    traversal attempt) — not for the "no dist anywhere" case above."""
    server, adapter = httpd
    dist = tmp_path / "fake-dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    adapter.web_dist_dir = lambda: str(dist)

    status, content_type, body = _raw_get(server, "/../../../../etc/passwd")

    assert status == 404
    assert content_type == "application/json"
    assert b"web UI not built" in body


def test_source_tree_ships_apple_touch_icon():
    """iOS home-screen bookmarks ignore the small favicon PNG. The SPA must
    ship a 256x256 apple-touch-icon and declare it in index.html so Vite
    copies it into dist/ (the adapter serves dist/ from disk)."""
    here = os.path.abspath(adapter_module.__file__)
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(here)))
    )
    index = os.path.join(repo_root, "retinue-web", "index.html")
    png = os.path.join(repo_root, "retinue-web", "public", "apple-touch-icon.png")
    with open(index, encoding="utf-8") as f:
        html = f.read()
    assert 'rel="apple-touch-icon"' in html
    assert "apple-touch-icon.png" in html
    assert os.path.isfile(png), png
    with open(png, "rb") as f:
        header = f.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    assert (width, height) == (256, 256)
