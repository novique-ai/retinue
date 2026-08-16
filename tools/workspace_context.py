"""Per-context workspace overlay for the terminal backend.

Carried patch (retinue). The terminal backend picks a container from process
environment: ``TERMINAL_DOCKER_SHARED_CONTAINER_KEY`` decides identity and
cache key, ``TERMINAL_DOCKER_VOLUMES`` decides what is mounted. That is fine
for one agent per process, and wrong for a gateway hosting many rooms: a room
cycle had to mutate ``os.environ`` to select its container, ``os.environ`` is
process-global, so two cycles could race each other's mounts. The rooms adapter
therefore serialized every cycle behind one process-wide lock, and a single
turn — up to 1800s on a local model — blocked every other room, including
pure-chat rooms that never touch a terminal (novique-ai/retinue#67).

The blocking was a side effect of the *carrier*, not a design ceiling. A
ContextVar is the carrier the value actually wants: it is per-asyncio-task and
per-thread, it is inherited by child tasks, and ``tools.thread_context``
already snapshots it into the worker threads that dispatch tools — so a value
set at the top of a room cycle is still in force when the container is created
deep inside a tool call, with no global state and nothing to serialize.

Lookup order is overlay-then-environment, and an *active* overlay wins
outright, including when it holds an empty value. A room that mounts nothing
must not inherit the process-wide volume list; "this context defines the
workspace" is the whole contract. With no overlay active every caller — CLI,
desktop, ``delegate_task`` children, RL rollouts — reads plain ``os.environ``
exactly as before.

Upstream feature request: NousResearch/hermes-agent#84671 — drop this patch
when an equivalent per-call knob lands.
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from typing import Dict, Iterator, Mapping, Optional

#: Env names whose value may be supplied per-context instead of per-process.
SHARED_CONTAINER_KEY = "TERMINAL_DOCKER_SHARED_CONTAINER_KEY"
DOCKER_VOLUMES = "TERMINAL_DOCKER_VOLUMES"

_overlay: contextvars.ContextVar[Optional[Mapping[str, str]]] = contextvars.ContextVar(
    "hermes_workspace_overlay", default=None
)


def current() -> Optional[Mapping[str, str]]:
    """The overlay in force for this context, or ``None``."""
    return _overlay.get()


def getenv(name: str, default: str = "") -> str:
    """``os.getenv`` that consults the active workspace overlay first.

    An active overlay that carries *name* wins even if its value is empty —
    see the module docstring. Names the overlay does not carry fall through to
    the process environment.
    """
    overlay = _overlay.get()
    if overlay is not None and name in overlay:
        value = overlay[name]
        return "" if value is None else str(value)
    return os.getenv(name, default)


def shared_container_key() -> str:
    """Workspace container key for this context (``""`` when unset)."""
    return (getenv(SHARED_CONTAINER_KEY, "") or "").strip()


@contextmanager
def workspace(overlay: Optional[Dict[str, str]]) -> Iterator[Optional[Mapping[str, str]]]:
    """Bind *overlay* for the duration of the block.

    Nesting restores the previous overlay rather than clearing it, so an inner
    scope cannot strand an outer one. A ``None`` overlay is a no-op, which
    keeps callers free of ``if`` branches at the call site.
    """
    if overlay is None:
        yield _overlay.get()
        return
    token = _overlay.set(dict(overlay))
    try:
        yield overlay
    finally:
        _overlay.reset(token)
