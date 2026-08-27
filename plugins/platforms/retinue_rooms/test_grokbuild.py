"""Grok Build runtime — ACP client, approval policy, session lifecycle (#218).

Everything here runs against a FAKE ACP agent (a small python script speaking
newline-delimited JSON-RPC), so the suite needs neither the grok binary nor
credentials. The live multi-step acceptance test is `test_grokbuild_live.py`
(env-gated).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

from . import grokbuild
from .grokbuild import (
    APPROVAL_ALWAYS,
    APPROVAL_READ_ONLY,
    APPROVAL_WORKSPACE,
    GrokBuildAuthRequired,
    GrokBuildError,
    GrokBuildProcessExit,
    GrokBuildManager,
    approval_mode,
    decide_permission,
    _path_inside,
)

# ── fake ACP agent ───────────────────────────────────────────────────────

_FAKE_AGENT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Fake `grok agent stdio`: enough ACP to exercise the client. Behavior
    # dials via FAKE_ACP_* env vars; observable side effects land in
    # FAKE_ACP_LOG (one JSON line per notable event).
    import json, os, sys, uuid

    LOG = os.environ.get("FAKE_ACP_LOG") or ""

    def log(kind, **kw):
        if LOG:
            with open(LOG, "a") as f:
                f.write(json.dumps({"kind": kind, **kw}) + "\\n")

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    def update(sid, upd):
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": upd}})

    cancelled = set()
    next_req = [1000]

    def ask_permission(sid, tool_call):
        rid = next_req[0]; next_req[0] += 1
        send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
              "params": {"sessionId": sid, "toolCall": tool_call, "options": [
                  {"optionId": "allow", "name": "Yes", "kind": "allow_once"},
                  {"optionId": "reject", "name": "No", "kind": "reject_once"},
              ]}})
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("id") == rid:
                outcome = (msg.get("result") or {}).get("outcome") or {}
                allowed = outcome.get("optionId") == "allow"
                log("permission", tool=tool_call.get("title"), allowed=allowed)
                return allowed
            handle(msg)  # interleaved traffic (e.g. session/cancel)
        return False

    def run_prompt(msg):
        sid = msg["params"]["sessionId"]
        prompt_text = "".join(
            p.get("text", "") for p in msg["params"].get("prompt", [])
        )
        log("prompt", session=sid, text=prompt_text)
        if os.environ.get("FAKE_ACP_GARBAGE"):
            sys.stdout.write("this is not json\\n{broken\\n")
            sys.stdout.flush()
        update(sid, {"sessionUpdate": "agent_thought_chunk",
                     "content": {"type": "text", "text": "secret reasoning"}})
        cwd = os.environ.get("FAKE_ACP_TOOL_PATH") or os.getcwd()
        tool_call = {
            "toolCallId": "tc-1", "title": "write hello.txt", "kind": "edit",
            "locations": [{"path": cwd}],
            "rawInput": {"file_path": cwd},
            "_meta": {"x.ai/tool": {"name": "write", "kind": "write",
                                     "read_only": False}},
        }
        update(sid, dict(tool_call, sessionUpdate="tool_call"))
        if os.environ.get("FAKE_ACP_DIE_MID_TURN"):
            sys.exit(3)
        allowed = ask_permission(sid, tool_call)
        if sid in cancelled:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "result": {"stopReason": "cancelled"}})
            return
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1",
                     "status": "completed" if allowed else "failed",
                     "title": "write hello.txt"})
        reply = "did the thing" if allowed else "could not write"
        for word in reply.split(" "):
            update(sid, {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": word + " "}})
        send({"jsonrpc": "2.0", "id": msg["id"],
              "result": {"stopReason": "end_turn",
                          "_meta": {"modelId": "fake-grok", "totalTokens": 7}}})

    def handle(msg):
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "authMethods": [],
                "_meta": {"agentVersion": "fake-0.1"}}})
        elif method == "session/new":
            if os.environ.get("FAKE_ACP_AUTH_FAIL"):
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": "Authentication required"}})
                return
            sid = str(uuid.uuid4())
            log("new", session=sid, cwd=msg["params"].get("cwd"),
                mcp=msg["params"].get("mcpServers"),
                broker_token=bool(os.environ.get("RETINUE_BROKER_TOKEN")))
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": sid}})
        elif method == "session/load":
            sid = msg["params"]["sessionId"]
            log("load", session=sid, cwd=msg["params"].get("cwd"),
                mcp=msg["params"].get("mcpServers"))
            if os.environ.get("FAKE_ACP_LOAD_FAIL"):
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32001, "message": "no such session"}})
                return
            update(sid, {"sessionUpdate": "user_message_chunk",
                         "content": {"type": "text", "text": "replayed"}})
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "session/prompt":
            run_prompt(msg)
        elif method == "session/cancel":
            cancelled.add(msg["params"]["sessionId"])
            log("cancel", session=msg["params"]["sessionId"])
        elif "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "error": {"code": -32601, "message": "nope"}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        handle(json.loads(line))
    """
)


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    """Point grokbuild at a fake ACP agent; returns the event-log path."""
    script = tmp_path / "fake-grok"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "acp-events.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    monkeypatch.setenv(grokbuild.BIN_ENV, str(script))
    monkeypatch.setenv(grokbuild.AUTH_PATH_ENV, str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text('{"fake": true}', encoding="utf-8")
    # The fake is a python script; make sure "#!" resolution finds python.
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")]))
    grokbuild._invalidate_health_cache()
    return log


def _events(log_path):
    if not os.path.isfile(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── approval policy (pure) ───────────────────────────────────────────────


def _tool(kind="edit", path=None, read_only=False, title="t"):
    call = {
        "toolCallId": "x",
        "title": title,
        "kind": kind,
        "_meta": {"x.ai/tool": {"kind": kind, "read_only": read_only}},
    }
    if path is not None:
        call["locations"] = [{"path": path}]
        call["rawInput"] = {"file_path": path}
    return call


class TestDecidePermission:
    def test_read_only_tools_allowed_in_every_mode(self):
        for mode in (APPROVAL_WORKSPACE, APPROVAL_READ_ONLY, APPROVAL_ALWAYS):
            ok, _ = decide_permission(
                _tool(kind="read", read_only=True), mode=mode, cwd="/srv/p"
            )
            assert ok, mode

    def test_workspace_scoped_edit_allowed(self, tmp_path):
        ok, why = decide_permission(
            _tool(path=str(tmp_path / "a.txt")), mode=APPROVAL_WORKSPACE, cwd=str(tmp_path)
        )
        assert ok, why

    def test_out_of_tree_edit_rejected(self, tmp_path):
        ok, why = decide_permission(
            _tool(path="/etc/passwd"), mode=APPROVAL_WORKSPACE, cwd=str(tmp_path)
        )
        assert not ok
        assert "outside" in why

    def test_relative_path_resolves_against_cwd(self, tmp_path):
        ok, _ = decide_permission(
            _tool(path="sub/file.txt"), mode=APPROVAL_WORKSPACE, cwd=str(tmp_path)
        )
        assert ok
        ok, _ = decide_permission(
            _tool(path="../escape.txt"), mode=APPROVAL_WORKSPACE, cwd=str(tmp_path)
        )
        assert not ok

    def test_symlinked_dir_cannot_smuggle_a_write_out(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "link").symlink_to(outside)
        ok, _ = decide_permission(
            _tool(path=str(ws / "link" / "f.txt")), mode=APPROVAL_WORKSPACE, cwd=str(ws)
        )
        assert not ok

    def test_execute_allowed_in_workspace_rejected_in_read_only(self):
        ok, _ = decide_permission(_tool(kind="execute"), mode=APPROVAL_WORKSPACE, cwd="/p")
        assert ok
        ok, why = decide_permission(_tool(kind="execute"), mode=APPROVAL_READ_ONLY, cwd="/p")
        assert not ok
        assert "read-only" in why

    def test_unknown_mutating_tool_without_paths_rejected(self):
        ok, _ = decide_permission(_tool(kind="other"), mode=APPROVAL_WORKSPACE, cwd="/p")
        assert not ok

    def test_always_mode_allows_everything(self):
        ok, _ = decide_permission(
            _tool(path="/etc/passwd"), mode=APPROVAL_ALWAYS, cwd="/p"
        )
        assert ok

    def test_mutation_with_no_visible_path_rejected(self):
        ok, why = decide_permission(_tool(kind="edit"), mode=APPROVAL_WORKSPACE, cwd="/p")
        assert not ok
        assert "no target path" in why


class TestApprovalMode:
    def test_default_is_workspace(self, monkeypatch):
        monkeypatch.delenv(grokbuild.APPROVAL_ENV, raising=False)
        assert approval_mode({}) == APPROVAL_WORKSPACE

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(grokbuild.APPROVAL_ENV, "read-only")
        assert approval_mode({}) == APPROVAL_READ_ONLY

    def test_member_meta_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(grokbuild.APPROVAL_ENV, "read-only")
        assert approval_mode({"grok_approval": "always"}) == APPROVAL_ALWAYS

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(grokbuild.APPROVAL_ENV, "yolo-everything")
        assert approval_mode({"grok_approval": "sure"}) == APPROVAL_WORKSPACE


def test_path_inside_root_itself():
    assert _path_inside("/srv/p", "/srv/p")
    assert not _path_inside("/srv/p-sibling/x", "/srv/p")


# ── health ───────────────────────────────────────────────────────────────


class TestHealth:
    def test_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(grokbuild.BIN_ENV, str(tmp_path / "missing"))
        grokbuild._invalidate_health_cache()
        assert grokbuild.health(str(tmp_path))["status"] == "not_installed"

    def test_auth_required_when_store_missing(self, tmp_path, fake_agent, monkeypatch):
        # fake_agent wrote an auth store; point at a missing one instead.
        monkeypatch.setenv(grokbuild.AUTH_PATH_ENV, str(tmp_path / "nope.json"))
        grokbuild._invalidate_health_cache()
        state = grokbuild.health(str(tmp_path))
        assert state["status"] == "auth_required"
        assert "grok login" in state["detail"]

    def test_available_with_fake_binary(self, tmp_path, fake_agent):
        # The fake script exits 0 on --version (it just EOFs on stdin).
        state = grokbuild.health(str(tmp_path), force=True)
        assert state["status"] == "available"

    def test_cached_until_forced(self, tmp_path, fake_agent, monkeypatch):
        first = grokbuild.health(str(tmp_path), force=True)
        monkeypatch.setenv(grokbuild.BIN_ENV, str(tmp_path / "gone"))
        assert grokbuild.health(str(tmp_path)) == first
        assert grokbuild.health(str(tmp_path), force=True)["status"] == "not_installed"


def test_grok_home_writes_isolating_config_once(tmp_path):
    home = grokbuild.grok_home(str(tmp_path))
    cfg = os.path.join(home, "config.toml")
    text = open(cfg, encoding="utf-8").read()
    assert "[compat.claude]" in text and "mcps = false" in text
    # Grok appends state to this file — a second call must not clobber it.
    with open(cfg, "a", encoding="utf-8") as f:
        f.write("# operator edit\n")
    grokbuild.grok_home(str(tmp_path))
    assert "# operator edit" in open(cfg, encoding="utf-8").read()


def test_sandbox_workspace_dir_created(tmp_path):
    path = grokbuild.sandbox_workspace_dir(str(tmp_path), "room-9")
    assert os.path.isdir(path)
    assert "room-9" in path


# ── manager / wire protocol (fake agent) ─────────────────────────────────


class TestTurns:
    def test_basic_turn_streams_and_completes(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "hello.txt"))
        manager = GrokBuildManager(str(tmp_path))
        activity = []
        prompts = []

        async def go():
            result = await manager.run_turn(
                "r1",
                "scout",
                str(ws),
                build_prompt=lambda fresh: prompts.append(fresh) or "hello agent",
                approval=APPROVAL_WORKSPACE,
                timeout=30,
                on_activity=lambda ev, p: activity.append((ev, p["title"])),
            )
            await manager.shutdown()
            return result

        result = _run(go())
        assert result.stop_reason == "end_turn"
        assert result.text.strip() == "did the thing"
        assert "secret reasoning" not in result.text
        assert ("tool_start", "write hello.txt") in activity
        assert prompts == [True]  # fresh session -> briefing prompt
        events = _events(fake_agent)
        assert [e["kind"] for e in events if e["kind"] == "permission"] == ["permission"]
        assert events[-1]["allowed"] is True

    def test_out_of_tree_tool_is_rejected_and_reported(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", "/etc/passwd")
        manager = GrokBuildManager(str(tmp_path))
        activity = []

        async def go():
            result = await manager.run_turn(
                "r1",
                "scout",
                str(ws),
                build_prompt=lambda fresh: "hello",
                approval=APPROVAL_WORKSPACE,
                timeout=30,
                on_activity=lambda ev, p: activity.append(ev),
            )
            await manager.shutdown()
            return result

        result = _run(go())
        assert result.text.strip() == "could not write"
        assert "rejected" in activity
        perms = [e for e in _events(fake_agent) if e["kind"] == "permission"]
        assert perms and perms[0]["allowed"] is False

    def test_malformed_frames_do_not_kill_the_turn(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_GARBAGE", "1")
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))
        manager = GrokBuildManager(str(tmp_path))

        async def go():
            result = await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()
            return result

        assert _run(go()).stop_reason == "end_turn"

    def test_process_death_mid_turn_raises(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_DIE_MID_TURN", "1")
        manager = GrokBuildManager(str(tmp_path))

        async def go():
            try:
                with pytest.raises(GrokBuildProcessExit):
                    await manager.run_turn(
                        "r1", "scout", str(ws),
                        build_prompt=lambda fresh: "x",
                        approval=APPROVAL_WORKSPACE, timeout=30,
                    )
            finally:
                await manager.shutdown()

        _run(go())

    def test_cancel_event_maps_to_cancelled_stop(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))
        manager = GrokBuildManager(str(tmp_path))

        async def go():
            cancel_ev = asyncio.Event()
            # The fake asks permission mid-turn; cancel while it waits by
            # never... simpler: set cancel before the permission response
            # lands. The fake checks the cancelled set after ask_permission.
            async def trip():
                await asyncio.sleep(0.3)
                cancel_ev.set()

            trip_task = asyncio.ensure_future(trip())
            result = await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
                cancel_event=cancel_ev,
            )
            trip_task.cancel()
            await manager.shutdown()
            return result

        result = _run(go())
        # Depending on timing the fake sees the cancel before or after it
        # finishes; both terminal states are legitimate, hanging is not.
        assert result.stop_reason in ("cancelled", "end_turn")

    def test_auth_error_surfaces_as_auth_required(self, tmp_path, fake_agent, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_AUTH_FAIL", "1")
        manager = GrokBuildManager(str(tmp_path))

        async def go():
            try:
                with pytest.raises(GrokBuildAuthRequired):
                    await manager.run_turn(
                        "r1", "scout", str(tmp_path),
                        build_prompt=lambda fresh: "x",
                        approval=APPROVAL_WORKSPACE, timeout=30,
                    )
            finally:
                await manager.shutdown()

        _run(go())

    def test_second_turn_reuses_the_session(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))
        manager = GrokBuildManager(str(tmp_path))
        freshness = []

        async def go():
            for _ in range(2):
                await manager.run_turn(
                    "r1", "scout", str(ws),
                    build_prompt=lambda fresh: freshness.append(fresh) or "x",
                    approval=APPROVAL_WORKSPACE, timeout=30,
                )
            await manager.shutdown()

        _run(go())
        assert freshness == [True, False]
        news = [e for e in _events(fake_agent) if e["kind"] == "new"]
        assert len(news) == 1  # one session served both turns

    def test_restart_resumes_via_session_load(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))

        async def first():
            manager = GrokBuildManager(str(tmp_path))
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(first())

        freshness = []

        async def second():
            manager = GrokBuildManager(str(tmp_path))  # "gateway restart"
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: freshness.append(fresh) or "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(second())
        events = _events(fake_agent)
        news = [e for e in events if e["kind"] == "new"]
        loads = [e for e in events if e["kind"] == "load"]
        assert len(news) == 1 and len(loads) == 1
        assert loads[0]["session"] == news[0]["session"]
        assert freshness == [False]  # resumed session is NOT fresh

    def test_resume_failure_falls_back_to_new_session(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))

        async def first():
            manager = GrokBuildManager(str(tmp_path))
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(first())
        monkeypatch.setenv("FAKE_ACP_LOAD_FAIL", "1")
        freshness = []

        async def second():
            manager = GrokBuildManager(str(tmp_path))
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: freshness.append(fresh) or "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(second())
        assert freshness == [True]  # fell back to a genuinely new session
        assert len([e for e in _events(fake_agent) if e["kind"] == "new"]) == 2

    def test_concurrent_sessions_in_different_rooms(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))
        manager = GrokBuildManager(str(tmp_path))

        async def go():
            results = await asyncio.gather(
                manager.run_turn(
                    "r1", "scout", str(ws),
                    build_prompt=lambda fresh: "x",
                    approval=APPROVAL_WORKSPACE, timeout=30,
                ),
                manager.run_turn(
                    "r2", "scout", str(ws),
                    build_prompt=lambda fresh: "x",
                    approval=APPROVAL_WORKSPACE, timeout=30,
                ),
            )
            await manager.shutdown()
            return results

        results = _run(go())
        assert all(r.stop_reason == "end_turn" for r in results)
        news = [e for e in _events(fake_agent) if e["kind"] == "new"]
        assert len(news) == 2
        assert news[0]["session"] != news[1]["session"]

    def test_reset_forgets_the_persisted_session(self, tmp_path, fake_agent, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))

        async def go():
            manager = GrokBuildManager(str(tmp_path))
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.reset("r1", "scout")
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(go())
        events = _events(fake_agent)
        assert len([e for e in events if e["kind"] == "new"]) == 2
        assert not [e for e in events if e["kind"] == "load"]


