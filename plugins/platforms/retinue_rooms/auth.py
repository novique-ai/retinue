"""Workspace provider-auth status and in-product reauth (issue #18).

File-only status — never call Hermes credential resolution here. That path
can refresh a rotating xAI grant and strip tokens. Rooms only *reads*
``auth.json`` and starts Hermes' existing device-code login on demand.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import hire

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_RELOGIN = "relogin_required"
STATUS_MISSING = "missing"
STATUS_NOT_REQUIRED = "not_required"

_XAI_PROVIDER = "xai-oauth"
_XAI_ALIASES = frozenset(
    {
        "xai-oauth",
        "grok-oauth",
        "x-ai-oauth",
        "xai-grok-oauth",
        "xai",
    }
)

_SUPPORTED_REAUTH = frozenset({_XAI_PROVIDER})

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _auth_path(home_dir: str, slug: Optional[str] = None) -> Path:
    if slug and slug != "default":
        return Path(home_dir) / "profiles" / slug / "auth.json"
    return Path(home_dir) / "auth.json"


def _load_auth_store(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _provider_state(store: Dict[str, Any], provider: str) -> Optional[Dict[str, Any]]:
    providers = store.get("providers")
    if not isinstance(providers, dict):
        return None
    state = providers.get(provider)
    return state if isinstance(state, dict) else None


def _classify_xai_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not state:
        return {
            "id": _XAI_PROVIDER,
            "status": STATUS_MISSING,
            "error": None,
        }
    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    err = state.get("last_auth_error") if isinstance(state.get("last_auth_error"), dict) else {}
    err_msg = (
        str(err.get("message") or err.get("error") or err.get("code") or "").strip()
        or None
    )
    # A successful device-code save leaves tokens in place and may leave a
    # stale last_auth_error from the previous revoked grant. Tokens win.
    if access and refresh:
        return {"id": _XAI_PROVIDER, "status": STATUS_OK, "error": None}
    if err.get("relogin_required") or state:
        # Failed refresh often leaves the block with no tokens.
        return {
            "id": _XAI_PROVIDER,
            "status": STATUS_RELOGIN,
            "error": err_msg or "provider credentials are missing after a failed refresh",
        }
    return {"id": _XAI_PROVIDER, "status": STATUS_MISSING, "error": None}


def _worse(left: str, right: str) -> str:
    rank = {STATUS_OK: 0, STATUS_MISSING: 1, STATUS_RELOGIN: 2}
    return left if rank.get(left, 0) >= rank.get(right, 0) else right


def agent_model_provider(home_dir: str, slug: str) -> str:
    """``model.provider`` for a hired profile, or empty."""
    slug = (slug or "").strip() or "default"
    if slug == "default":
        path = os.path.join(home_dir, "config.yaml")
    else:
        path = os.path.join(home_dir, "profiles", slug, "config.yaml")
    block = hire._read_model_block(path)
    if not block and slug != "default":
        block = hire._read_model_block(os.path.join(home_dir, "config.yaml"))
    fields = hire._fields_from_model_block(block)
    return str(fields.get("provider") or "").strip()


def normalize_provider(provider: str) -> str:
    raw = (provider or "").strip().lower()
    if raw in _XAI_ALIASES:
        return _XAI_PROVIDER
    return raw


def xai_status_for_store(path: Path) -> Optional[Dict[str, Any]]:
    """Classify a store that has an xAI block. ``None`` = no opinion."""
    state = _provider_state(_load_auth_store(path), _XAI_PROVIDER)
    if state is None:
        return None
    return _classify_xai_state(state)


def workspace_provider_status(home_dir: str) -> List[Dict[str, Any]]:
    """Worst-of workspace + profile xAI status. File reads only.

    Profiles with no ``providers.xai-oauth`` block inherit the workspace
    grant — they must not pull a healthy root down to ``missing``.
    """
    opinions: List[Dict[str, Any]] = []
    root = xai_status_for_store(_auth_path(home_dir))
    if root is not None:
        opinions.append(root)
    profiles = Path(home_dir) / "profiles"
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        names = []
    for name in names:
        if not os.path.isdir(profiles / name):
            continue
        one = xai_status_for_store(_auth_path(home_dir, name))
        if one is not None:
            opinions.append(one)
    if not opinions:
        return [{"id": _XAI_PROVIDER, "status": STATUS_MISSING, "error": None}]
    status = STATUS_OK
    error = None
    for one in opinions:
        status = _worse(status, one["status"])
        if one.get("error") and status == STATUS_RELOGIN:
            error = one.get("error") or error
    return [{"id": _XAI_PROVIDER, "status": status, "error": error}]


def health_payload(home_dir: str, rooms: int) -> Dict[str, Any]:
    return {
        "ok": True,
        "rooms": rooms,
        "auth": {"providers": workspace_provider_status(home_dir)},
    }


def annotate_agents(home_dir: str, agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    workspace = {p["id"]: p for p in workspace_provider_status(home_dir)}
    for agent in agents:
        slug = str(agent.get("slug") or "")
        if agent.get("local_llm"):
            agent["auth_status"] = STATUS_NOT_REQUIRED
            agent["auth_provider"] = None
            agent["auth_error"] = None
            continue
        provider = normalize_provider(agent_model_provider(home_dir, slug))
        agent["auth_provider"] = provider or None
        if provider != _XAI_PROVIDER:
            agent["auth_status"] = STATUS_OK
            agent["auth_error"] = None
            continue
        own = xai_status_for_store(_auth_path(home_dir, slug))
        # A profile without its own block inherits the workspace grant.
        if own is None:
            own = workspace.get(_XAI_PROVIDER) or {
                "id": _XAI_PROVIDER,
                "status": STATUS_MISSING,
                "error": None,
            }
        agent["auth_status"] = own["status"]
        agent["auth_error"] = own.get("error")
    return agents


def slugs_using_provider(home_dir: str, provider: str) -> List[str]:
    want = normalize_provider(provider)
    out: List[str] = []
    for agent in hire.list_agents(home_dir):
        slug = str(agent.get("slug") or "")
        if not slug or agent.get("local_llm"):
            continue
        if normalize_provider(agent_model_provider(home_dir, slug)) == want:
            out.append(slug)
    return out


def _atomic_write_auth(path: Path, store: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def clear_profile_xai_shadows(home_dir: str) -> int:
    """Drop competing ``providers.xai-oauth`` copies under profiles/.

    Hired agents must inherit the workspace grant. A shadowing profile
    copy is how a rotated refresh token keeps killing room members.
    """
    profiles = Path(home_dir) / "profiles"
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        return 0
    cleared = 0
    for name in names:
        path = _auth_path(home_dir, name)
        store = _load_auth_store(path)
        providers = store.get("providers")
        if not isinstance(providers, dict) or _XAI_PROVIDER not in providers:
            continue
        providers.pop(_XAI_PROVIDER, None)
        store["providers"] = providers
        try:
            _atomic_write_auth(path, store)
        except OSError:
            logger.warning("Retinue auth: could not clear xAI shadow in %s", path)
            continue
        cleared += 1
    return cleared


def public_session(sess: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "session_id": sess.get("session_id"),
        "provider": sess.get("provider"),
        "status": sess.get("status"),
        "flow": sess.get("flow"),
        "user_code": sess.get("user_code"),
        "verification_url": sess.get("verification_url"),
        "verification_uri": sess.get("verification_uri"),
        "expires_in": max(0, int(sess.get("expires_at", 0) - time.time()))
        if sess.get("expires_at")
        else None,
        "poll_interval": sess.get("poll_interval"),
        "error": sess.get("error"),
        "evicted": sess.get("evicted"),
    }
    return {k: v for k, v in out.items() if v is not None}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess is None:
            return None
        if sess.get("status") == "pending" and float(sess.get("expires_at") or 0) < time.time():
            sess["status"] = "expired"
            sess["error"] = "device-code login expired — start again"
        return public_session(sess)


def active_pending(provider: str) -> Optional[Dict[str, Any]]:
    want = normalize_provider(provider)
    now = time.time()
    with _sessions_lock:
        for sess in _sessions.values():
            if (
                sess.get("provider") == want
                and sess.get("status") == "pending"
                and float(sess.get("expires_at") or 0) > now
            ):
                return public_session(sess)
    return None


def _new_session(provider: str, device: Dict[str, Any]) -> Dict[str, Any]:
    sid = secrets.token_hex(8)
    expires_in = int(device.get("expires_in") or 900)
    sess = {
        "session_id": sid,
        "provider": provider,
        "status": "pending",
        "flow": "device_code",
        "device_code": str(device["device_code"]),
        "user_code": str(device["user_code"]),
        "verification_uri": str(device.get("verification_uri") or ""),
        "verification_url": str(
            device.get("verification_uri_complete") or device.get("verification_uri") or ""
        ),
        "poll_interval": int(device.get("interval") or 5),
        "expires_at": time.time() + max(60, expires_in),
        "error": None,
    }
    with _sessions_lock:
        _sessions[sid] = sess
    return sess


def _request_xai_device_code() -> Dict[str, Any]:
    import httpx
    from hermes_cli.auth import _xai_oauth_request_device_code

    with httpx.Client(
        timeout=httpx.Timeout(20.0),
        headers={"Accept": "application/json"},
    ) as client:
        return _xai_oauth_request_device_code(client)


def _poll_and_save_xai(session_id: str) -> None:
    import httpx
    from hermes_cli.auth import (
        _save_xai_oauth_tokens,
        _xai_oauth_discovery,
        _xai_oauth_poll_device_token,
        mark_provider_active_if_unset,
        unsuppress_credential_source,
    )

    with _sessions_lock:
        sess = _sessions.get(session_id)
    if not sess:
        return
    device_code = sess["device_code"]
    interval = int(sess["poll_interval"])
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        discovery = _xai_oauth_discovery(20.0)
        with httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json"},
        ) as client:
            token_data = _xai_oauth_poll_device_token(
                client,
                token_endpoint=discovery["token_endpoint"],
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        tokens = {
            "access_token": str(token_data.get("access_token", "") or "").strip(),
            "refresh_token": str(token_data.get("refresh_token", "") or "").strip(),
            "id_token": str(token_data.get("id_token", "") or "").strip(),
            "expires_in": token_data.get("expires_in"),
            "token_type": str(token_data.get("token_type") or "Bearer").strip() or "Bearer",
        }
        if not tokens["access_token"] or not tokens["refresh_token"]:
            raise RuntimeError("xAI device-code login returned incomplete tokens")
        _save_xai_oauth_tokens(
            tokens,
            discovery=discovery,
            last_refresh=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            auth_mode="oauth_device_code",
            set_active=False,
        )
        mark_provider_active_if_unset(_XAI_PROVIDER)
        unsuppress_credential_source(_XAI_PROVIDER, "device_code")
        with _sessions_lock:
            sess["status"] = "approved"
        logger.info("Retinue auth: xAI device-code login completed")
    except Exception as exc:
        logger.warning("Retinue auth: xAI device-code poll failed: %s", exc)
        with _sessions_lock:
            sess["status"] = "error"
            sess["error"] = str(exc)


def start_reauth(
    provider: str,
    *,
    on_success: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Start (or reuse) a device-code login for *provider*."""
    provider = normalize_provider(provider or _XAI_PROVIDER)
    if provider not in _SUPPORTED_REAUTH:
        raise ValueError(
            f"in-product reauth is not implemented for {provider or 'that provider'}"
        )
    existing = active_pending(provider)
    if existing:
        return existing
    device = _request_xai_device_code()
    sess = _new_session(provider, device)

    def _run() -> None:
        _poll_and_save_xai(sess["session_id"])
        with _sessions_lock:
            status = _sessions.get(sess["session_id"], {}).get("status")
        if status == "approved" and on_success is not None:
            try:
                on_success(provider)
            except Exception:
                logger.warning("Retinue auth: on_success callback failed", exc_info=True)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"retinue-oauth-{sess['session_id'][:6]}",
    ).start()
    return public_session(sess)


