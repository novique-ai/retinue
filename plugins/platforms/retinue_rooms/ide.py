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
from typing import Any, Dict, Iterator, List, Optional

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


def overlay_env(room: Room, home_dir: Optional[str] = None) -> Dict[str, str]:
    """Env the terminal backend must see for this room's container."""
    from . import attachments

    mode = parse_workspace(room.workspace)
    env = {
        "TERMINAL_ENV": "docker",
        "TERMINAL_DOCKER_SHARED_CONTAINER_KEY": container_key(room.id, mode),
        "TERMINAL_CWD": CONTAINER_MOUNT,
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "0",
    }
    volumes: List[str] = []
    if mode == WORKSPACE_IDE:
        path = resolve_ide_path(room.ide_path)
        volumes.append(f"{path}:{CONTAINER_MOUNT}:rw")
    if home_dir:
        uploads = attachments._dir(home_dir, room.id)
        os.makedirs(uploads, exist_ok=True)
        volumes.append(f"{uploads}:{CONTAINER_MOUNT}/uploads:ro")
    env["TERMINAL_DOCKER_VOLUMES"] = json.dumps(volumes)
    return env


def _under_root(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    return real == root_real or real.startswith(root_real + os.sep)


def list_folders(raw: Optional[str] = None) -> Dict[str, Any]:
    """Immediate subdirectories of *raw* (or RETINUE_IDE_ROOT).

    Browse is scoped to the configured IDE root when one is set, so the
    picker cannot walk the rest of the host. The create/patch path field
    still accepts any existing absolute directory.
    """
    root = configured_ide_root()
    requested = (raw or "").strip() or (root or "")
    if not requested:
        raise ValueError("pick a folder or set RETINUE_IDE_ROOT")
    path = os.path.abspath(os.path.expanduser(requested))
    if root and not _under_root(path, root):
        raise ValueError("folder must be under the configured IDE root")
    if not os.path.isdir(path):
        raise ValueError(f"not a directory: {path}")
    folders: List[Dict[str, str]] = []
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except OSError as e:
        raise ValueError(f"cannot list {path}: {e}") from e
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            folders.append({"name": name, "path": full})
    parent = os.path.dirname(path)
    if parent == path or (root and not _under_root(parent, root)):
        parent_out: Optional[str] = None
    else:
        parent_out = parent
    return {
        "path": path,
        "parent": parent_out,
        "root": root,
        "folders": folders,
    }


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
def apply_room_workspace(
    room: Room, home_dir: Optional[str] = None
) -> Iterator[Dict[str, str]]:
    """Overlay process env for one room cycle. Caller must serialize (asyncio lock)."""
    overlay = overlay_env(room, home_dir)
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
