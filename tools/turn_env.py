"""Per-turn environment for sandbox command execution.

Carried patch (retinue), sibling of :mod:`tools.workspace_context`. Room
containers are shared per ROOM, not per member, so an env var set at
container create cannot carry member identity. Turns within a room are
serialized, though — so a per-TURN value injected into each executed
command's environment identifies the member exactly for the duration of
their turn.

The rooms adapter binds a mapping (today: ``RETINUE_BROKER_TOKEN``) around
the member's turn; :func:`export_prefix` turns it into a shell-safe
``export K=V; `` prefix that :meth:`BaseEnvironment.execute` prepends to the
command. Properties that matter:

- **ContextVar carrier**: per-asyncio-task, snapshotted into tool worker
  threads by ``tools.thread_context`` — the value set at the top of a room
  cycle is in force when the container exec is built deep inside a tool
  call, with nothing global to race (same argument as workspace_context).
- **No backend plumbing**: the prefix rides the command string, so docker,
  local, and ssh backends all honor it without per-backend env support.
- **Empty by default**: no binding → empty prefix → every non-rooms surface
  (CLI, desktop, delegate_task children) is byte-identical to before.

Upstream feature request: NousResearch/hermes-agent#84671 (per-call env for
sandbox exec) — fold this in when an equivalent knob lands.
"""

from __future__ import annotations

import contextvars
import re
import shlex
from typing import Mapping, Optional

_turn_env: contextvars.ContextVar[Optional[Mapping[str, str]]] = contextvars.ContextVar(
    "hermes_turn_env", default=None
)

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def set_turn_env(mapping: Optional[Mapping[str, str]]) -> contextvars.Token:
    """Bind *mapping* for this context; pass the token to :func:`reset`."""
    return _turn_env.set(dict(mapping) if mapping else None)


def reset(token: contextvars.Token) -> None:
    _turn_env.reset(token)


def current() -> Optional[Mapping[str, str]]:
    return _turn_env.get()


def export_prefix() -> str:
    """Shell-safe ``export K=V; `` prefix for the bound mapping, or ``""``.

    Names are constrained to uppercase env-var shape and values are
    shlex-quoted, so a bound value can never break out of the export
    statement into the command.
    """
    mapping = _turn_env.get()
    if not mapping:
        return ""
    parts = []
    for name, value in mapping.items():
        if not _NAME_RE.match(str(name) or ""):
            continue
        parts.append(f"export {name}={shlex.quote(str(value))}")
    return "; ".join(parts) + "; " if parts else ""
