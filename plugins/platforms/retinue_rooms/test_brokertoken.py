"""Broker identity: token mint/verify, per-turn env binding, exec injection.

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_brokertoken.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

from gateway.config import PlatformConfig
from tools import turn_env

from . import brokertoken, hire
from .adapter import RetinueRoomsAdapter
from .engine import KIND_USER, Room, RoomMessage
from .store import RoomStore


# ── mint / verify ─────────────────────────────────────────────────────


def test_mint_verify_roundtrip_and_key_autocreate(tmp_path, monkeypatch):
    monkeypatch.delenv(brokertoken.ENV_KEY_FILE, raising=False)
    home = str(tmp_path)
    token = brokertoken.mint(home, "mangus")
    assert brokertoken.verify(home, token) == "mangus"

    key_file = tmp_path / "broker.key"
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_verify_rejects_forgery_expiry_and_shape(tmp_path):
    home = str(tmp_path)
    token = brokertoken.mint(home, "mangus")

    # borrow another slug on a valid mac → dead
    parts = token.split(":")
    forged = ":".join(["v1", "patty", parts[2], parts[3]])
    assert brokertoken.verify(home, forged) is None

    # expired
    old = brokertoken.mint(home, "mangus", now=0)
    assert brokertoken.verify(home, old) is None

    # shape garbage
    for bad in ("", "v1:mangus", "v2:" + token[3:], token + ":x"):
        assert brokertoken.verify(home, bad) is None

    # a different key does not honor the token
    other = str(tmp_path / "other")
    os.makedirs(other)
    assert brokertoken.verify(other, token) is None


# ── turn_env carrier + exec injection ─────────────────────────────────


def test_export_prefix_quotes_and_filters():
    tok = turn_env.set_turn_env({"RETINUE_BROKER_TOKEN": "v1:a:1:b", "bad-name": "x"})
    try:
        prefix = turn_env.export_prefix()
        assert prefix == "export RETINUE_BROKER_TOKEN=v1:a:1:b; " or "RETINUE_BROKER_TOKEN=" in prefix
        assert "bad-name" not in prefix
    finally:
        turn_env.reset(tok)
    assert turn_env.export_prefix() == ""


def test_local_environment_execute_sees_turn_env(tmp_path):
    """The injection is at BaseEnvironment.execute, so every backend gets it —
    prove it with the local backend, no docker needed."""
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    tok = turn_env.set_turn_env({"RETINUE_BROKER_TOKEN": "v1:scout:99:aa"})
    try:
        result = env.execute("printf '%s' \"$RETINUE_BROKER_TOKEN\"")
    finally:
        turn_env.reset(tok)
    assert "v1:scout:99:aa" in result.get("output", "")

    # The injection is subshell-scoped: the backends keep a persistent
    # shell, and in a shared room container an export that survived this
    # command would still be set on the NEXT MEMBER's turn. This assertion
    # was red against a bare-export implementation.
    result = env.execute("printf '%s' \"${RETINUE_BROKER_TOKEN:-unset}\"")
    assert "unset" in result.get("output", "")


# ── the real turn binds the token ─────────────────────────────────────


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def test_agent_turn_binds_a_valid_member_token(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    pdir = tmp_path / "profiles" / "scout"
    pdir.mkdir(parents=True)
    (pdir / hire.AGENT_META_FILENAME).write_text(
        json.dumps({"display_name": "Scout", "slug": "scout", "job": "dev", "how": ""}),
        encoding="utf-8",
    )
    room = Room(id="r-1", name="T", members=["scout"], lead="scout", workspace="sandbox")
    adapter.store.create(room)
    adapter.store.append(
        room.id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hi")
    )

    seen = {}

    async def fake_handle_message(event):
        mapping = turn_env.current() or {}
        seen["token"] = mapping.get(brokertoken.TOKEN_ENV, "")
        adapter._resolve_pending(room.id, ok=True, text="done", member="scout")

    adapter.handle_message = fake_handle_message
    ok, text = asyncio.run(adapter._agent_turn(room, "scout"))
    assert (ok, text) == (True, "done")

    assert brokertoken.verify(str(tmp_path), seen["token"]) == "scout"
    # and the binding does not leak past the turn
    assert turn_env.current() is None
