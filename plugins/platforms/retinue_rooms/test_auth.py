"""Provider auth status + in-product reauth (issue #18)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from . import auth, hire
from .store import RoomStore


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_presets(home: Path) -> None:
    d = home / "retinue_models"
    d.mkdir(exist_ok=True)
    (d / "grok-4.5.yaml").write_text(
        "model:\n  default: grok-4.5\n  provider: xai-oauth\n  base_url: https://api.x.ai/v1\n",
        encoding="utf-8",
    )
    (d / "local.yaml").write_text(
        "model:\n  default: local/auto\n  provider: custom\n  base_url: http://127.0.0.1:8091/v1\n",
        encoding="utf-8",
    )


def _xai_store(*, access="tok", refresh="ref", relogin=False, error=None) -> dict:
    state: dict = {
        "tokens": {"access_token": access, "refresh_token": refresh},
        "auth_mode": "oauth_device_code",
    }
    if relogin or error:
        state["last_auth_error"] = {
            "relogin_required": bool(relogin),
            "message": error or "Refresh token has been revoked",
        }
    return {"active_provider": "xai-oauth", "providers": {"xai-oauth": state}}


@pytest.fixture(autouse=True)
def _clean_sessions():
    auth._reset_sessions_for_tests()
    yield
    auth._reset_sessions_for_tests()


def test_classify_ok_missing_relogin_and_stripped(tmp_path):
    root = tmp_path / "auth.json"
    _write(root, _xai_store())
    assert auth.xai_status_for_store(root)["status"] == auth.STATUS_OK

    missing = tmp_path / "none.json"
    _write(missing, {"providers": {}})
    assert auth.xai_status_for_store(missing)["status"] == auth.STATUS_MISSING
    assert auth.xai_status_for_store(tmp_path / "absent.json")["status"] == auth.STATUS_MISSING

    dead = tmp_path / "dead.json"
    _write(dead, _xai_store(access="", refresh="", relogin=True))
    got = auth.xai_status_for_store(dead)
    assert got["status"] == auth.STATUS_RELOGIN
    assert "revoked" in (got["error"] or "")

    stripped = tmp_path / "stripped.json"
    _write(stripped, _xai_store(access="", refresh=""))
    assert auth.xai_status_for_store(stripped)["status"] == auth.STATUS_RELOGIN


def test_workspace_status_picks_worst_profile_shadow(tmp_path):
    _write(tmp_path / "auth.json", _xai_store())
    hire.scaffold_profile(str(tmp_path), "Sally", "writer", "draft")
    _write(
        tmp_path / "profiles" / "sally" / "auth.json",
        _xai_store(access="", refresh="", relogin=True),
    )
    providers = auth.workspace_provider_status(str(tmp_path))
    assert providers[0]["status"] == auth.STATUS_RELOGIN


def test_annotate_local_ok_cloud_inherits_workspace(tmp_path):
    _write(tmp_path / "auth.json", _xai_store(access="", refresh="", relogin=True))
    _seed_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Sally", "writer", "draft", model_preset="grok-4.5")
    hire.scaffold_profile(str(tmp_path), "Editor", "edit", "terse", model_preset="local")
    agents = hire.list_agents(str(tmp_path))
    auth.annotate_agents(str(tmp_path), agents)
    by = {a["slug"]: a for a in agents}
    assert by["editor"]["auth_status"] == auth.STATUS_NOT_REQUIRED
    assert by["editor"]["auth_provider"] is None
    assert by["sally"]["auth_provider"] == "xai-oauth"
    assert by["sally"]["auth_status"] == auth.STATUS_RELOGIN


def test_clear_profile_xai_shadows_leaves_root(tmp_path):
    _write(tmp_path / "auth.json", _xai_store(access="root", refresh="rootref"))
    hire.scaffold_profile(str(tmp_path), "Sally", "writer", "draft")
    _write(tmp_path / "profiles" / "sally" / "auth.json", _xai_store(access="", refresh=""))
    assert auth.clear_profile_xai_shadows(str(tmp_path)) == 1
    shadow = json.loads((tmp_path / "profiles" / "sally" / "auth.json").read_text())
    assert "xai-oauth" not in (shadow.get("providers") or {})
    root = json.loads((tmp_path / "auth.json").read_text())
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == "rootref"


def test_health_and_agents_expose_auth(tmp_path, monkeypatch):
    import http.client
    import threading

    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(tmp_path / "auth.json", _xai_store(access="", refresh="", relogin=True))
    _seed_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Sally", "writer", "draft", model_preset="grok-4.5")
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        health = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert health["ok"] is True
        assert health["auth"]["providers"][0]["status"] == "relogin_required"

        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        conn.request("GET", "/agents")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        sally = next(a for a in payload["agents"] if a["slug"] == "sally")
        assert sally["auth_status"] == "relogin_required"
        assert sally["auth_provider"] == "xai-oauth"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_reauth_start_poll_and_success_evicts(tmp_path, monkeypatch):
    import http.client
    import threading

    from gateway.config import PlatformConfig

    from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    _write(tmp_path / "auth.json", _xai_store(access="", refresh="", relogin=True))
    _seed_presets(tmp_path)
    hire.scaffold_profile(str(tmp_path), "Sally", "writer", "draft", model_preset="grok-4.5")
    _write(tmp_path / "profiles" / "sally" / "auth.json", _xai_store(access="", refresh=""))

    evicted: list[str] = []

    class Runner:
        def __init__(self):
            self._agent_cache = {"agent:sally:retinue_rooms:group:r-1": object()}

        def _evict_cached_agent(self, key):
            evicted.append(key)
            self._agent_cache.pop(key, None)

    monkeypatch.setattr(
        auth,
        "_request_xai_device_code",
        lambda: {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://x.ai/activate",
            "verification_uri_complete": "https://x.ai/activate?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 5,
        },
    )

    def _fake_poll(session_id: str) -> None:
        with auth._sessions_lock:
            sess = auth._sessions[session_id]
            sess["status"] = "approved"
        _write(tmp_path / "auth.json", _xai_store(access="new", refresh="newref"))

    monkeypatch.setattr(auth, "_poll_and_save_xai", _fake_poll)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.gateway_runner = Runner()
    httpd = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        return resp.status, payload

    try:
        status, payload = call("POST", "/auth/reauth", {"provider": "xai-oauth"})
        assert status == 202
        assert payload["user_code"] == "ABCD-EFGH"
        assert payload["verification_url"].startswith("https://x.ai/activate")
        assert "device_code" not in payload
        sid = payload["session_id"]

        # Poller is a daemon thread; wait for approve + shadow clear.
        sess = {}
        shadow: dict = {}
        for _ in range(50):
            st, sess = call("GET", f"/auth/reauth?session={sid}")
            shadow = json.loads((tmp_path / "profiles" / "sally" / "auth.json").read_text())
            if (
                st == 200
                and sess.get("status") == "approved"
                and "xai-oauth" not in (shadow.get("providers") or {})
            ):
                break
            __import__("time").sleep(0.02)
        assert sess.get("status") == "approved"
        assert "device_code" not in sess
        assert "xai-oauth" not in (shadow.get("providers") or {})
        assert any("sally" in key for key in evicted)

        status, payload = call("POST", "/auth/reauth", {"provider": "anthropic"})
        assert status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_reauth_reuses_pending_session(monkeypatch):
    monkeypatch.setattr(
        auth,
        "_request_xai_device_code",
        lambda: {
            "device_code": "dev-1",
            "user_code": "CODE-1",
            "verification_uri": "https://x.ai/activate",
            "expires_in": 600,
            "interval": 5,
        },
    )
    monkeypatch.setattr(auth, "_poll_and_save_xai", lambda _sid: None)
    first = auth.start_reauth("xai-oauth")
    second = auth.start_reauth("xai-oauth")
    assert first["session_id"] == second["session_id"]
    assert first["user_code"] == "CODE-1"
