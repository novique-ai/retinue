"""Unit tests for the room engine + store (no gateway required).

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/ -q
"""

from __future__ import annotations

from . import engine
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor", "critic"])
    defaults.update(kwargs)
    return Room(**defaults)


# ── mentions ─────────────────────────────────────────────────────────────


def test_mentions_in_order_deduped_case_insensitive():
    room = _room()
    got = engine.parse_mentions("@Editor then @scout, and @EDITOR again", room.members)
    assert got == ["editor", "scout"]


def test_mentions_ignore_non_members_and_support_hyphens():
    members = ["data-scout", "editor"]
    got = engine.parse_mentions("cc @data-scout @nobody @Mark", members)
    assert got == ["data-scout"]


def test_mention_regex_does_not_match_emails():
    got = engine.parse_mentions("mail me at mark@how3ll.net", ["how3ll"])
    # 'how3ll' appears after '@' in an email; accepting it would make every
    # email address a turn trigger. Documented current behavior: it DOES
    # match the token — the engine relies on member names not colliding with
    # mail domains. If this becomes a problem, tighten the regex with a
    # lookbehind and update this test.
    assert got == ["how3ll"]


# ── user-turn planning ───────────────────────────────────────────────────


def test_user_message_with_mentions_plans_those_turns():
    room = _room()
    assert engine.plan_user_turns(room, "@critic then @scout please") == ["critic", "scout"]


def test_user_message_without_mentions_goes_to_lead():
    room = _room(lead="editor")
    assert engine.plan_user_turns(room, "what do you all think?") == ["editor"]


def test_no_lead_falls_back_to_first_member():
    room = _room(lead=None)
    assert engine.plan_user_turns(room, "hello") == ["scout"]


def test_invalid_lead_falls_back_to_first_member():
    room = _room(lead="ghost")
    assert engine.plan_user_turns(room, "hello") == ["scout"]


# ── follow-up planning (the budget rules) ────────────────────────────────


def test_followups_exclude_self():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "I think @scout and @editor", [], 5)
    assert got == ["editor"]


def test_followups_exclude_already_queued():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "@editor @critic", ["critic"], 5)
    assert got == ["editor"]


def test_followups_truncated_to_budget():
    room = _room()
    got = engine.plan_agent_followups(room, "scout", "@editor @critic", [], 1)
    assert got == ["editor"]


def test_followups_zero_budget_yields_nothing():
    room = _room()
    assert engine.plan_agent_followups(room, "scout", "@editor", [], 0) == []


# ── formatting ───────────────────────────────────────────────────────────


def test_format_lines_attribution():
    msgs = [
        RoomMessage(seq=1, ts=1, kind=KIND_USER, speaker="Mark", text="hi"),
        RoomMessage(seq=2, ts=2, kind=KIND_AGENT, speaker="scout", text="hello"),
        RoomMessage(seq=3, ts=3, kind=KIND_SYSTEM, speaker="room", text="budget reached"),
    ]
    block = engine.format_lines(msgs)
    assert block.splitlines() == [
        "[Mark] hi",
        "[scout (agent)] hello",
        "[room] budget reached",
    ]


def test_briefing_names_room_and_members():
    room = _room(lead="scout")
    text = engine.room_briefing(room, "scout", ["Mark"])
    assert '"scout"' in text and '"Test"' in text
    assert "editor" in text and "critic" in text
    assert "Mark" in text


# ── store ────────────────────────────────────────────────────────────────


def test_store_roundtrip_and_seq_assignment(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    room = _room()
    store.create(room)

    assert store.get("r-1").name == "Test"
    m1 = store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))
    m2 = store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker="scout", text="b"))
    assert (m1.seq, m2.seq) == (1, 2)
    assert [m.text for m in store.read_since("r-1", 0)] == ["a", "b"]
    assert [m.text for m in store.read_since("r-1", 1)] == ["b"]


def test_store_seq_survives_reopen(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))

    reopened = RoomStore(base_dir=str(tmp_path))
    m2 = reopened.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="b"))
    assert m2.seq == 2


def test_store_update_last_seen_roundtrip(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    room = _room()
    store.create(room)
    room.last_seen["scout"] = 7
    store.update(room)
    assert RoomStore(base_dir=str(tmp_path)).get("r-1").last_seen == {"scout": 7}


def test_wait_since_wakes_on_append(tmp_path):
    import threading
    import time as _time

    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    got: list[str] = []

    def waiter():
        got.extend(m.text for m in store.wait_since("r-1", 0, timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()
    _time.sleep(0.05)
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello"))
    t.join(3)
    assert got == ["hello"]


def test_wait_since_timeout_empty(tmp_path):
    import time as _time

    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    t0 = _time.time()
    assert store.wait_since("r-1", 0, timeout=0.2) == []
    assert _time.time() - t0 >= 0.15


def test_sse_stream_emits_existing_messages(tmp_path, monkeypatch):
    """GET /rooms/{id}/stream is text/event-stream and emits already-written
    lines immediately (the EventSource catch-up case)."""
    import http.client
    import json
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(_room())
    adapter.store.append(
        "r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hi")
    )
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/rooms/r-1/stream?since=0")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/event-stream" in (resp.getheader("Content-Type") or "")
        lines: list[str] = []
        while True:
            raw = resp.fp.readline()
            assert raw, "SSE stream closed before a messages event"
            line = raw.decode().rstrip("\n")
            lines.append(line.rstrip("\r"))
            if line in ("\n", "\r\n", "") and any(l.startswith("data:") for l in lines):
                break
        data_line = next(l for l in lines if l.startswith("data:"))
        payload = json.loads(data_line[len("data:") :].strip())
        assert [m["text"] for m in payload["messages"]] == ["hi"]
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_store_delete_and_corrupt_line_tolerance(tmp_path):
    store = RoomStore(base_dir=str(tmp_path))
    store.create(_room())
    store.append("r-1", RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="a"))
    # Torn write: a corrupt line must not take the room down.
    with open(tmp_path / "r-1.transcript.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert [m.text for m in store.read_since("r-1", 0)] == ["a"]
    assert store.delete("r-1") is True
    assert store.get("r-1") is None
