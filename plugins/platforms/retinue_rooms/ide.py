"""IDE-attached rooms — option A.

Same podman/docker workspace-computer runtime for every room. An ``ide``
room bind-mounts a host path at ``/workspace``. A ``sandbox`` room gets an
isolated container and must not inherit that mount.

Per-room ``TERMINAL_DOCKER_SHARED_CONTAINER_KEY`` keeps the two kinds from
sharing a container. Overlaying that env for a turn cycle is serialized so
two rooms cannot race ``os.environ`` (members of one room still run in
parallel — they share one computer).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

from .engine import Room

WORKSPACE_SANDBOX = "sandbox"
WORKSPACE_IDE = "ide"
WORKSPACE_MODES = frozenset({WORKSPACE_SANDBOX, WORKSPACE_IDE})
IDE_ROOT_ENV = "RETINUE_IDE_ROOT"
CONTAINER_MOUNT = "/workspace"


def parse_workspace(value: object) -> str:
    raw = (str(value).strip().lower() if value is not None and str(value).strip() else WORKSPACE_SANDBOX)
    if raw not in WORKSPACE_MODES:
        raise ValueError("workspace must be 'sandbox' or 'ide'")
    return raw


def configured_ide_root() -> Optional[str]:
    raw = (os.getenv(IDE_ROOT_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else None


def resolve_ide_path(explicit: Optional[str] = None) -> str:
    """Absolute existing directory: body ``ide_path``, else ``RETINUE_IDE_ROOT``."""
    raw = (explicit or "").strip() or (os.getenv(IDE_ROOT_ENV) or "").strip()
    if not raw:
        raise ValueError(
            "IDE rooms need a host path: pass ide_path or set RETINUE_IDE_ROOT"
        )
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise ValueError(f"IDE path is not a directory: {path}")
    return path


def container_key(room_id: str, workspace: str) -> str:
    mode = parse_workspace(workspace)
    rid = (room_id or "room").strip() or "room"
    return f"retinue-{mode}-{rid}"


def overlay_env(room: Room) -> Dict[str, str]:
    """Env the terminal backend must see for this room's container."""
    mode = parse_workspace(room.workspace)
    env = {
        "TERMINAL_ENV": "docker",
        "TERMINAL_DOCKER_SHARED_CONTAINER_KEY": container_key(room.id, mode),
        "TERMINAL_CWD": CONTAINER_MOUNT,
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "0",
    }
    if mode == WORKSPACE_IDE:
        path = resolve_ide_path(room.ide_path)
        env["TERMINAL_DOCKER_VOLUMES"] = json.dumps([f"{path}:{CONTAINER_MOUNT}:rw"])
    else:
        # Clear a gateway-global mount so sandbox rooms stay isolated.
        env["TERMINAL_DOCKER_VOLUMES"] = "[]"
    return env


def apply_workspace_fields(
    room: Room, *, workspace: object = None, ide_path: object = None, touching_path: bool = False
) -> Room:
    """Set workspace mode + resolved ide_path on *room*. Mutates and returns it."""
    mode = parse_workspace(workspace if workspace is not None else room.workspace)
    room.workspace = mode
    if mode == WORKSPACE_IDE:
        raw = ide_path if touching_path or ide_path is not None else room.ide_path
        room.ide_path = resolve_ide_path(None if raw is None else str(raw))
    else:
        room.ide_path = None
    return room


@contextmanager
def apply_room_workspace(room: Room) -> Iterator[Dict[str, str]]:
    """Overlay process env for one room cycle. Caller must serialize (asyncio lock)."""
    overlay = overlay_env(room)
    saved = {key: os.environ.get(key) for key in overlay}
    os.environ.update(overlay)
    try:
        yield overlay
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
