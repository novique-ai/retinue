"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading

import pytest
import yaml

from hermes_cli.config import DEFAULT_CONFIG

_mcp_server_mod = pytest.importorskip("mcp.server")

if not hasattr(_mcp_server_mod, "MCPServer"):
    # `mcp.server.MCPServer` replaced `mcp.server.fastmcp.FastMCP` in mcp 2.0.
    # Skip rather than fail on a FastMCP-era SDK: the probe below is written
    # against the 2.x API, and the pinned version provides it.
    pytest.skip(
        "profile-local MCP discovery probe requires mcp >= 2.0 (MCPServer)",
        allow_module_level=True,
    )


# How long the worker may take to answer /tools at all.
#
# That means: boot a fresh interpreter, import the agent stack, spawn the
# profile-local MCP server as a further subprocess, complete the stdio
# handshake, and enumerate its tools. At 10s this was a startup race, not an
# assertion — the #176 flake, red on loaded runners running twelve slices of
# per-file pytest subprocesses in parallel and green on an idle laptop.
#
# #193 was the next shape of the same race: the worker *did* answer /tools
# inside this bound, but the interactive mcp_discovery_timeout had already
# expired, so the catalog listed 36 tools without the still-connecting
# profileprobe server. show_tools now joins in-flight discovery (30s) in
# the slash worker (no late-refresh; the TUI has one and does not join),
# so the first /tools response is deterministic — this outer bound is
# only "the worker never answered at all."
_RESPONSE_TIMEOUT_S = 120
_PROFILE_MCP_TOOL = "mcp__profileprobe__hermes_61922_profile_probe"

# Interactive discovery wait (startup + get_tool_definitions). Pulled from
# DEFAULT_CONFIG so a default change cannot silently put this sleep on the
# wrong side of the bound.
_INTERACTIVE_MCP_DISCOVERY_S = float(DEFAULT_CONFIG["mcp_discovery_timeout"])
# show_tools join in cli.py — same bound the late-refresh waiter uses.
_SHOW_TOOLS_JOIN_S = 30.0
# Handshake sleep sits BETWEEN those two bounds:
#   mcp_discovery_timeout (default 1.5s)  <  sleep  <  show_tools join (30s)
# The race is between (probe spawn + sleep) and (worker boot + first
# /tools). 20s clears any plausible loaded-CI boot while still sitting
# far under the 30s join and nowhere near the 120s outer bound. If this
# were <= the interactive bound, or if HermesCLI construction after the
# 1.5s startup wait could outrun the sleep, the first /tools would
# succeed even without the join and the test would no-op the product
# fix. If it were >= the join, the join would time out and /tools would
# miss the tool even WITH the fix.
_PROBE_HANDSHAKE_SLEEP_S = 20.0


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    if not (
        _INTERACTIVE_MCP_DISCOVERY_S
        < _PROBE_HANDSHAKE_SLEEP_S
        < _SHOW_TOOLS_JOIN_S
    ):
        pytest.fail(
            f"probe handshake sleep {_PROBE_HANDSHAKE_SLEEP_S}s must sit "
            f"between mcp_discovery_timeout ({_INTERACTIVE_MCP_DISCOVERY_S}s) "
            f"and the show_tools join ({_SHOW_TOOLS_JOIN_S}s); the product "
            f"fix is unguarded if this sleep is too short, and the join "
            f"times out if it is too long"
        )
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "mcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server import MCPServer

            mcp = MCPServer("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                import time
                # Deliberately slower than mcp_discovery_timeout, faster
                # than the show_tools join. See _PROBE_HANDSHAKE_SLEEP_S.
                time.sleep({_PROBE_HANDSHAKE_SLEEP_S!r})
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    # Real slash-worker process: _prepare_slash_worker_runtime sets
    # HERMES_SLASH_WORKER=1, which is the only gate on the show_tools join.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    errors: list[str] = []
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout = proc.stdout
        stderr = proc.stderr
        threading.Thread(
            target=lambda: [output.put(line) for line in iter(stdout.readline, "")],
            daemon=True,
        ).start()
        # Drain stderr concurrently. Without this the only evidence a timeout
        # left behind was "no response within 10 seconds" — no traceback, no
        # exit status — which is why this test recurred in #176 for weeks
        # without anyone being able to say why. A full stderr pipe would also
        # deadlock the worker before it could answer.
        threading.Thread(
            target=lambda: errors.extend(iter(stderr.readline, "")),
            daemon=True,
        ).start()
        # Single shot. The probe handshake is slower than mcp_discovery_timeout
        # and faster than the show_tools join, so the first /tools is the
        # load-bearing assertion: without the join it misses the still-
        # connecting server; with the join it waits and lists the tool.
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        try:
            line = output.get(timeout=_RESPONSE_TIMEOUT_S)
        except queue.Empty:
            line = ""
        if not line:
            pytest.fail(
                f"slash worker never answered /tools within "
                f"{_RESPONSE_TIMEOUT_S:.0f}s (exit status: {proc.poll()})\n"
                f"last /tools output:\n(empty)\n"
                f"--- worker stderr ---\n{''.join(errors) or '(empty)'}"
            )
        response = json.loads(line)
        assert response["ok"] is True
        last_output = response["output"]
        if _PROFILE_MCP_TOOL not in last_output:
            pytest.fail(
                f"first /tools response did not list {_PROFILE_MCP_TOOL} "
                f"(exit status: {proc.poll()})\n"
                f"last /tools output:\n{last_output or '(empty)'}\n"
                f"--- worker stderr ---\n{''.join(errors) or '(empty)'}"
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