def _clear_root_last_auth_error(home_dir: str) -> None:
    path = _auth_path(home_dir)
    store = _load_auth_store(path)
    state = _provider_state(store, _XAI_PROVIDER)
    if not state or "last_auth_error" not in state:
        return
    state.pop("last_auth_error", None)
    providers = store.setdefault("providers", {})
    if isinstance(providers, dict):
        providers[_XAI_PROVIDER] = state
        try:
            _atomic_write_auth(path, store)
        except OSError:
            logger.warning("Retinue auth: could not clear last_auth_error on %s", path)


def finish_reauth_success(home_dir: str, provider: str, runner: Any = None) -> int:
    """After a good grant: drop profile shadows and evict cached agents."""
    provider = normalize_provider(provider)
    cleared = 0
    if provider == _XAI_PROVIDER:
        _clear_root_last_auth_error(home_dir)
        cleared = clear_profile_xai_shadows(home_dir)
    evicted = 0
    for slug in slugs_using_provider(home_dir, provider):
        evicted += hire.evict_profile_agent_cache(runner, slug)
    logger.info(
        "Retinue auth: reauth success provider=%s shadows_cleared=%s evicted=%s",
        provider,
        cleared,
        evicted,
    )
    return evicted


# Test seam — wipe in-process sessions between cases.
def _reset_sessions_for_tests() -> None:
    with _sessions_lock:
        _sessions.clear()
