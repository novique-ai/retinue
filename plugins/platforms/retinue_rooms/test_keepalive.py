"""Idle xAI OAuth keepalive (issue #34)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.auth import AuthError
from hermes_constants import get_hermes_home

from . import auth, hire, keepalive
from .test_auth import _seed_presets, _write, _xai_store


def _hire_cloud(home: Path) -> None:
    _seed_presets(home)
    hire.scaffold_profile(str(home), "Sally", "writer", "draft", model_preset="grok-4.5")


def _hire_local(home: Path) -> None:
    _seed_presets(home)
    hire.scaffold_profile(str(home), "Editor", "edit", "terse", model_preset="local")


def test_should_keepalive_requires_cloud_xai_and_ok_grant(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    assert keepalive.should_keepalive(str(tmp_path)) is False

    _hire_local(tmp_path)
    assert keepalive.should_keepalive(str(tmp_path)) is False

    _hire_cloud(tmp_path)
    assert keepalive.should_keepalive(str(tmp_path)) is True

    _write(tmp_path / "auth.json", _xai_store(access="", refresh="", relogin=True))
    assert keepalive.should_keepalive(str(tmp_path)) is False


def test_interval_from_env(monkeypatch):
    monkeypatch.delenv("RETINUE_XAI_KEEPALIVE_SECONDS", raising=False)
    assert keepalive.interval_from_env() == 30.0
    monkeypatch.setenv("RETINUE_XAI_KEEPALIVE_SECONDS", "0")
    assert keepalive.interval_from_env() is None
    monkeypatch.setenv("RETINUE_XAI_KEEPALIVE_SECONDS", "-1")
    assert keepalive.interval_from_env() is None
    monkeypatch.setenv("RETINUE_XAI_KEEPALIVE_SECONDS", "12.5")
    assert keepalive.interval_from_env() == 12.5
    monkeypatch.setenv("RETINUE_XAI_KEEPALIVE_SECONDS", "nope")
    assert keepalive.interval_from_env() == 30.0


def test_refresh_skips_when_ineligible(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    called = []
    result = keepalive.refresh_workspace_xai(
        str(tmp_path), resolve=lambda **kw: called.append(kw) or {}
    )
    assert result["skipped"] is True
    assert called == []


def test_refresh_calls_resolve_under_workspace_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/should-not-win")
    _write(tmp_path / "auth.json", _xai_store())
    _hire_cloud(tmp_path)
    seen: list[str] = []

    def resolve(**_kw):
        seen.append(str(get_hermes_home()))
        return {"ok": True}

    result = keepalive.refresh_workspace_xai(str(tmp_path), resolve=resolve)
    assert result["skipped"] is False
    assert result["ok"] is True
    assert seen == [str(tmp_path)]
    assert os.environ.get("HERMES_HOME") == "/should-not-win"


def test_refresh_does_not_write_profile_shadow(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    _hire_cloud(tmp_path)
    profile_auth = tmp_path / "profiles" / "sally" / "auth.json"
    before = profile_auth.read_text(encoding="utf-8") if profile_auth.exists() else None

    keepalive.refresh_workspace_xai(str(tmp_path), resolve=lambda **_kw: {})

    if before is None:
        if profile_auth.exists():
            store = json.loads(profile_auth.read_text(encoding="utf-8"))
            assert "xai-oauth" not in (store.get("providers") or {})
    else:
        assert profile_auth.read_text(encoding="utf-8") == before


def test_terminal_refresh_sets_relogin_and_does_not_raise(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    _hire_cloud(tmp_path)

    def resolve(**_kw):
        _write(tmp_path / "auth.json", _xai_store(access="", refresh="", relogin=True))
        raise AuthError(
            "invalid_grant",
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=True,
        )

    result = keepalive.refresh_workspace_xai(str(tmp_path), resolve=resolve)
    assert result["skipped"] is False
    assert result["ok"] is False
    assert result["relogin_required"] is True
    assert auth.workspace_provider_status(str(tmp_path))[0]["status"] == auth.STATUS_RELOGIN
    # Next tick must not keep calling resolve against a dead grant.
    called = []
    skipped = keepalive.refresh_workspace_xai(
        str(tmp_path), resolve=lambda **kw: called.append(1)
    )
    assert skipped["skipped"] is True
    assert called == []


def test_loop_ticks_then_stops(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    _hire_cloud(tmp_path)
    ticks = []
    started = threading.Event()

    def resolve(**_kw):
        ticks.append(time.time())
        started.set()
        return {}

    loop = keepalive.XaiKeepalive(
        lambda: str(tmp_path),
        interval=0.05,
        refresh_fn=lambda home: keepalive.refresh_workspace_xai(home, resolve=resolve),
    )
    loop.start()
    try:
        assert started.wait(1.0)
        time.sleep(0.12)
    finally:
        loop.stop()
    assert len(ticks) >= 2
    assert loop.alive is False


@pytest.mark.asyncio
async def test_adapter_starts_and_stops_keepalive(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RETINUE_ROOMS_ENABLED", "1")
    monkeypatch.setenv("RETINUE_ROOMS_PORT", "0")
    monkeypatch.setenv("RETINUE_XAI_KEEPALIVE_SECONDS", "0.05")
    # connect() requires a docker-backed gateway; rooms are containerised by
    # definition and the adapter checks rather than imposing it (see
    # ide.docker_backend_error). This test is about keepalive, not backends.
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    _write(tmp_path / "auth.json", _xai_store())
    _hire_cloud(tmp_path)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    assert getattr(adapter, "_xai_keepalive", None) is None
    assert await adapter.connect() is True
    try:
        ka = adapter._xai_keepalive
        assert ka is not None
        assert ka.alive is True
    finally:
        await adapter.disconnect()
    assert adapter._xai_keepalive is None
    assert ka.alive is False
