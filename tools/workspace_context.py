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


# --- ide-room mount-root search fence (novique-ai/retinue#165) ---------------
#
# An ide room bind-mounts the operator's ENTIRE IDE at /workspace. A recursive
# search rooted there is legal shell but always a mistake: minutes of wall
# clock under the exclusive room lock, and a ~50KB tool dump that poisons the
# member's session (#164). The rooms overlay marks ide rooms with
# IDE_WORKSPACE_FLAG; the terminal tool consults ide_root_scan_refusal()
# before executing. This is a product fence against an honest-but-wasteful
# pattern, not a security boundary — `cd /workspace && grep -r .` still
# passes, and the room briefing owns that education.

IDE_WORKSPACE_FLAG = "RETINUE_IDE_WORKSPACE"

_IDE_MOUNT_ROOT = "/workspace"
_COMMAND_BREAKS = frozenset({"|", "||", "&&", ";", "&"})
_RECURSIVE_LONG_OPTS = frozenset({"--recursive", "--dereference-recursive"})
#: Programs that recurse by default when handed a directory.
_RECURSIVE_BY_DEFAULT = frozenset({"rg", "ripgrep", "find"})
#: Programs that recurse only with an -r/-R style flag.
_RECURSIVE_WITH_FLAG = frozenset({"grep", "egrep", "fgrep"})


def _fence_tokens(command: str) -> list:
    import shlex

    try:
        # punctuation_chars makes ;, |, && standalone tokens, so the
        # per-segment program scan below sees compound commands correctly.
        lex = shlex.shlex(command or "", posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return (command or "").split()


def _is_mount_root(token: str) -> bool:
    return token in (_IDE_MOUNT_ROOT, _IDE_MOUNT_ROOT + "/", _IDE_MOUNT_ROOT + "/.")


def scans_ide_root(command: str) -> bool:
    """True when *command* recursively searches the ide mount root itself.

    Refuses ``grep -r … /workspace``, ``rg … /workspace``, and
    ``find /workspace …`` (each observed live on 2026-08-20); a path one
    level deeper (``/workspace/<repo>``) always passes.
    """
    tokens = _fence_tokens(command)
    for index, token in enumerate(tokens):
        if not _is_mount_root(token):
            continue
        start = 0
        for j in range(index - 1, -1, -1):
            if tokens[j] in _COMMAND_BREAKS:
                start = j + 1
                break
        if start >= len(tokens):
            continue
        program = os.path.basename(tokens[start])
        if program in _RECURSIVE_BY_DEFAULT:
            return True
        if program in _RECURSIVE_WITH_FLAG:
            for opt in tokens[start + 1 : index]:
                if opt in _RECURSIVE_LONG_OPTS:
                    return True
                if (
                    opt.startswith("-")
                    and not opt.startswith("--")
                    and any(c in "rR" for c in opt[1:])
                ):
                    return True
    return False


def ide_root_scan_refusal(command: str) -> Optional[str]:
    """Refusal message for an ide-room mount-root scan, or ``None`` to allow.

    Fires only when the active workspace overlay carries
    ``IDE_WORKSPACE_FLAG`` — CLI, desktop, delegate children, and sandbox
    rooms are untouched.
    """
    if getenv(IDE_WORKSPACE_FLAG, "") != "1":
        return None
    if not scans_ide_root(command):
        return None
    return (
        "refused: recursive search rooted at /workspace. /workspace is the "
        "entire host IDE (every repo and data tree), not this task's "
        "project — a root scan takes minutes and floods your context. "
        "Scope the search to the repo you are working in, e.g. "
        "`grep -rn PATTERN /workspace/<repo>/`, or list the root first "
        "with `ls /workspace`."
    )
