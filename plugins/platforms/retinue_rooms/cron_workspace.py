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
from contextlib import ExitStack
from typing import Any, Callable, Optional

from . import brokertoken, cronjobs, ide

logger = logging.getLogger(__name__)

_original_run_job: Optional[Callable[..., Any]] = None


def _member_for(job: dict) -> str:
    """Which member this job speaks as — the slug its broker token will name.

    ``retinue.owner`` is authoritative. ``origin.thread_id`` is only a member
    slug when the origin really is the rooms platform: ``cronjobs._room_for``
    also resolves a room from ``retinue.room`` or a ``deliver`` token, and on
    those paths ``thread_id`` is some other platform's thread identifier. The
    original order preferred ``thread_id`` unconditionally, so a job routed to
    a room by ``deliver`` would have minted a broker credential named after a
    Discord/Telegram thread.
    """
    meta = cronjobs._dict(job.get("retinue"))
    owner = meta.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    origin = cronjobs._dict(job.get("origin"))
    if origin.get("platform") == cronjobs.ROOMS_PLATFORM:
        thread = origin.get("thread_id")
        if isinstance(thread, str) and thread.strip():
            return thread.strip()
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

        # Membership is the authorization model. A mention cycle only ever
        # binds a member the engine picked off the roster; the cron path had
        # no equivalent check, and create_cron_job validates the room and the
        # owner but never that the owner is IN that room. Before this wrap
        # that gap was inert — no mount, no token. With it, an unchecked owner
        # would receive the room's rw bind-mount and a genuine broker
        # credential (brokertoken.mint HMACs whatever slug it is handed).
        # An unknown member runs unwrapped rather than being refused: the job
        # is still the operator's, it simply gets no room capability.
        member = _member_for(record)
        if not member or member not in (room.members or []):
            logger.warning(
                "cron workspace: job %s targets room %s as %r, which is not a "
                "member — running without the room overlay or broker token",
                record.get("id") or "<unknown>",
                room_id,
                member or "<none>",
            )
            return inner(job, *args, **kwargs)

        tenv_token = None
        try:
            from tools import turn_env as _turn_env_mod

            tenv_token = _turn_env_mod.set_turn_env(
                {brokertoken.TOKEN_ENV: brokertoken.mint(home_dir(), member)}
            )
        except Exception:
            logger.debug("cron workspace: broker token bind failed", exc_info=True)
            tenv_token = None
        try:
            with ExitStack() as stack:
                try:
                    # publish_invariants=False: this runs on a cron pool
                    # thread, and TERMINAL_CWD is process-global state that
                    # run_job serialises under _terminal_cwd_lock. Writing it
                    # here would land outside that lock AND poison run_job's
                    # own restore, which snapshots the value after we return.
                    stack.enter_context(
                        ide.apply_room_workspace(
                            room, home_dir(), publish_invariants=False
                        )
                    )
                except Exception:
                    # Match the token bind: a broken overlay must not turn a
                    # previously-working job into a hard failure. apply_room_
                    # workspace raises on a stale ide_path, a bad
                    # RETINUE_SHARED_DIR, or an OSError in ensure_share_layout,
                    # and it raised BEFORE inner ran, so the job never
                    # executed at all.
                    logger.warning(
                        "cron workspace: overlay for room %s failed; running "
                        "job %s without it",
                        room_id,
                        record.get("id") or "<unknown>",
                        exc_info=True,
                    )
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
