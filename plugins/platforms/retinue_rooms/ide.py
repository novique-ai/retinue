"""IDE-attached rooms — option A.

Same podman/docker workspace-computer runtime for every room. An ``ide``
room bind-mounts a host path at ``/workspace``. A ``sandbox`` room gets an
isolated container and must not inherit that mount.

Per-room ``TERMINAL_DOCKER_SHARED_CONTAINER_KEY`` keeps the two kinds from
sharing a container. That key rides a ContextVar for the cycle
(``tools/workspace_context.py``), so rooms can run concurrently without
racing ``os.environ``.

Within a room, turns are sequential by design: ``take_wave()`` returns exactly
one speaker, and a member's reply is on the transcript before the next member
starts. (This docstring previously claimed members of one room run in
parallel; they never have.)
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from tools import workspace_context

from .engine import Room

WORKSPACE_SANDBOX = "sandbox"
WORKSPACE_IDE = "ide"
WORKSPACE_MODES = frozenset({WORKSPACE_SANDBOX, WORKSPACE_IDE})
IDE_ROOT_ENV = "RETINUE_IDE_ROOT"
CONTAINER_MOUNT = "/workspace"
SHARED_DIR_ENV = "RETINUE_SHARED_DIR"
SHARED_MOUNT = "/shared"
SHARED_MODE_RO = "ro"
SHARED_MODE_RW = "rw"
SHARED_MODES = frozenset({SHARED_MODE_RO, SHARED_MODE_RW})


def parse_workspace(value: object) -> str:
    raw = (str(value).strip().lower() if value is not None and str(value).strip() else WORKSPACE_SANDBOX)
    if raw not in WORKSPACE_MODES:
        raise ValueError("workspace must be 'sandbox' or 'ide'")
    return raw


def parse_shared_mode(value: object) -> str:
    raw = (str(value).strip().lower() if value is not None and str(value).strip() else SHARED_MODE_RO)
    if raw not in SHARED_MODES:
        raise ValueError("shared_mode must be 'ro' or 'rw'")
    return raw


def shared_mode_for(room: Room) -> str:
    """Absent or unknown on the record is read-only."""
    raw = (room.shared_mode or "").strip().lower()
    return raw if raw in SHARED_MODES else SHARED_MODE_RO


def configured_ide_root() -> Optional[str]:
    raw = (os.getenv(IDE_ROOT_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else None


def configured_shared_dir() -> Optional[str]:
    raw = (os.getenv(SHARED_DIR_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(raw)) if raw else None


def resolve_shared_dir() -> Optional[str]:
    """Absolute existing directory, or None if ``RETINUE_SHARED_DIR`` is unset.

    Unset means no mount and no directory created. A configured path that
    is missing is a hard error — never a silent skip.
    """
    path = configured_shared_dir()
    if path is None:
        return None
    if not os.path.isdir(path):
        raise ValueError(f"shared folder is not a directory: {path}")
    return path


def shared_dir_error() -> Optional[str]:
    """Why the configured shared folder cannot be mounted, or ``None``."""
    path = configured_shared_dir()
    if path is None:
        return None
    if not os.path.isdir(path):
        return f"shared folder is not a directory: {path}"
    return None


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


def _room_overlay_volumes(room: Room) -> List[str]:
    mode = parse_workspace(room.workspace)
    volumes: List[str] = []
    if mode == WORKSPACE_IDE:
        path = resolve_ide_path(room.ide_path)
        volumes.append(f"{path}:{CONTAINER_MOUNT}:rw")
    shared = resolve_shared_dir()
    if shared:
        volumes.append(f"{shared}:{SHARED_MOUNT}:{shared_mode_for(room)}")
    return volumes


def _overlay_fingerprint(volumes: List[str]) -> str:
    payload = json.dumps(volumes, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def container_key(room_id: str, workspace: str, overlay_fingerprint: Optional[str] = None) -> str:
    mode = parse_workspace(workspace)
    rid = (room_id or "room").strip() or "room"
    suffix = f"-{overlay_fingerprint}" if overlay_fingerprint else ""
    return f"retinue-{mode}-{rid}{suffix}"


def container_key_for_room(room: Room) -> str:
    mode = parse_workspace(room.workspace)
    return container_key(
        room.id,
        mode,
        overlay_fingerprint=_overlay_fingerprint(_room_overlay_volumes(room)),
    )


def overlay_env(room: Room, home_dir: Optional[str] = None) -> Dict[str, str]:
    """Env the terminal backend must see for this room's container."""
    from . import attachments

    volumes = _room_overlay_volumes(room)
    env = {
        "TERMINAL_ENV": "docker",
        "TERMINAL_DOCKER_SHARED_CONTAINER_KEY": container_key_for_room(room),
        "TERMINAL_CWD": CONTAINER_MOUNT,
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "0",
    }
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


# Of the values overlay_env() produces, only the container key and the volume
# list differ per room. TERMINAL_CWD and the mount flag are rooms-specific but
# identical for every room, and other tools read them straight from
# os.environ (the code-execution tool among them), so they are published
# process-wide — race-free, because every room publishes the same value.
#
# TERMINAL_ENV is deliberately NOT in this list. It is read directly at ~30
# sites across the engine and selects the terminal backend for the WHOLE
# process, including any other platform sharing this gateway. Setting it here
# would silently move a Discord or Telegram agent's shell into a container.
# Rooms require a docker-backed gateway, so the adapter checks that
# precondition at connect() (see require_docker_backend) instead of quietly
# imposing it per cycle.
INVARIANT_ENV = (
    "TERMINAL_CWD",
    "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
)


def docker_backend_error() -> Optional[str]:
    """Why this gateway cannot host room workspaces, or ``None`` if it can.

    Rooms are containerised by definition: every room gets a workspace
    computer, and an ``ide`` room bind-mounts a host path into it. That needs
    ``TERMINAL_ENV=docker`` for the process. Reporting the mismatch is
    strictly better than repairing it — the repair is invisible, process-wide,
    and lands on platforms that never asked for it.
    """
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend == "docker":
        return None
    return (
        f"rooms need a docker-backed gateway, but TERMINAL_ENV is {backend!r}. "
        "Set TERMINAL_ENV=docker (and TERMINAL_DOCKER_IMAGE) for the gateway "
        "process — see retinue/ROOMS.md."
    )


@contextmanager
def apply_room_workspace(
    room: Room, home_dir: Optional[str] = None
) -> Iterator[Dict[str, str]]:
    """Bind this room's workspace for one cycle.

    Safe to run concurrently with other rooms: the per-room values ride a
    ContextVar (see tools/workspace_context.py), which is per-asyncio-task and
    is propagated into the worker threads that dispatch tools, so no caller
    needs to serialize on a process-wide lock to keep its mounts.
    """
    overlay = overlay_env(room, home_dir)
    for key in INVARIANT_ENV:
        value = overlay.get(key)
        if value is not None and os.environ.get(key) != value:
            os.environ[key] = value
    with workspace_context.workspace(overlay):
        yield overlay
