"""Idle xAI OAuth keepalive for rooms (issue #34).

Hermes already refreshes on the next credential resolve. Rooms add a
background tick so an idle gateway warms the workspace grant shortly
before the access JWT expires (same skew ``resolve_xai_oauth_runtime_credentials``
already uses). One rotating copy: the workspace ``auth.json``. Terminal
``invalid_grant`` is left to Hermes' existing quarantine + the Reauth banner.

Do not copy a grant from another Hermes root.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

from . import auth

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 30.0
_ENV_INTERVAL = "RETINUE_XAI_KEEPALIVE_SECONDS"

ResolveFn = Callable[..., Dict[str, Any]]
RefreshFn = Callable[[str], Dict[str, Any]]


def interval_from_env() -> Optional[float]:
    """Seconds between ticks. ``None`` disables the loop (0 / negative)."""
    raw = (os.getenv(_ENV_INTERVAL) or "").strip()
    if not raw:
        return _DEFAULT_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_INTERVAL
    if value <= 0:
        return None
    return value


def should_keepalive(home_dir: str) -> bool:
    """True when a hired cloud member uses xAI and the workspace grant is ok."""
    if not auth.slugs_using_provider(home_dir, auth._XAI_PROVIDER):
        return False
    for provider in auth.workspace_provider_status(home_dir):
        if (
            provider.get("id") == auth._XAI_PROVIDER
            and provider.get("status") == auth.STATUS_OK
        ):
            return True
    return False


def _default_resolve(**kwargs: Any) -> Dict[str, Any]:
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    return resolve_xai_oauth_runtime_credentials(**kwargs)


def refresh_workspace_xai(
    home_dir: str,
    *,
    resolve: Optional[ResolveFn] = None,
) -> Dict[str, Any]:
    """Refresh the workspace grant if a tick is due. Never writes profile copies."""
    if not should_keepalive(home_dir):
        return {"skipped": True, "reason": "not_eligible"}

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    resolve_fn = resolve or _default_resolve
    token = set_hermes_home_override(home_dir)
    try:
        resolve_fn(refresh_if_expiring=True)
        return {"skipped": False, "ok": True}
    except Exception as exc:
        relogin = bool(getattr(exc, "relogin_required", False))
        logger.warning(
            "Retinue rooms: xAI keepalive refresh failed relogin_required=%s: %s",
            relogin,
            exc,
        )
        return {
            "skipped": False,
            "ok": False,
            "error": str(exc),
            "relogin_required": relogin,
        }
    finally:
        reset_hermes_home_override(token)


class XaiKeepalive:
    """Daemon loop that ticks ``refresh_fn(home_dir)`` while the adapter is up."""

    def __init__(
        self,
        home_dir_fn: Callable[[], str],
        *,
        interval: float = _DEFAULT_INTERVAL,
        refresh_fn: Optional[RefreshFn] = None,
    ) -> None:
        self._home_dir_fn = home_dir_fn
        self.interval = max(0.01, float(interval))
        self._refresh_fn = refresh_fn or refresh_workspace_xai
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="retinue-xai-keepalive",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_fn(self._home_dir_fn())
            except Exception:
                logger.warning(
                    "Retinue rooms: xAI keepalive tick crashed",
                    exc_info=True,
                )
            self._stop.wait(self.interval)
