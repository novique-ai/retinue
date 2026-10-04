"""Workspace principal card (issue #55)."""

from __future__ import annotations

import json
import threading

import pytest
from gateway.config import PlatformConfig

from . import principal
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .store import RoomStore


def test_load_missing_is_you(tmp_path):
    assert principal.load(str(tmp_path)) == {
        "display_name": "You",
        "about": "",
        "trusted_senders": [],
    }


def test_save_and_load_roundtrip(tmp_path):
    saved = principal.save(
        str(tmp_path), {"display_name": "Clayton", "about": "Call me Clayton."}
    )
    assert saved["display_name"] == "Clayton"
    assert principal.load(str(tmp_path))["about"] == "Call me Clayton."
    data = json.loads((tmp_path / principal.FILENAME).read_text(encoding="utf-8"))
    assert data["display_name"] == "Clayton"


def test_save_requires_name(tmp_path):
    with pytest.raises(ValueError):
        principal.save(str(tmp_path), {"display_name": "  ", "about": "x"})


def test_speaker_name_uses_principal_for_generic(tmp_path):
    principal.save(str(tmp_path), {"name": "Clayton", "about": ""})
    home = str(tmp_path)
    assert principal.speaker_name(home, "") == "Clayton"
    assert principal.speaker_name(home, "You") == "Clayton"
    assert principal.speaker_name(home, "User") == "Clayton"
    assert principal.speaker_name(home, "Mark") == "Mark"


@pytest.fixture
def httpd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_http_principal_roundtrip(httpd):
    import http.client

    host, port = httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/principal")
    resp = conn.getresponse()
    first = json.loads(resp.read().decode())
    conn.close()
    assert resp.status == 200
    assert first["display_name"] == "You"

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request(
        "PUT",
        "/principal",
        body=json.dumps({"display_name": "Clayton", "about": "Call me Clayton."}),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    saved = json.loads(resp.read().decode())
    conn.close()
    assert resp.status == 200
    assert saved == {
        "display_name": "Clayton",
        "about": "Call me Clayton.",
        "trusted_senders": [],
    }

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/principal")
    resp = conn.getresponse()
    again = json.loads(resp.read().decode())
    conn.close()
    assert again["display_name"] == "Clayton"


def test_load_includes_empty_trusted_senders(tmp_path):
    assert principal.load(str(tmp_path))["trusted_senders"] == []


def test_trusted_senders_roundtrip(tmp_path):
    saved = principal.save(
        str(tmp_path),
        {
            "display_name": "Clayton",
            "about": "",
            "trusted_senders": [
                "Claude-Terminal",
                " claude-terminal ",
                "",
                "ops-bot",
            ],
        },
    )
    assert saved["trusted_senders"] == ["Claude-Terminal", "ops-bot"]
    assert principal.load(str(tmp_path))["trusted_senders"] == [
        "Claude-Terminal",
        "ops-bot",
    ]
    # A save that omits the list clears it. Empty is the fail-safe default.
    cleared = principal.save(str(tmp_path), {"display_name": "Clayton", "about": "x"})
    assert cleared["trusted_senders"] == []
    assert principal.load(str(tmp_path))["trusted_senders"] == []


def test_trusted_senders_reject_bad_shape_without_wiping(tmp_path):
    principal.save(
        str(tmp_path),
        {"display_name": "Clayton", "about": "", "trusted_senders": ["claude-terminal"]},
    )
    with pytest.raises(ValueError):
        principal.save(
            str(tmp_path),
            {"display_name": "Clayton", "about": "", "trusted_senders": "claude-terminal"},
        )
    with pytest.raises(ValueError):
        principal.save(
            str(tmp_path),
            {
                "display_name": "Clayton",
                "about": "",
                "trusted_senders": ["ok", 3],
            },
        )
    with pytest.raises(ValueError):
        principal.save(
            str(tmp_path),
            {
                "display_name": "Clayton",
                "about": "",
                "trusted_senders": ["x" * 81],
            },
        )
    with pytest.raises(ValueError):
        principal.save(
            str(tmp_path),
            {
                "display_name": "Clayton",
                "about": "",
                "trusted_senders": [f"sender-{i}" for i in range(33)],
            },
        )
    assert principal.load(str(tmp_path))["trusted_senders"] == ["claude-terminal"]


def test_load_drops_junk_trusted_senders(tmp_path):
    (tmp_path / principal.FILENAME).write_text(
        json.dumps(
            {
                "display_name": "Clayton",
                "about": "",
                "trusted_senders": ["claude-terminal", "", 3, "x" * 81, "ops-bot"],
            }
        ),
        encoding="utf-8",
    )
    assert principal.load(str(tmp_path))["trusted_senders"] == [
        "claude-terminal",
        "ops-bot",
    ]
    (tmp_path / principal.FILENAME).write_text(
        json.dumps({"display_name": "Clayton", "about": "", "trusted_senders": "nope"}),
        encoding="utf-8",
    )
    assert principal.load(str(tmp_path))["trusted_senders"] == []


def test_http_trusted_senders_roundtrip(httpd):
    import http.client

    host, port = httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/principal")
    resp = conn.getresponse()
    first = json.loads(resp.read().decode())
    conn.close()
    assert resp.status == 200
    assert first["trusted_senders"] == []

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request(
        "PUT",
        "/principal",
        body=json.dumps(
            {
                "display_name": "Clayton",
                "about": "",
                "trusted_senders": ["claude-terminal", "claude-terminal"],
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    saved = json.loads(resp.read().decode())
    conn.close()
    assert resp.status == 200
    assert saved["trusted_senders"] == ["claude-terminal"]

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/principal")
    resp = conn.getresponse()
    again = json.loads(resp.read().decode())
    conn.close()
    assert again["trusted_senders"] == ["claude-terminal"]
