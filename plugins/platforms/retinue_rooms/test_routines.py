"""Routines + workspace-status unit tests (no gateway)."""

from __future__ import annotations

import pytest

from . import routines, workspace
from .engine import KIND_AGENT, KIND_USER, RoomMessage


def test_user_prompts_from_messages_skips_agents_and_bounds():
    msgs = [
        RoomMessage(1, 1, KIND_USER, "Mark", "first"),
        RoomMessage(2, 2, KIND_AGENT, "scout", "ok"),
        RoomMessage(3, 3, KIND_USER, "Mark", "second"),
        RoomMessage(4, 4, KIND_USER, "Mark", "third"),
    ]
    assert routines.user_prompts_from_messages(msgs) == ["first", "second", "third"]
    assert routines.user_prompts_from_messages(msgs, since=1, until=3) == ["second"]


def test_save_list_get_delete_routine(tmp_path):
    meta = routines.save_routine(str(tmp_path), "Daily standup", ["what's open?", "summarize"])
    assert meta["slug"] == "daily-standup"
    assert [r["slug"] for r in routines.list_routines(str(tmp_path))] == ["daily-standup"]
    got = routines.get_routine(str(tmp_path), "daily-standup")
    assert got["messages"] == ["what's open?", "summarize"]
    with pytest.raises(FileExistsError):
        routines.save_routine(str(tmp_path), "Daily standup", ["x"])
    with pytest.raises(ValueError):
        routines.save_routine(str(tmp_path), "Empty", [])
    assert routines.delete_routine(str(tmp_path), "daily-standup") is True
    assert routines.get_routine(str(tmp_path), "daily-standup") is None
    assert routines.delete_routine(str(tmp_path), "daily-standup") is False


def test_workspace_status_disabled_without_key(monkeypatch):
    monkeypatch.delenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", raising=False)
    status = workspace.workspace_status()
    assert status["enabled"] is False
    assert status["running"] is False
    assert "TERMINAL_DOCKER_SHARED_CONTAINER_KEY" in (status["detail"] or "")


def test_workspace_status_enabled_but_no_runtime(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "demo")
    monkeypatch.setenv("HERMES_DOCKER_BINARY", "/no/such/runtime")
    monkeypatch.setattr(workspace.shutil, "which", lambda *_a, **_k: None)
    status = workspace.workspace_status()
    assert status["enabled"] is True
    assert status["key"] == "demo"
    # binary is forced, inspect will fail loudly — still not running
    assert status["running"] is False
