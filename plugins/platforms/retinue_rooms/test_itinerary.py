"""Room itinerary store + HTTP (issue #37)."""

from __future__ import annotations

import json

import pytest
from gateway.config import PlatformConfig

from . import engine, itinerary
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import Room
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-plan", name="Plan", members=["sally", "editor"], lead="sally")
    defaults.update(kwargs)
    return Room(**defaults)


def test_empty_itinerary_shape():
    empty = itinerary.empty("r-plan")
    assert empty["room_id"] == "r-plan"
    assert empty["items"] == []
    assert empty["summary"] == ""
    assert empty["title"] == ""


def test_save_round_trip_and_normalize(tmp_path):
    home = str(tmp_path)
    itinerary.save(
        home,
        "r-plan",
        {
            "title": "Ship I1",
            "summary": "Lucy made a really bad ",
            "items": [
                {"text": "Blog live", "status": "done"},
                {"id": "keep-me", "text": "FB/IG walkthrough", "status": "DOING"},
                {"text": "   ", "status": "todo"},
                {"text": "Ignore me", "status": "nope"},
            ],
        },
        updated_by="user",
    )
    got = itinerary.load(home, "r-plan")
    assert got["title"] == "Ship I1"
    assert got["summary"] == "Lucy made a really bad "
    assert [i["text"] for i in got["items"]] == [
        "Blog live",
        "FB/IG walkthrough",
        "Ignore me",
    ]
    assert got["items"][0]["status"] == "done"
    assert got["items"][1]["id"] == "keep-me"
    assert got["items"][1]["status"] == "doing"
    assert got["items"][2]["status"] == "todo"
    assert got["updated_by"] == "user"
    assert got["updated_at"] > 0


def test_summary_keeps_trailing_space_so_the_next_word_can_be_typed(tmp_path):
    """The pane saves on each keystroke. strip() ate the space after the
    fifth word and the field could not grow."""
    itinerary.save(
        str(tmp_path),
        "r-plan",
        {"title": "", "summary": "Lucy made a really bad ", "items": []},
        updated_by="user",
    )
    assert itinerary.load(str(tmp_path), "r-plan")["summary"] == "Lucy made a really bad "


def test_missing_file_is_empty(tmp_path):
    got = itinerary.load(str(tmp_path), "nope")
    assert got["items"] == []
    assert got["room_id"] == "nope"


def test_parse_fence_reads_lead_block():
    text = """Plan is medium. @Dave take the API.

```itinerary
title: Auth cutover
where: Dave on the form; Junior next
- [doing] Dave: login form
- [todo] Junior: tests
- [done] Spike
```
"""
    got = itinerary.parse_fence(text)
    assert got is not None
    assert got["title"] == "Auth cutover"
    assert "Dave on the form" in got["summary"]
    assert [i["status"] for i in got["items"]] == ["doing", "todo", "done"]
    assert got["items"][0]["text"].startswith("Dave:")
    assert itinerary.parse_fence("no fence") is None


def test_briefing_includes_itinerary_for_lead():
    room = _room()
    plan = {
        "title": "I1",
        "summary": "Waiting on FB/IG.",
        "items": [
            {"id": "a", "text": "X thread", "status": "done"},
            {"id": "b", "text": "FB/IG", "status": "doing"},
        ],
    }
    text = engine.room_briefing(room, "sally", ["You"], itinerary=plan)
    assert "```itinerary" in text
    assert "[done] X thread" in text
    assert "[doing] FB/IG" in text
    assert "Waiting on FB/IG." in text
    assert "you write it" in text.lower()


def test_briefing_without_itinerary_unchanged():
    room = _room()
    text = engine.room_briefing(room, "editor", ["You"])
    assert "```itinerary" not in text


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
    yield server, adapter, tmp_path
    server.shutdown()
    server.server_close()


def _call(httpd, method, path, body=None):
    import http.client

    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
    raw = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if raw is not None else {}
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode())
    conn.close()
    return resp.status, payload


def test_http_get_put_itinerary(httpd):
    server, _adapter, _home = httpd
    status, payload = _call(server, "GET", "/rooms/r-plan/itinerary")
    assert status == 200
    assert payload["items"] == []

    status, payload = _call(
        server,
        "PUT",
        "/rooms/r-plan/itinerary",
        {
            "title": "I1",
            "summary": "Halfway.",
            "items": [{"text": "Post X", "status": "doing"}],
        },
    )
    assert status == 200
    assert payload["title"] == "I1"
    assert payload["items"][0]["text"] == "Post X"
    assert payload["items"][0]["status"] == "doing"

    status, again = _call(server, "GET", "/rooms/r-plan/itinerary")
    assert again["summary"] == "Halfway."
    assert again["items"][0]["text"] == "Post X"


@pytest.mark.asyncio
async def test_lead_notify_saves_itinerary_fence(httpd):
    from concurrent.futures import Future

    from .adapter import _PendingTurn

    _server, adapter, home = httpd
    fut: Future = Future()
    with adapter._pending_lock:
        adapter._pending[("r-plan", "sally")] = _PendingTurn(
            task_id="t1", room_id="r-plan", member="sally", future=fut
        )
    await adapter.send(
        "r-plan",
        "On it.\n\n```itinerary\ntitle: Ship\nwhere: scoping\n- [doing] Spike\n```\n",
        metadata={"notify": True, "thread_id": "sally"},
    )
    got = itinerary.load(str(home), "r-plan")
    assert got["title"] == "Ship"
    assert got["items"][0]["text"] == "Spike"
    assert got["updated_by"] == "sally"


def test_http_itinerary_unknown_room(httpd):
    server, _adapter, _home = httpd
    status, payload = _call(server, "GET", "/rooms/missing/itinerary")
    assert status == 404
    status, payload = _call(server, "PUT", "/rooms/missing/itinerary", {"items": []})
    assert status == 404
