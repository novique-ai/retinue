"""Principal approval of Janus confirm-tier requests (novique-ai/retinue#266).

Run:
  scripts/run_tests.sh plugins/platforms/retinue_rooms/test_janus_approval.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from gateway.config import PlatformConfig

from . import engine, grokbuild, janus_approval, principal
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import KIND_AGENT, KIND_TOOL, KIND_USER, Room, RoomMessage
from .janus_approval import (
    JANUS_MCP_SERVER_ENV,
    TOKEN_ENV,
    URL_ENV,
    JanusApprovalError,
    approval_id_from_tool_result,
    client_from_env,
    compose_tool_complete_callbacks,
    configured_janus_mcp_server,
    extract_approval_request_ids,
    expires_in_past,
    is_janus_mcp_tool,
    room_tool_complete_callback,
    trusted_retinue_room,
)
from .store import RoomStore

APPROVAL_ID = "req-1001"
OTHER_ID = "req-2002"
SECRET = "s3cret-arg-value"
LEAK = "gateway-should-not-leak"
TOKEN = "gw-token-value"
JANUS_TOOL = "mcp__janus__capability_call"
CODEX_JANUS_TOOL = "mcp.janus.capability_call"


class FakeJanus:
    def __init__(self):
        self.records = {}
        self.gets = []
        self.inspects = []
        self.decisions = []
        self.fail_get = None
        self.fail_inspect = None

    def get(self, approval_id):
        self.gets.append(approval_id)
        if self.fail_get is not None:
            raise self.fail_get
        if approval_id not in self.records:
            raise JanusApprovalError("not_found")
        return dict(self.records[approval_id])

    def inspect(self, approval_id):
        """Tripwire. Room binding does not read the operator payload."""
        self.inspects.append(approval_id)
        raise AssertionError("operator inspect is not a room bind")

    def decide(self, approval_id, decision):
        self.decisions.append((approval_id, decision))
        record = dict(self.records.get(approval_id) or {})
        status = "approved" if decision == "approve" else "denied"
        record["status"] = status
        record["decision"] = decision
        self.records[approval_id] = record
        return {
            "approval_request_id": approval_id,
            "status": status,
            "decision": decision,
            "duplicate": False,
            "arguments": record.get("arguments"),
            "token": LEAK,
        }


def _pending(approval_id=APPROVAL_ID, **over):
    record = {
        "approval_request_id": approval_id,
        "status": "pending",
        "identity": "member:scout",
        "capability_id": "files.read",
        "arguments": {"path": "/tmp/x", "password": SECRET},
        "env": {"MODE": "confirm"},
        "reason": "read a file",
        "expires_at": "2099-01-01T00:00:00Z",
        "token": LEAK,
    }
    record.update(over)
    return record


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(JANUS_MCP_SERVER_ENV, raising=False)
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter._janus_client_override = None
    return adapter


def _room(room_id="room-a"):
    return Room(
        id=room_id,
        name=room_id,
        members=["scout", "editor"],
        lead="scout",
        max_followup_rounds=0,
    )


def _open(adapter, room_id="room-a"):
    room = _room(room_id)
    adapter.store.create(room)
    return room


def _say(adapter, room_id, text, speaker="scout", kind=KIND_AGENT):
    message = adapter.store.append(
        room_id,
        RoomMessage(seq=0, ts=0, kind=kind, speaker=speaker, text=text),
    )
    adapter._note_posted(room_id, message)
    return message


def _confirmation(approval_id=APPROVAL_ID, **over):
    record = {
        "status": "needs_confirmation",
        "approval_request_id": approval_id,
        "capability_id": "files.read",
        "reason": "read a file",
        "preview": {"arg_keys": ["path", "password"]},
        "arguments": {"path": "/tmp/x", "password": SECRET},
        "token": TOKEN,
    }
    record.update(over)
    return record


def _envelope(approval_id=APPROVAL_ID, **over):
    body = _confirmation(approval_id, **over)
    return json.dumps({"result": json.dumps(body), "structuredContent": body})


def _observe(adapter, room_id, approval_id=APPROVAL_ID, tool=JANUS_TOOL, result=None):
    payload = _envelope(approval_id) if result is None else result
    return adapter.observe_janus_tool_result(room_id, tool, payload)


def _claim(adapter, room_id, approval_id=APPROVAL_ID):
    assert _observe(adapter, room_id, approval_id) is True
    return _say(adapter, room_id, f"approval_request_id: {approval_id}")


def _texts(adapter, room_id):
    return [message.text for message in adapter.store.read_since(room_id, 0)]


def _binding_text(adapter):
    path = os.path.join(adapter.store.base_dir, "janus_approvals.json")
    if not os.path.isfile(path):
        return ""
    return open(path, encoding="utf-8").read()


class _Fut:
    def result(self, timeout=None):
        return None


@contextmanager
def _armed(adapter, monkeypatch):
    wakes = []
    loop = asyncio.new_event_loop()
    adapter._loop = loop

    def capture(*_args, **_kwargs):
        wakes.append(1)
        return _Fut()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", capture)
    try:
        yield wakes
    finally:
        adapter._loop = None
        loop.close()


def _refused(adapter, room_id, approval_id, origin):
    try:
        adapter.decide_janus_approval(room_id, approval_id, "approve", origin=origin)
    except PermissionError:
        return True
    return False


# ── pure helpers ──────────────────────────────────────────────────────────


def test_extract_requires_the_label_and_a_full_id():
    assert extract_approval_request_ids(
        "Need a decision. approval_request_id: req-1001"
    ) == ["req-1001"]
    assert extract_approval_request_ids("approval_request_id=req-1001") == ["req-1001"]
    assert extract_approval_request_ids('{"approval_request_id": "req-1001"}') == ["req-1001"]
    assert extract_approval_request_ids('approval_request_id\\":\\"req-1001\\"') == ["req-1001"]
    assert extract_approval_request_ids("the id is req-1001") == []
    assert extract_approval_request_ids("approval_request_id: abc") == []
    assert extract_approval_request_ids(
        "approval_request_id: req-1001 and approval_request_id: req-2002"
    ) == ["req-1001", "req-2002"]


def test_tool_result_parser_accepts_only_one_janus_confirmation():
    body = _confirmation()
    text = json.dumps(body)
    assert configured_janus_mcp_server({}) == "janus"
    assert configured_janus_mcp_server({JANUS_MCP_SERVER_ENV: "  "}) == ""
    assert is_janus_mcp_tool(JANUS_TOOL, "janus") is True
    assert is_janus_mcp_tool(CODEX_JANUS_TOOL, "janus") is True
    assert is_janus_mcp_tool("terminal", "janus") is False
    assert is_janus_mcp_tool("mcp__janus_evil__capability_call", "janus") is False
    assert is_janus_mcp_tool(JANUS_TOOL, "") is False
    for payload in (
        text,
        json.dumps({"result": body}),
        json.dumps({"result": text}),
        json.dumps({"structuredContent": body}),
        json.dumps({"result": text, "structuredContent": body}),
        {"result": body, "structuredContent": body},
    ):
        assert approval_id_from_tool_result(JANUS_TOOL, payload, server="janus") == APPROVAL_ID
    assert approval_id_from_tool_result(CODEX_JANUS_TOOL, text, server="janus") == APPROVAL_ID
    prose = f"approval_request_id: {APPROVAL_ID} password={SECRET}"
    assert approval_id_from_tool_result(JANUS_TOOL, prose, server="janus") is None
    assert approval_id_from_tool_result(
        JANUS_TOOL,
        json.dumps({"result": prose, "structuredContent": body}),
        server="janus",
    ) == APPROVAL_ID
    assert approval_id_from_tool_result("terminal", _envelope(), server="janus") is None
    assert approval_id_from_tool_result(
        "mcp__other__capability_call", _envelope(), server="janus"
    ) is None
    assert approval_id_from_tool_result(
        JANUS_TOOL, json.dumps({"result": _confirmation(status="pending")}), server="janus"
    ) is None
    other = _confirmation(OTHER_ID)
    assert approval_id_from_tool_result(
        JANUS_TOOL,
        json.dumps({"result": text, "structuredContent": other}),
        server="janus",
    ) is None
    doubled = dict(body)
    doubled["preview"] = {"approval_request_id": APPROVAL_ID}
    assert approval_id_from_tool_result(
        JANUS_TOOL, json.dumps({"result": doubled}), server="janus"
    ) is None
    assert approval_id_from_tool_result(JANUS_TOOL, text[:-1], server="janus") is None
    assert approval_id_from_tool_result(
        JANUS_TOOL,
        json.dumps({"result": text[:-1], "structuredContent": body}),
        server="janus",
    ) is None
    titled = f"capability_call approval_request_id: {APPROVAL_ID}"
    assert approval_id_from_tool_result(titled, _envelope(), server="janus") is None


def test_codex_mcp_completion_truncation_fails_closed():
    """The app-server bridge slices the result. No live frame is pinned here."""
    from agent.codex_runtime import _codex_item_completion_payload

    small = {
        "type": "mcpToolCall",
        "server": "janus",
        "tool": "capability_call",
        "result": _confirmation(),
    }
    text, is_error = _codex_item_completion_payload(small)
    assert is_error is False
    assert approval_id_from_tool_result(CODEX_JANUS_TOOL, text, server="janus") == APPROVAL_ID
    huge = {
        "type": "mcpToolCall",
        "server": "janus",
        "tool": "capability_call",
        "result": _confirmation(preview={"blob": "x" * 5000, "password": SECRET}),
    }
    truncated, _is_error = _codex_item_completion_payload(huge)
    assert len(truncated) == 4000
    assert approval_id_from_tool_result(CODEX_JANUS_TOOL, truncated, server="janus") is None
    assert SECRET not in "bound"


def test_expires_in_past_fails_closed():
    assert expires_in_past(None) is False
    assert expires_in_past("") is False
    assert expires_in_past("2099-01-01T00:00:00Z") is False
    assert expires_in_past("2000-01-01T00:00:00Z") is True
    assert expires_in_past("tomorrow") is True
    assert expires_in_past(True) is True
    assert expires_in_past(1) is True
    assert expires_in_past(4102444800000) is False


def test_client_from_env_rejects_missing_and_credentialed_urls(caplog):
    caplog.set_level(logging.INFO)
    assert client_from_env({}) is None
    assert client_from_env({URL_ENV: "https://example.test", TOKEN_ENV: ""}) is None
    assert client_from_env({URL_ENV: "ftp://example.test", TOKEN_ENV: "t"}) is None
    assert client_from_env({URL_ENV: "http://user:s3cret@example.test/v1", TOKEN_ENV: "t"}) is None
    client = client_from_env({URL_ENV: "https://example.test/ignored", TOKEN_ENV: TOKEN})
    assert client is not None
    assert client._base_url == "https://example.test"
    assert TOKEN not in repr(client)
    assert "s3cret" not in caplog.text


def test_urllib_client_uses_bearer_and_hides_upstream_bodies(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _json(self, status, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _seen(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body.decode("utf-8") if body else "",
                }
            )

        def do_GET(self):
            self._seen()
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._json(401, {"error": "no", "arguments": {"password": SECRET}})
            if self.path.endswith("/missing"):
                return self._json(404, {"arguments": {"password": SECRET}})
            if self.path.rsplit("/", 1)[-1] == "ab.cd":
                return self._json(200, _pending("ab.cd"))
            return self._json(500, {"arguments": {"password": SECRET}, "token": TOKEN})

        def do_POST(self):
            self._seen()
            # Decision URLs are /v1/approvals/{id}/decision, so the id is
            # not the final path segment.
            if "/conflict/" in self.path:
                return self._json(
                    409,
                    {"status": "denied", "arguments": {"password": SECRET}, "token": TOKEN},
                )
            return self._json(200, {"status": "approved", "decision": "approve", "arguments": {"password": SECRET}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        client = janus_approval.JanusApprovalsClient(origin, TOKEN, timeout=3)
        detail = client.get("ab.cd")
        assert detail["arguments"] == {"path": "/tmp/x", "password": SECRET}
        assert "token" not in detail
        assert LEAK not in json.dumps(detail)
        decided = client.decide("ab.cd", "approve")
        assert decided["duplicate"] is False
        assert "arguments" not in decided
        with pytest.raises(JanusApprovalError) as caught:
            client.get("missing")
        assert SECRET not in str(caught.value)
        assert TOKEN not in str(caught.value)
        assert TOKEN not in repr(caught.value)
        with pytest.raises(JanusApprovalError) as conflict:
            client.decide("conflict", "approve")
        assert conflict.value.code == "already_decided"
        assert SECRET not in str(conflict.value)
        assert TOKEN not in str(conflict.value)
    finally:
        server.shutdown()
        server.server_close()
    assert seen[0]["path"] == "/v1/approvals/ab.cd"
    assert seen[0]["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in seen[0]["path"]
    assert seen[1]["path"] == "/v1/approvals/ab.cd/decision"
    assert json.loads(seen[1]["body"]) == {"decision": "approve"}
    assert SECRET not in caplog.text
    assert TOKEN not in caplog.text


def test_scrub_drops_gateway_env_and_exact_token_headers(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv(URL_ENV, "https://example.test")
    rooms_key = "rooms-browser-key"
    env = {
        "PATH": "/bin",
        URL_ENV: "https://example.test",
        TOKEN_ENV: TOKEN,
        "RETINUE_ROOMS_API_KEY": rooms_key,
        "GROK_SANDBOX": "inherit-me",
    }
    cleaned = grokbuild.member_subprocess_env(
        env,
        {"FOO": "bar", TOKEN_ENV: "sekret-extra"},
        grok_home_dir="/tmp/grok-home",
        auth="/tmp/auth.json",
        sandbox="workspace",
    )
    assert cleaned["PATH"] == "/bin"
    assert "FOO" not in cleaned
    assert cleaned["GROK_HOME"] == "/tmp/grok-home"
    assert cleaned["GROK_AUTH_PATH"] == "/tmp/auth.json"
    assert cleaned["GROK_SANDBOX"] == "workspace"
    assert URL_ENV not in cleaned
    assert TOKEN_ENV not in cleaned
    assert "RETINUE_ROOMS_API_KEY" not in cleaned
    assert TOKEN not in cleaned.values()
    assert rooms_key not in cleaned.values()
    assert "sekret-extra" not in cleaned.values()
    empty_sandbox = grokbuild.member_subprocess_env(
        {"PATH": "/bin"},
        None,
        grok_home_dir="h",
        auth="a",
        sandbox="",
    )
    assert "GROK_SANDBOX" not in empty_sandbox

    path = grokbuild.mcp_config_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "servers": [
                    {
                        "name": "broker",
                        "command": "/bin/client",
                        "args": ["--x"],
                        "env": {"K": "V", TOKEN_ENV: TOKEN, URL_ENV: "https://example.test"},
                    },
                    {
                        "name": "docs",
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "headers": {"Authorization": f"Bearer {TOKEN}", "X-Other": "Bearer t"},
                    },
                ]
            },
            handle,
        )
    servers = grokbuild.mcp_servers(str(tmp_path))
    assert servers[0]["env"] == [{"name": "K", "value": "V"}]
    assert servers[1]["headers"] == [{"name": "X-Other", "value": "Bearer t"}]
    dumped = json.dumps(servers)
    assert TOKEN not in dumped
    assert rooms_key not in dumped
    assert "grokbuild: gateway credential removed from member MCP config" in caplog.text
    assert TOKEN not in caplog.text


def test_member_process_start_scrubs_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv(URL_ENV, "https://example.test")
    monkeypatch.setattr(grokbuild, "grok_binary", lambda: "/bin/grok")
    captured = {}

    async def fake_exec(*_argv, **kwargs):
        captured["env"] = kwargs.get("env")
        raise OSError("stop")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    proc = grokbuild.AcpProcess(
        str(tmp_path),
        env_extra={
            TOKEN_ENV: "sekret-extra",
            "FOO": "bar",
            "RETINUE_ROOMS_API_KEY": "rooms-browser-key",
        },
    )
    with pytest.raises(grokbuild.GrokBuildUnavailable):
        asyncio.run(proc.start())
    env = captured["env"]
    assert TOKEN_ENV not in env
    assert URL_ENV not in env
    assert "RETINUE_ROOMS_API_KEY" not in env
    assert "FOO" not in env
    assert TOKEN not in env.values()
    assert "sekret-extra" not in env.values()
    assert "rooms-browser-key" not in env.values()


def test_briefing_tells_the_member_to_surface_the_id_and_wait():
    text = engine.room_briefing(_room(), "scout", ["You"])
    assert "approval_request_id: <id>" in text
    assert "stop and wait" in text
    assert "Do not approve, deny, or confirm it yourself" in text
    assert "only if you retry it after they approve" in text


# ── surfacing ─────────────────────────────────────────────────────────────


def _arm_fake(adapter):
    fake = FakeJanus()
    fake.records[APPROVAL_ID] = _pending()
    adapter._janus_client_override = fake
    return fake


def test_pending_fetch_pauses_with_a_gateway_line_and_keeps_arguments_off_the_transcript(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    assert _observe(adapter, "room-a") is True
    assert fake.gets == []
    _say(adapter, "room-a", f"Need a decision. approval_request_id: {APPROVAL_ID}")
    room = adapter.store.get("room-a")
    assert room.needs_user is True
    lines = _texts(adapter, "room-a")
    gateway = [line for line in lines if line.startswith(janus_approval.LINE_PREFIX)]
    assert gateway == [janus_approval.gateway_approval_line(APPROVAL_ID)]
    assert "@user" in gateway[0]
    assert SECRET not in "\n".join(lines)
    assert LEAK not in "\n".join(lines)
    assert TOKEN not in "\n".join(lines)
    stored = _binding_text(adapter)
    assert SECRET not in stored
    assert LEAK not in stored
    assert TOKEN not in stored
    row = json.loads(stored)[APPROVAL_ID]
    assert set(row) == {"approval_request_id", "room_id", "bound_at", "surfaced", "decision"}
    assert row["room_id"] == "room-a"
    assert row["surfaced"] is True
    assert row["decision"] is None
    assert fake.gets == [APPROVAL_ID]
    assert fake.decisions == []
    assert SECRET not in caplog.text
    assert LEAK not in caplog.text
    messages = adapter.store.read_since("room-a", 0)
    gateway_message = [message for message in messages if message.text.startswith(janus_approval.LINE_PREFIX)][0]
    assert gateway_message.kind == KIND_AGENT
    assert gateway_message.speaker == "janus"


def test_second_mention_does_not_post_another_gateway_line(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    spoken = f"approval_request_id: {APPROVAL_ID}"
    assert _observe(adapter, "room-a") is True
    _say(adapter, "room-a", spoken)
    _say(adapter, "room-a", spoken)
    gateway = [line for line in _texts(adapter, "room-a") if line.startswith(janus_approval.LINE_PREFIX)]
    assert gateway == [janus_approval.gateway_approval_line(APPROVAL_ID)]
    assert fake.gets == [APPROVAL_ID]
    assert fake.decisions == []


def test_failed_fetch_does_not_pause(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _open(adapter)
    adapter._janus_client_override = None
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    assert _binding_text(adapter) == ""
    fake = FakeJanus()
    adapter._janus_client_override = fake
    assert _observe(adapter, "room-a") is True
    adapter._janus_client_override = None
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    adapter._janus_client_override = fake
    _say(adapter, "room-a", f"approval_request_id: {OTHER_ID}")
    fake.records[APPROVAL_ID] = _pending(approval_request_id="other-id99")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    fake.records.clear()
    fake.fail_get = JanusApprovalError("transport")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    fake.fail_get = None
    fake.records[APPROVAL_ID] = _pending(status="expired")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    fake.records[APPROVAL_ID] = _pending(expires_at="2000-01-01T00:00:00Z")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    fake.records[APPROVAL_ID] = _pending(expires_at="tomorrow")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    fake.records[APPROVAL_ID] = _pending(status="consumed")
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    lines = _texts(adapter, "room-a")
    assert not any(line.startswith(janus_approval.LINE_PREFIX) for line in lines)
    assert adapter.store.get("room-a").needs_user is False
    assert SECRET not in "\n".join(lines)
    assert TOKEN not in "\n".join(lines)
    assert fake.decisions == []
    assert OTHER_ID not in fake.gets
    assert "could not be verified" in "\n".join(lines)
    assert "is expired" in "\n".join(lines)
    stored = json.loads(_binding_text(adapter))
    assert set(stored) == {APPROVAL_ID}
    assert stored[APPROVAL_ID]["surfaced"] is False
    assert SECRET not in _binding_text(adapter)


def test_agent_mention_still_pauses_when_janus_cannot_verify(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _open(adapter)
    _say(adapter, "room-a", f"@user approval_request_id: {APPROVAL_ID}")
    assert adapter.store.get("room-a").needs_user is True
    assert not any(line.startswith(janus_approval.LINE_PREFIX) for line in _texts(adapter, "room-a"))


def test_tool_line_is_not_a_surface(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}", kind=KIND_TOOL)
    assert fake.gets == []
    assert fake.decisions == []
    assert adapter.store.get("room-a").needs_user is False


def test_wrong_room_cannot_see_or_decide(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter, "room-a")
    _open(adapter, "room-b")
    _claim(adapter, "room-a")
    _say(adapter, "room-b", f"approval_request_id: {APPROVAL_ID}")
    notice = "\n".join(_texts(adapter, "room-b"))
    assert "not available in this room" in notice
    assert "room-a" not in notice
    assert SECRET not in notice
    with _armed(adapter, monkeypatch):
        with pytest.raises(KeyError):
            adapter.get_janus_approval("room-b", APPROVAL_ID)
        with pytest.raises(KeyError):
            adapter.decide_janus_approval(
                "room-b", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
            )
    assert fake.decisions == []
    assert adapter.store.get("room-a").needs_user is True


def test_cycle_surfaces_before_later_speakers(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    room = _room()
    room.members = ["scout", "editor"]
    adapter.store.create(room)
    user_message = adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Clayton", text="status?"),
    )

    async def fake_turn(_room, member):
        if member != "scout":
            raise AssertionError(member)
        assert _observe(adapter, room.id) is True
        assert adapter._janus_bindings().get(APPROVAL_ID)["room_id"] == room.id
        assert not any(
            line.startswith(janus_approval.LINE_PREFIX) for line in _texts(adapter, room.id)
        )
        assert adapter.store.get(room.id).needs_user is False
        return True, f"Holding for Janus. approval_request_id: {APPROVAL_ID}"

    monkeypatch.setattr(adapter, "_agent_turn", fake_turn)

    async def run():
        async with adapter._room_lock(room.id):
            await adapter._run_cycle_workspace(room, user_message)

    asyncio.run(run())
    assert adapter.store.get(room.id).needs_user is True
    blob = "\n".join(_texts(adapter, room.id))
    assert janus_approval.gateway_approval_line(APPROVAL_ID) in blob
    assert SECRET not in blob
    assert LEAK not in blob
    assert fake.decisions == []
    assert fake.gets == [APPROVAL_ID]


# ── decisions ─────────────────────────────────────────────────────────────


def test_agent_origin_cannot_decide(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    fake.records[OTHER_ID] = _pending(OTHER_ID)
    _open(adapter)
    _claim(adapter, "room-a", APPROVAL_ID)
    _claim(adapter, "room-a", OTHER_ID)
    with _armed(adapter, monkeypatch):
        agent_refused = _refused(adapter, "room-a", APPROVAL_ID, "agent")
        tool_refused = _refused(adapter, "room-a", OTHER_ID, "tool")
    assert agent_refused and tool_refused and fake.decisions == [], (
        f"agent origin reached Janus decisions={fake.decisions!r} "
        f"agent_refused={agent_refused} tool_refused={tool_refused}"
    )
    assert adapter.store.get("room-a").needs_user is True


def test_approve_and_deny_wake_as_the_principal(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    fake.records[OTHER_ID] = _pending(OTHER_ID)
    _open(adapter)
    _claim(adapter, "room-a", APPROVAL_ID)
    _claim(adapter, "room-a", OTHER_ID)
    with _armed(adapter, monkeypatch) as wakes:
        approved = adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
        denied = adapter.decide_janus_approval(
            "room-a", OTHER_ID, "deny", origin=janus_approval.HTTP_ORIGIN
        )
    assert approved["duplicate"] is False
    assert approved["decision"] == "approve"
    assert "arguments" not in approved
    assert denied["decision"] == "deny"
    assert set(approved) == {"approval_request_id", "status", "decision", "duplicate"}
    assert wakes == [1, 1]
    user_lines = [
        message
        for message in adapter.store.read_since("room-a", 0)
        if message.kind == KIND_USER
    ]
    assert [message.speaker for message in user_lines] == ["Clayton", "Clayton"]
    assert user_lines[0].text == janus_approval.principal_decision_line("approve", APPROVAL_ID)
    assert user_lines[1].text == janus_approval.principal_decision_line("deny", OTHER_ID)
    assert SECRET not in user_lines[0].text
    assert adapter.store.get("room-a").needs_user is False
    assert fake.decisions == [(APPROVAL_ID, "approve"), (OTHER_ID, "deny")]


def test_duplicate_decision_does_not_wake_again_and_opposite_is_conflict(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    with _armed(adapter, monkeypatch) as wakes:
        first = adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
        second = adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
        with pytest.raises(JanusApprovalError) as caught:
            adapter.decide_janus_approval(
                "room-a", APPROVAL_ID, "deny", origin=janus_approval.HTTP_ORIGIN
            )
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert caught.value.code == "already_decided"
    assert wakes == [1]
    assert fake.decisions == [(APPROVAL_ID, "approve")]
    principal_lines = [
        message.text
        for message in adapter.store.read_since("room-a", 0)
        if message.kind == KIND_USER
    ]
    assert principal_lines == [janus_approval.principal_decision_line("approve", APPROVAL_ID)]


def test_expired_decision_leaves_the_room_paused(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    fake.records[APPROVAL_ID] = _pending(status="expired")
    with _armed(adapter, monkeypatch) as wakes:
        with pytest.raises(JanusApprovalError) as caught:
            adapter.decide_janus_approval(
                "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
            )
    assert caught.value.code == "expired"
    assert wakes == []
    assert fake.decisions == []
    assert adapter.store.get("room-a").needs_user is True
    assert not any(message.kind == KIND_USER for message in adapter.store.read_since("room-a", 0))


def test_restart_keeps_the_binding_and_can_finish_a_decision(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    restarted = RetinueRoomsAdapter(PlatformConfig())
    restarted.store = adapter.store
    restarted._janus_client_override = fake
    detail = restarted.get_janus_approval("room-a", APPROVAL_ID)
    assert detail["arguments"] == {"path": "/tmp/x", "password": SECRET}
    assert detail["room_id"] == "room-a"
    assert "token" not in detail
    assert LEAK not in json.dumps(detail)
    with _armed(restarted, monkeypatch) as wakes:
        result = restarted.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert result["decision"] == "approve"
    assert wakes == [1]
    assert adapter.store.get("room-a").needs_user is False


def test_already_accepted_upstream_wakes_once_without_a_second_post(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter, needs_user=True) if False else _open(adapter)
    adapter.store.create  # keep the room from _open
    _open_room = adapter.store.get("room-a")
    assert _open_room is not None
    adapter._janus_bindings().bind(APPROVAL_ID, "room-a")
    adapter._janus_bindings().mark_surfaced(APPROVAL_ID)
    fake.records[APPROVAL_ID] = _pending(status="approved")

    def mutate_needs(stored):
        stored.needs_user = True

    adapter.store.mutate("room-a", mutate_needs)
    with _armed(adapter, monkeypatch) as wakes:
        result = adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert result["duplicate"] is True
    assert fake.decisions == []
    assert wakes == [1]
    assert adapter.store.get("room-a").needs_user is False


def test_missing_or_corrupt_binding_fails_closed(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    with pytest.raises(KeyError):
        adapter.get_janus_approval("room-a", APPROVAL_ID)
    path = os.path.join(adapter.store.base_dir, "janus_approvals.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert fake.gets == []
    assert fake.decisions == []


def test_unconfigured_and_unready_loop_do_not_post_to_janus(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    fake = _arm_fake(adapter)
    _open(adapter)
    adapter._janus_bindings().bind(APPROVAL_ID, "room-a")
    adapter._janus_client_override = None
    with pytest.raises(JanusApprovalError) as missing:
        adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert missing.value.code == "not_configured"
    adapter._janus_client_override = fake
    adapter._loop = None
    with pytest.raises(RuntimeError):
        adapter.decide_janus_approval(
            "room-a", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert fake.gets == []
    assert fake.decisions == []


def test_message_post_does_not_decide(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    with _armed(adapter, monkeypatch) as wakes:
        principal_post = adapter.post_user_message(
            "room-a",
            f"approve approval_request_id: {APPROVAL_ID}",
            from_name="You",
        )
        scout_post = adapter.post_user_message(
            "room-a",
            f"Approved Janus request {APPROVAL_ID}.",
            from_name="scout",
        )
    assert principal_post["seq"] > 0
    assert principal_post.get("held") is not True
    # The principal's own post still clears the pause. It does not approve.
    # Scout's later post is an ordinary message after that clear, so the
    # scheduler may plan it; it still must not decide the Janus request.
    assert scout_post.get("held") is not True
    assert fake.decisions == []
    assert fake.gets == [APPROVAL_ID]
    assert wakes == [1, 1]
    assert adapter.store.get("room-a").needs_user is False


# ── HTTP ──────────────────────────────────────────────────────────────────


@pytest.fixture
def httpd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter._janus_client_override = None
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _request(server, method, path, body=None, token=None):
    import http.client

    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=3)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    cache = resp.getheader("Cache-Control")
    conn.close()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    return status, payload, cache, raw.decode("utf-8")


def test_http_auth_returns_arguments_only_to_the_caller_and_decide_strips_them(
    httpd, tmp_path, monkeypatch
):
    adapter = httpd.adapter
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    adapter.api_key = "room-key"
    status, payload, _cache, raw = _request(
        httpd, "GET", f"/rooms/room-a/approvals/{APPROVAL_ID}"
    )
    assert status == 401
    assert SECRET not in raw
    status, payload, cache, raw = _request(
        httpd, "GET", f"/rooms/room-a/approvals/{APPROVAL_ID}", token="room-key"
    )
    assert status == 200
    assert cache == "no-store"
    assert payload["arguments"] == {"path": "/tmp/x", "password": SECRET}
    assert payload["capability_id"] == "files.read"
    assert payload["env"] == {"MODE": "confirm"}
    assert payload["room_id"] == "room-a"
    assert "token" not in payload
    assert LEAK not in raw
    status, payload, cache, raw = _request(
        httpd,
        "POST",
        f"/rooms/room-a/approvals/{APPROVAL_ID}/decision",
        body={"decision": "approve", "from": "scout", "origin": "agent", "text": SECRET},
        token="room-key",
    )
    assert status == 503
    assert "gateway loop not ready" in payload["error"]
    assert SECRET not in raw
    assert fake.decisions == []
    with _armed(adapter, monkeypatch) as wakes:
        status, payload, cache, raw = _request(
            httpd,
            "POST",
            f"/rooms/room-a/approvals/{APPROVAL_ID}/decision",
            body={"decision": "approve", "from": "scout", "origin": "agent", "text": SECRET},
            token="room-key",
        )
    assert status == 200
    assert cache == "no-store"
    assert set(payload) == {"approval_request_id", "status", "decision", "duplicate"}
    assert payload["decision"] == "approve"
    assert payload["duplicate"] is False
    assert SECRET not in raw
    assert LEAK not in raw
    assert wakes == [1]
    speakers = [
        message.speaker
        for message in adapter.store.read_since("room-a", 0)
        if message.kind == KIND_USER
    ]
    assert speakers == ["Clayton"]
    assert adapter.store.get("room-a").needs_user is False
    status, payload, _cache, raw = _request(
        httpd,
        "POST",
        f"/rooms/room-a/approvals/{APPROVAL_ID}/decision",
        body={"decision": "maybe", "from": "scout"},
        token="room-key",
    )
    assert status == 400
    assert payload == {"error": "bad request"}
    assert "maybe" not in raw
    assert "scout" not in raw
    status, _payload, _cache, raw = _request(
        httpd,
        "GET",
        "/rooms/room-a/approvals/%2e%2e%2fetc",
        token="room-key",
    )
    assert status == 404
    assert SECRET not in raw
    with _armed(adapter, monkeypatch):
        status, payload, _cache, raw = _request(
            httpd,
            "POST",
            "/rooms/room-a/messages",
            body={"text": f"approve approval_request_id: {APPROVAL_ID}", "from": "scout"},
            token="room-key",
        )
    assert status == 202
    assert fake.decisions == [(APPROVAL_ID, "approve")]


def test_approval_routes_fail_closed_without_api_key_and_reject_a_wrong_key(
    httpd, tmp_path, monkeypatch
):
    """Live rooms often have no RETINUE_ROOMS_API_KEY. Detail and decision
    must not use the localhost-open branch that ordinary routes use."""
    adapter = httpd.adapter
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    _open(adapter)
    _claim(adapter, "room-a")
    room = adapter.store.get("room-a")
    assert room is not None and room.needs_user is True
    gets_after_surface = list(fake.gets)
    adapter.api_key = ""

    detail_path = f"/rooms/room-a/approvals/{APPROVAL_ID}"
    decision_path = f"{detail_path}/decision"
    decision_body = {
        "decision": "approve",
        "from": "scout",
        "origin": "agent",
        "text": SECRET,
    }

    def assert_closed(status, payload, cache, raw):
        assert status == 503
        assert payload == {"error": "room api key is required"}
        assert cache == "no-store"
        assert SECRET not in raw
        assert LEAK not in raw
        assert APPROVAL_ID not in raw
        assert "arguments" not in raw

    for token in (None, "guessed-key"):
        assert_closed(*_request(httpd, "GET", detail_path, token=token))
        assert_closed(
            *_request(httpd, "POST", decision_path, body=decision_body, token=token)
        )

    adapter.api_key = "   "
    assert_closed(*_request(httpd, "GET", detail_path))
    assert_closed(*_request(httpd, "POST", decision_path, body=decision_body))

    adapter.api_key = ""
    status, payload, _cache, raw = _request(httpd, "GET", "/rooms/room-a")
    assert status == 200
    assert payload["id"] == "room-a"
    assert SECRET not in raw

    with _armed(adapter, monkeypatch):
        status, payload, _cache, raw = _request(
            httpd,
            "POST",
            "/rooms/room-a/messages",
            body={"text": f"approve approval_request_id: {APPROVAL_ID}", "from": "scout"},
        )
    assert status == 202
    assert payload.get("held") is True
    assert fake.decisions == []
    assert fake.gets == gets_after_surface
    assert adapter.store.get("room-a").needs_user is True

    adapter.api_key = "room-key"
    for method, path, body in (
        ("GET", detail_path, None),
        ("POST", decision_path, decision_body),
    ):
        status, payload, _cache, raw = _request(
            httpd, method, path, body=body, token="wrong-key"
        )
        assert status == 401
        assert payload == {"error": "unauthorized"}
        assert SECRET not in raw
        assert LEAK not in raw
    assert fake.decisions == []
    assert fake.gets == gets_after_surface
    assert adapter.store.get("room-a").needs_user is True


# ── Grok speech stays unverified; Hermes binds from the tool result ──────


def _mark_grok(tmp_path, slug):
    path = tmp_path / "profiles" / slug / "retinue-agent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runtime": "grok-build"}), encoding="utf-8")


def _shared_record(approval_id, **over):
    record = _pending(approval_id)
    record["identity"] = "shared-janus"
    record["session_id"] = "shared-janus:mcp:not-a-room"
    record.update(over)
    return record


def _assert_grok_unverified(adapter, room_id, approval_id, caplog_text):
    """Grok speech is a notice only. It does not bind or open a card."""
    blob = "\n".join(_texts(adapter, room_id))
    assert janus_approval.unverified_notice(approval_id) in blob
    assert not any(
        line.startswith(janus_approval.LINE_PREFIX) and approval_id in line
        for line in _texts(adapter, room_id)
    )
    assert "@user" not in blob
    assert "Clayton" not in blob
    assert SECRET not in blob
    assert LEAK not in blob
    assert SECRET not in caplog_text
    assert LEAK not in caplog_text
    room = adapter.store.get(room_id)
    assert room is not None and room.needs_user is False


def test_other_room_speaking_first_does_not_bind(tmp_path, monkeypatch, caplog):
    """Two rooms share a Janus identity. Speech is not the bind."""
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    fake.records[APPROVAL_ID] = _shared_record(APPROVAL_ID)
    _open(adapter, "room-a")
    _open(adapter, "room-b")
    _say(adapter, "room-b", f"approval_request_id: {APPROVAL_ID}")
    early = "\n".join(_texts(adapter, "room-b"))
    assert "could not be verified" in early
    assert janus_approval.LINE_PREFIX not in early
    assert "@user" not in early
    assert "Clayton" not in early
    assert adapter.store.get("room-b").needs_user is False
    with pytest.raises(KeyError):
        adapter.get_janus_approval("room-b", APPROVAL_ID)
    assert fake.gets == []
    assert fake.inspects == []
    assert fake.decisions == []
    assert _binding_text(adapter) == ""
    assert _observe(adapter, "room-a") is True
    _say(adapter, "room-a", f"approval_request_id: {APPROVAL_ID}")
    assert janus_approval.gateway_approval_line(APPROVAL_ID) in "\n".join(_texts(adapter, "room-a"))
    assert adapter.store.get("room-a").needs_user is True
    assert SECRET not in "\n".join(_texts(adapter, "room-a"))
    gets_after_bind = list(fake.gets)
    _say(adapter, "room-b", f"approval_request_id: {APPROVAL_ID}")
    notice = "\n".join(_texts(adapter, "room-b"))
    assert "not available in this room" in notice
    assert "room-a" not in notice
    assert SECRET not in notice
    assert "@user" not in notice
    assert fake.gets == gets_after_bind
    with pytest.raises(KeyError):
        adapter.get_janus_approval("room-b", APPROVAL_ID)
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            "room-b", APPROVAL_ID, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert adapter.store.get("room-b").needs_user is False
    assert fake.decisions == []


def test_grok_speech_stays_unverified(tmp_path, monkeypatch, caplog):
    """Own id, another room's id, and a same-room bound id stay unverified.

    Grok speech does not fetch the operator API, does not bind, and does
    not post the card. A Hermes member of the same room can still surface
    an id that member's own tool result already bound.
    """
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    _mark_grok(tmp_path, "scout")
    fake = _arm_fake(adapter)
    own_id = "req-own1"
    other_id = "req-oth2"
    shared_id = "req-sam3"
    for approval_id in (own_id, other_id, shared_id):
        fake.records[approval_id] = _shared_record(approval_id)
    _open(adapter, "room-a")
    _open(adapter, "room-b")

    _say(adapter, "room-a", f"approval_request_id: {own_id}", speaker="scout")
    _assert_grok_unverified(adapter, "room-a", own_id, caplog.text)
    assert fake.gets == []
    assert fake.inspects == []
    assert fake.decisions == []
    assert _binding_text(adapter) == ""
    with pytest.raises(KeyError):
        adapter.get_janus_approval("room-a", own_id)
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            "room-a", own_id, "approve", origin=janus_approval.HTTP_ORIGIN
        )

    assert _observe(adapter, "room-b", other_id) is True
    assert adapter._janus_bindings().get(other_id)["room_id"] == "room-b"
    gets_after_other = list(fake.gets)
    _say(adapter, "room-a", f"approval_request_id: {other_id}", speaker="scout")
    _assert_grok_unverified(adapter, "room-a", other_id, caplog.text)
    assert fake.gets == gets_after_other
    assert fake.inspects == []
    assert adapter._janus_bindings().get(other_id)["room_id"] == "room-b"
    assert adapter.store.get("room-a").needs_user is False
    with pytest.raises(KeyError):
        adapter.get_janus_approval("room-a", other_id)
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            "room-a", other_id, "deny", origin=janus_approval.HTTP_ORIGIN
        )

    assert _observe(adapter, "room-a", shared_id) is True
    assert adapter._janus_bindings().get(shared_id)["surfaced"] is False
    assert adapter.store.get("room-a").needs_user is False
    gets_after_shared = list(fake.gets)
    _say(adapter, "room-a", f"approval_request_id: {shared_id}", speaker="scout")
    _say(adapter, "room-a", f"approval_request_id: {shared_id}", speaker="scout")
    blob = "\n".join(_texts(adapter, "room-a"))
    assert blob.count(janus_approval.unverified_notice(shared_id)) == 2
    assert janus_approval.LINE_PREFIX not in blob
    assert "@user" not in blob
    assert "Clayton" not in blob
    assert SECRET not in blob
    assert adapter.store.get("room-a").needs_user is False
    assert fake.gets == gets_after_shared
    assert fake.inspects == []
    assert fake.decisions == []
    assert adapter._janus_bindings().get(shared_id)["room_id"] == "room-a"
    assert adapter._janus_bindings().get(shared_id)["surfaced"] is False

    _say(adapter, "room-a", f"approval_request_id: {shared_id}", speaker="editor")
    assert janus_approval.gateway_approval_line(shared_id) in "\n".join(_texts(adapter, "room-a"))
    assert adapter.store.get("room-a").needs_user is True
    assert fake.gets == gets_after_shared + [shared_id]
    assert fake.inspects == []
    detail = adapter.get_janus_approval("room-a", shared_id)
    assert detail["arguments"]["password"] == SECRET
    assert "room_binding" not in detail
    assert SECRET not in caplog.text


def test_hermes_tool_result_binds_before_prose_and_cross_room_race(
    tmp_path, monkeypatch, caplog
):
    """The observing room binds before prose. A racing room cannot steal it."""
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(tmp_path, monkeypatch)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": ""})
    fake = _arm_fake(adapter)
    _open(adapter, "room-a")
    _open(adapter, "room-b")

    unbound = "req-free"
    fake.records[unbound] = _shared_record(unbound)
    _say(adapter, "room-a", f"approval_request_id: {unbound}")
    assert "could not be verified" in "\n".join(_texts(adapter, "room-a"))
    assert unbound not in (_binding_text(adapter) or "")
    assert fake.gets == []
    assert adapter.store.get("room-a").needs_user is False
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            "room-a", unbound, "approve", origin=janus_approval.HTTP_ORIGIN
        )

    assert _observe(adapter, "room-a") is True
    bound = adapter._janus_bindings().get(APPROVAL_ID)
    assert bound["room_id"] == "room-a"
    assert bound["surfaced"] is False
    assert janus_approval.LINE_PREFIX not in "\n".join(_texts(adapter, "room-a"))
    assert adapter.store.get("room-a").needs_user is False
    assert fake.gets == []

    race_id = "req-race"
    fake.records[race_id] = _shared_record(race_id)
    payload = _envelope(race_id)
    barrier = threading.Barrier(2)
    results = {}

    def race(room_id):
        barrier.wait(timeout=5)
        callback = janus_approval.room_tool_complete_callback(adapter, room_id)
        callback("call-race", JANUS_TOOL, {"password": SECRET}, payload)
        results[room_id] = adapter._janus_bindings().get(race_id)

    threads = [
        threading.Thread(target=race, args=(room_id,))
        for room_id in ("room-a", "room-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    row = adapter._janus_bindings().get(race_id)
    assert row is not None
    winner = row["room_id"]
    assert winner in {"room-a", "room-b"}
    loser = "room-b" if winner == "room-a" else "room-a"
    assert _observe(adapter, loser, race_id) is False
    assert adapter._janus_bindings().get(race_id)["room_id"] == winner
    _say(adapter, loser, f"approval_request_id: {race_id}")
    loser_text = "\n".join(_texts(adapter, loser))
    assert "not available in this room" in loser_text
    assert winner not in loser_text
    assert "@user" not in loser_text
    assert adapter.store.get(loser).needs_user is False
    with pytest.raises(KeyError):
        adapter.get_janus_approval(loser, race_id)
    with pytest.raises(KeyError):
        adapter.decide_janus_approval(
            loser, race_id, "approve", origin=janus_approval.HTTP_ORIGIN
        )
    assert janus_approval.LINE_PREFIX not in "\n".join(_texts(adapter, winner))
    _say(adapter, winner, f"approval_request_id: {race_id}")
    assert janus_approval.gateway_approval_line(race_id) in "\n".join(_texts(adapter, winner))
    assert adapter.store.get(winner).needs_user is True
    assert SECRET not in _binding_text(adapter)
    assert SECRET not in caplog.text
    assert fake.decisions == []
    assert set(results) == {"room-a", "room-b"}


def test_gateway_turn_composes_the_room_callback(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    from gateway.run import TurnRunner, _retinue_room_id_for_turn
    from gateway.turn_context import TurnContext

    adapter = _adapter(tmp_path, monkeypatch)
    _open(adapter, "room-a")
    _open(adapter, "room-b")

    class _Platform:
        def __init__(self, value):
            self.value = value

    class _Source:
        def __init__(self, chat_id, platform):
            self.chat_id = chat_id
            self.platform = platform

    source = _Source("room-a", _Platform("retinue_rooms"))
    assert _retinue_room_id_for_turn(source, {"retinue_room": "room-a"}) == "room-a"
    assert _retinue_room_id_for_turn(source, {"retinue_room": "room-b"}) is None
    assert _retinue_room_id_for_turn(source, {"retinue_room": " room-a"}) is None
    assert _retinue_room_id_for_turn(source, {"retinue_member": "scout"}) is None
    assert _retinue_room_id_for_turn(source, None) is None
    assert _retinue_room_id_for_turn(
        _Source("room-a", _Platform("slack")), {"retinue_room": "room-a"}
    ) is None

    class _Runner:
        def _adapter_for_source(self, _source):
            return adapter

    class _Agent:
        def __init__(self):
            self.tool_complete_callback = None

    agent = _Agent()
    TurnRunner(
        _Runner(), TurnContext(source=source, retinue_room_id="room-a")
    )._assign_tool_complete_callback(agent)
    assert agent.tool_complete_callback is not None
    agent.tool_complete_callback("call-1", JANUS_TOOL, {"password": SECRET}, _envelope())
    assert adapter._janus_bindings().get(APPROVAL_ID)["room_id"] == "room-a"

    other = janus_approval.room_tool_complete_callback(adapter, "room-b")
    assert other is not None
    assert other("call-2", JANUS_TOOL, {"password": SECRET}, _envelope()) is None
    assert adapter._janus_bindings().get(APPROVAL_ID)["room_id"] == "room-a"

    seen = {}

    def slack_cb(call_id, tool_name, args, result):
        seen["tool"] = tool_name
        raise RuntimeError("slack down")

    both = _Agent()
    TurnRunner(
        _Runner(),
        TurnContext(
            source=source,
            retinue_room_id="room-a",
            _native_slack_task_cards=True,
            native_tool_complete_callback=slack_cb,
        ),
    )._assign_tool_complete_callback(both)
    both.tool_complete_callback(
        "call-3", JANUS_TOOL, {"password": SECRET}, _envelope(OTHER_ID)
    )
    assert seen["tool"] == JANUS_TOOL
    assert adapter._janus_bindings().get(OTHER_ID)["room_id"] == "room-a"
    assert SECRET not in _binding_text(adapter)
    assert SECRET not in caplog.text

    empty = _Agent()
    TurnRunner(
        _Runner(), TurnContext(source=source, retinue_room_id="")
    )._assign_tool_complete_callback(empty)
    assert empty.tool_complete_callback is None
