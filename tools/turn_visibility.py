"""Per-turn provider-event notifier (novique-ai/retinue#166).

Carried patch (retinue), sibling of :mod:`tools.turn_env` and
:mod:`tools.workspace_context`. The API retry loop lives deep in
``agent.conversation_loop``; when a provider stalls or rejects mid-turn, the
only witnesses were the gateway journal and ``podman top`` — a Retinue room
kept showing "X is on it." through five provider kills on 2026-08-20.

The rooms adapter binds a callback around each member turn; the conversation
loop's retry site calls :func:`notify` with a compact human summary (never a
payload). Properties, same as the sibling modules:

- **ContextVar carrier**: per-asyncio-task, snapshotted into the worker
  threads that run the conversation loop, so the callback bound at the top
  of a room turn is visible where the retry actually happens.
- **Empty by default**: no binding -> notify() is a no-op -> CLI, desktop,
  and delegate children are byte-identical to before.
- **Never load-bearing**: a broken callback is swallowed; visibility must
  not be able to fail a turn.

Upstream feature request: NousResearch/hermes-agent#84671 (per-call hooks) —
fold this in when an equivalent seam lands.
"""

from __future__ import annotations

import contextvars
from typing import Callable, Optional

_notifier: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("hermes_turn_visibility", default=None)
)


def set_notifier(callback: Optional[Callable[[str], None]]) -> contextvars.Token:
    """Bind *callback* for this context; pass the token to :func:`reset`."""
    return _notifier.set(callback)


def reset(token: contextvars.Token) -> None:
    _notifier.reset(token)


def current() -> Optional[Callable[[str], None]]:
    return _notifier.get()


def notify(message: str) -> None:
    """Deliver a compact provider-event summary to the bound notifier.

    No-op when nothing is bound. Exceptions from the callback are swallowed:
    the retry loop calls this mid-recovery, and visibility must never be the
    thing that kills the turn it is reporting on.
    """
    callback = _notifier.get()
    if callback is None:
        return
    try:
        callback(str(message))
    except Exception:
        pass
