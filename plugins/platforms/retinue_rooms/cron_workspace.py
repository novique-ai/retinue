"""Bind a destination room's workspace around Hermes cron ``run_job``.

Mention turns in a room wrap the cycle with :func:`ide.apply_room_workspace`
(and mint a broker token). Scheduled jobs with ``deliver=origin`` into the
same room used to skip that wrap: the reply landed on the transcript, but
the job's tools saw an empty ``/workspace`` (issue #171).

This module patches ``cron.scheduler.run_job`` for the lifetime of the
rooms adapter. Jobs with no room origin are unchanged. Unknown rooms are
also unchanged (no overlay), so a stale job id cannot invent a workspace.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import brokertoken, cronjobs, ide

logger = logging.getLogger(__name__)

_original_run_job: Optional[Callable[..., Any]] = None


def _member_for(job: dict) -> str:
    origin = cronjobs._dict(job.get("origin"))
    thread = origin.get("thread_id")
    if isinstance(thread, str) and thread.strip():
        return thread.strip()
    meta = cronjobs._dict(job.get("retinue"))
    owner = meta.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    return ""


def _wrap_run_job(
    get_room: Callable[[str], Any],
    home_dir: Callable[[], str],
    inner: Callable[..., Any],
):
    def run_job(job, *args, **kwargs):
        record = job if isinstance(job, dict) else {}
        room_id = cronjobs._room_for(record)
        room = get_room(room_id) if room_id else None
        if room is None:
            return inner(job, *args, **kwargs)
        member = _member_for(record)
        tenv_token = None
        try:
            if member:
                from tools import turn_env as _turn_env_mod

                tenv_token = _turn_env_mod.set_turn_env(
                    {brokertoken.TOKEN_ENV: brokertoken.mint(home_dir(), member)}
                )
        except Exception:
            logger.debug("cron workspace: broker token bind failed", exc_info=True)
            tenv_token = None
        try:
            with ide.apply_room_workspace(room, home_dir()):
                return inner(job, *args, **kwargs)
        finally:
            if tenv_token is not None:
                try:
                    from tools import turn_env as _turn_env_mod

                    _turn_env_mod.reset(tenv_token)
                except Exception:
                    logger.debug("cron workspace: broker token reset failed", exc_info=True)

    return run_job


def install(adapter) -> None:
    """Patch ``cron.scheduler.run_job`` so room-bound jobs inherit the overlay."""
    global _original_run_job
    import cron.scheduler as sched

    if _original_run_job is not None:
        return
    _original_run_job = sched.run_job
    sched.run_job = _wrap_run_job(adapter.store.get, adapter._home_dir, _original_run_job)


def uninstall() -> None:
    """Restore the unpatched ``run_job``. Idempotent."""
    global _original_run_job
    if _original_run_job is None:
        return
    import cron.scheduler as sched

    sched.run_job = _original_run_job
    _original_run_job = None