# ── workspace MCP bridge (#220) ──────────────────────────────────────────


def _write_mcp(home, servers):
    path = grokbuild.mcp_config_path(str(home))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"servers": servers}, f)


class TestMcpConfig:
    def test_absent_file_means_no_servers(self, tmp_path):
        assert grokbuild.mcp_servers(str(tmp_path)) == []

    def test_stdio_entry_normalized_to_acp_shape(self, tmp_path):
        _write_mcp(tmp_path, [
            {"name": "broker", "command": "/bin/client", "args": ["--x", 1],
             "env": {"K": "V"}},
        ])
        assert grokbuild.mcp_servers(str(tmp_path)) == [
            {"name": "broker", "command": "/bin/client", "args": ["--x", "1"],
             "env": [{"name": "K", "value": "V"}]},
        ]

    def test_http_entry_normalized(self, tmp_path):
        _write_mcp(tmp_path, [
            {"name": "docs", "type": "http", "url": "https://x/mcp",
             "headers": {"Authorization": "Bearer t"}},
        ])
        assert grokbuild.mcp_servers(str(tmp_path)) == [
            {"type": "http", "name": "docs", "url": "https://x/mcp",
             "headers": [{"name": "Authorization", "value": "Bearer t"}]},
        ]

    def test_invalid_entries_skipped_not_fatal(self, tmp_path):
        _write_mcp(tmp_path, [
            {"name": "", "command": "/x"},          # no name
            {"name": "a"},                           # stdio without command
            {"name": "b", "type": "http"},           # http without url
            {"name": "c", "type": "carrier-pigeon"},  # unknown type
            "not-a-dict",
            {"name": "ok", "command": "/bin/true"},
        ])
        assert [s["name"] for s in grokbuild.mcp_servers(str(tmp_path))] == ["ok"]

    def test_malformed_file_means_no_servers(self, tmp_path):
        path = grokbuild.mcp_config_path(str(tmp_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        assert grokbuild.mcp_servers(str(tmp_path)) == []


class TestMcpWire:
    def test_servers_passed_on_new_and_load_with_broker_token(
        self, tmp_path, fake_agent, monkeypatch
    ):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("FAKE_ACP_TOOL_PATH", str(ws / "f"))
        _write_mcp(tmp_path, [{"name": "broker", "command": "/bin/true"}])

        async def first():
            manager = GrokBuildManager(str(tmp_path))
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(first())

        async def second():
            manager = GrokBuildManager(str(tmp_path))  # restart -> session/load
            await manager.run_turn(
                "r1", "scout", str(ws),
                build_prompt=lambda fresh: "x",
                approval=APPROVAL_WORKSPACE, timeout=30,
            )
            await manager.shutdown()

        _run(second())
        events = _events(fake_agent)
        new = next(e for e in events if e["kind"] == "new")
        load = next(e for e in events if e["kind"] == "load")
        expected = [{"name": "broker", "command": "/bin/true", "args": [], "env": []}]
        assert new["mcp"] == expected
        assert load["mcp"] == expected
        # The member's broker identity rides the agent process env, so a
        # broker-client MCP server (child of grok) inherits it.
        assert new["broker_token"] is True


class TestMcpPermissions:
    def _use_tool(self, tool_name):
        return {
            "toolCallId": "x",
            "title": tool_name,
            "kind": "use_tool",
            "rawInput": {"tool_name": tool_name, "tool_input": {}},
            "_meta": {"x.ai/tool": {"kind": "use_tool", "read_only": False}},
        }

    def test_declared_server_allowed_in_workspace_mode(self):
        ok, why = decide_permission(
            self._use_tool("broker__list"), mode=APPROVAL_WORKSPACE, cwd="/p",
            mcp_server_names=frozenset({"broker"}),
        )
        assert ok, why

    def test_undeclared_server_rejected(self):
        ok, why = decide_permission(
            self._use_tool("rogue__rm"), mode=APPROVAL_WORKSPACE, cwd="/p",
            mcp_server_names=frozenset({"broker"}),
        )
        assert not ok
        assert "not declared" in why

    def test_use_tool_rejected_in_read_only_mode(self):
        ok, _ = decide_permission(
            self._use_tool("broker__list"), mode=APPROVAL_READ_ONLY, cwd="/p",
            mcp_server_names=frozenset({"broker"}),
        )
        assert not ok

    def test_search_tool_discovery_always_allowed(self):
        call = {
            "toolCallId": "x", "title": "search_tool", "kind": "search_tool",
            "_meta": {"x.ai/tool": {"kind": "search_tool", "read_only": False}},
        }
        for mode in (APPROVAL_WORKSPACE, APPROVAL_READ_ONLY):
            ok, _ = decide_permission(call, mode=mode, cwd="/p")
            assert ok, mode

    def test_malformed_use_tool_name_rejected(self):
        ok, _ = decide_permission(
            self._use_tool("no-separator"), mode=APPROVAL_WORKSPACE, cwd="/p",
            mcp_server_names=frozenset({"broker"}),
        )
        assert not ok

    def test_live_request_shape_kind_other_meta_use_tool(self):
        # EXACT payload observed from grok v0.2.93's session/request_permission
        # for an MCP call: top-level kind degrades to "other"; the identity is
        # in _meta["x.ai/tool"]. Pinned because the first policy cut keyed off
        # the top-level kind and rejected every declared MCP tool live.
        call = {
            "toolCallId": "c-1",
            "kind": "other",
            "title": "retinue-test__retinue_ping",
            "rawInput": {
                "variant": "UseTool",
                "tool_name": "retinue-test__retinue_ping",
                "tool_input": {"value": "bridge-check"},
            },
            "_meta": {"x.ai/tool": {"version": 1, "name": "use_tool",
                                     "kind": "use_tool", "namespace": "grok_build",
                                     "label": "Use Tool", "read_only": False}},
        }
        ok, why = decide_permission(
            call, mode=APPROVAL_WORKSPACE, cwd="/p",
            mcp_server_names=frozenset({"retinue-test"}),
        )
        assert ok, why
        ok, why = decide_permission(
            call, mode=APPROVAL_WORKSPACE, cwd="/p",
            mcp_server_names=frozenset(),
        )
        assert not ok
        assert "not declared" in why


class TestWorktreeIsolationRoots:
    """#223: denied roots redirect; extra roots extend the writable set."""

    def _wt(self, tmp_path):
        real = tmp_path / "ws" / "infra"
        real.mkdir(parents=True)
        wt = tmp_path / "worktrees" / "room" / "infra"
        wt.mkdir(parents=True)
        return str(tmp_path / "ws"), str(real), str(wt)

    def test_write_into_shadowed_tree_redirects(self, tmp_path):
        cwd, real, wt = self._wt(tmp_path)
        ok, why = decide_permission(
            _tool(path=os.path.join(real, "x.py")),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
            extra_roots=(wt,), denied_roots={real: wt},
        )
        assert not ok
        assert "isolates" in why and wt in why

    def test_read_of_shadowed_tree_also_redirects(self, tmp_path):
        cwd, real, wt = self._wt(tmp_path)
        ok, why = decide_permission(
            _tool(kind="read", path=os.path.join(real, "x.py"), read_only=True),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
            denied_roots={real: wt},
        )
        assert not ok
        assert wt in why

    def test_relative_path_into_shadowed_tree_redirects(self, tmp_path):
        cwd, real, wt = self._wt(tmp_path)
        ok, why = decide_permission(
            _tool(path="infra/x.py"),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
            denied_roots={real: wt},
        )
        assert not ok
        assert wt in why

    def test_write_into_worktree_checkout_allowed(self, tmp_path):
        cwd, real, wt = self._wt(tmp_path)
        ok, why = decide_permission(
            _tool(path=os.path.join(wt, "x.py")),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
            extra_roots=(wt,), denied_roots={real: wt},
        )
        assert ok, why

    def test_worktree_root_not_writable_without_extra_roots(self, tmp_path):
        # The worktree lives OUTSIDE cwd — without the explicit grant it
        # stays out of bounds, so the grant is doing real work.
        cwd, real, wt = self._wt(tmp_path)
        ok, _ = decide_permission(
            _tool(path=os.path.join(wt, "x.py")),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
        )
        assert not ok

    def test_rest_of_tree_still_writable(self, tmp_path):
        cwd, real, wt = self._wt(tmp_path)
        ok, why = decide_permission(
            _tool(path=os.path.join(cwd, "other", "y.py")),
            mode=APPROVAL_WORKSPACE, cwd=cwd,
            extra_roots=(wt,), denied_roots={real: wt},
        )
        assert ok, why

    def test_always_mode_bypasses_isolation(self, tmp_path):
        # "always" is the explicit full-trust dial; it overrides the
        # redirect exactly like every other guard. Documented.
        cwd, real, wt = self._wt(tmp_path)
        ok, _ = decide_permission(
            _tool(path=os.path.join(real, "x.py")),
            mode=APPROVAL_ALWAYS, cwd=cwd,
            denied_roots={real: wt},
        )
        assert ok
