"""Governed retainers: contract loading, briefing delivery, fail-closed turns.

Run:  .venv/bin/python -m pytest plugins/platforms/retinue_rooms/test_governed.py -q
"""

from __future__ import annotations

import asyncio
import json
import os

from gateway.config import PlatformConfig

from . import engine, governed, hire
from .adapter import RetinueRoomsAdapter
from .engine import KIND_USER, Room, RoomMessage
from .store import RoomStore


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _profile(tmp_path, slug, **meta):
    pdir = tmp_path / "profiles" / slug
    pdir.mkdir(parents=True)
    payload = {"display_name": slug.title(), "slug": slug, "job": "dev", "how": ""}
    payload.update(meta)
    (pdir / hire.AGENT_META_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    return pdir


def _contract(tmp_path, monkeypatch, text="- Rule one: stop on missing capability."):
    path = tmp_path / "retainer-contract.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(governed.ENV_VAR, str(path))
    return path


# ── flag storage ──────────────────────────────────────────────────────


def test_governed_flag_roundtrips_and_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile(tmp_path, "scout")

    hire.update_agent(str(tmp_path), "scout", governed=True)
    assert hire.agent_is_governed(str(tmp_path), "scout") is True
    listed = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
    assert listed["scout"]["governed"] is True

    hire.update_agent(str(tmp_path), "scout", governed=False)
    assert hire.agent_is_governed(str(tmp_path), "scout") is False
    meta = json.loads(
        (tmp_path / "profiles" / "scout" / hire.AGENT_META_FILENAME).read_text()
    )
    assert "governed" not in meta  # false is absence, meta stays clean


def test_agent_is_governed_defaults_false(tmp_path):
    assert hire.agent_is_governed(str(tmp_path), "nobody") is False


# ── contract loader ───────────────────────────────────────────────────


def test_contract_text_fail_reasons(tmp_path, monkeypatch):
    monkeypatch.delenv(governed.ENV_VAR, raising=False)
    text, err = governed.contract_text()
    assert text is None and governed.ENV_VAR in err

    monkeypatch.setenv(governed.ENV_VAR, str(tmp_path / "missing.md"))
    text, err = governed.contract_text()
    assert text is None and "unreadable" in err

    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv(governed.ENV_VAR, str(empty))
    text, err = governed.contract_text()
    assert text is None and "empty" in err

    big = tmp_path / "big.md"
    big.write_text("x" * (governed.MAX_CONTRACT_BYTES + 1), encoding="utf-8")
    monkeypatch.setenv(governed.ENV_VAR, str(big))
    text, err = governed.contract_text()
    assert text is None and "truncated contract is not a contract" in err


def test_contract_text_reads_and_tracks_mtime(tmp_path, monkeypatch):
    path = _contract(tmp_path, monkeypatch, "FIRST RULES")
    text, err = governed.contract_text()
    assert (text, err) == ("FIRST RULES", "")

    path.write_text("SECOND RULES", encoding="utf-8")
    os.utime(path, (os.stat(path).st_atime, os.stat(path).st_mtime + 5))
    text, err = governed.contract_text()
    assert (text, err) == ("SECOND RULES", "")


# ── briefing delivery ─────────────────────────────────────────────────


def test_briefing_appends_contract_last(tmp_path):
    room = Room(id="r", name="R", members=["scout"], lead="scout", workspace="ide")
    with_contract = engine.room_briefing(
        room, "scout", ["Mark"], governed_contract="- Never improvise around a refusal."
    )
    assert "OPERATING CONTRACT" in with_contract
    assert with_contract.rstrip().endswith("- Never improvise around a refusal.")

    without = engine.room_briefing(room, "scout", ["Mark"])
    assert "OPERATING CONTRACT" not in without


# ── the real _agent_turn: fail closed / deliver / scope ───────────────


def _room(adapter, tmp_path, workspace="ide"):
    room = Room(
        id="r-1",
        name="Test",
        members=["scout"],
        lead="scout",
        workspace=workspace,
        ide_path=str(tmp_path) if workspace == "ide" else None,
    )
    adapter.store.create(room)
    adapter.store.append(
        room.id,
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hello scout"),
    )
    return room


def test_governed_turn_fails_closed_without_contract(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _profile(tmp_path, "scout", governed=True)
    monkeypatch.delenv(governed.ENV_VAR, raising=False)
    room = _room(adapter, tmp_path)

    ok, text = asyncio.run(adapter._agent_turn(room, "scout"))
    assert ok is False
    assert "governed contract unavailable" in text
    assert governed.ENV_VAR in text


def _run_turn_capturing_prompt(adapter, room, member):
    """Drive the REAL _agent_turn; fake only the LLM dispatch."""
    seen = {}

    async def fake_handle_message(event):
        seen["channel_prompt"] = event.channel_prompt
        adapter._resolve_pending(room.id, ok=True, text="done", member=member)

    adapter.handle_message = fake_handle_message
    ok, text = asyncio.run(adapter._agent_turn(room, member))
    assert (ok, text) == (True, "done")
    return seen["channel_prompt"]


def test_governed_ide_turn_carries_the_contract(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _profile(tmp_path, "scout", governed=True)
    _contract(tmp_path, monkeypatch, "- The broker's refusal is final.")
    room = _room(adapter, tmp_path)

    prompt = _run_turn_capturing_prompt(adapter, room, "scout")
    assert "OPERATING CONTRACT" in prompt
    assert "The broker's refusal is final." in prompt


def test_ungoverned_and_sandbox_turns_carry_no_contract(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _profile(tmp_path, "scout")  # not governed
    _contract(tmp_path, monkeypatch)
    room = _room(adapter, tmp_path)
    prompt = _run_turn_capturing_prompt(adapter, room, "scout")
    assert "OPERATING CONTRACT" not in prompt

    # governed member, sandbox room: contract is out of scope, turn runs
    adapter2 = _adapter(tmp_path / "h2", monkeypatch)
    _profile(tmp_path / "h2", "scout", governed=True)
    monkeypatch.delenv(governed.ENV_VAR, raising=False)  # would fail closed in ide
    sandbox = _room(adapter2, tmp_path / "h2", workspace="sandbox")
    prompt = _run_turn_capturing_prompt(adapter2, sandbox, "scout")
    assert "OPERATING CONTRACT" not in prompt


# ── session cwd hook ──────────────────────────────────────────────────


class _Source:
    def __init__(self, chat_id):
        self.chat_id = chat_id


def test_session_cwd_for_ide_rooms_only(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    ide = _room(adapter, tmp_path, workspace="ide")
    assert adapter.session_cwd_for(_Source(ide.id)) == str(tmp_path)

    sandbox = Room(id="r-2", name="S", members=["scout"], workspace="sandbox")
    adapter.store.create(sandbox)
    assert adapter.session_cwd_for(_Source(sandbox.id)) == ""

    assert adapter.session_cwd_for(_Source("no-such-room")) == ""

    gone = Room(
        id="r-3", name="G", members=["scout"], workspace="ide",
        ide_path=str(tmp_path / "deleted"),
    )
    adapter.store.create(gone)
    assert adapter.session_cwd_for(_Source(gone.id)) == ""
