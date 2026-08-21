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

from . import worktrees
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

# Host-broker reach (infra-90xc). The broker socket and its client live under
# the IDE root, and the shims in the workspace image used to reach them only
# through /workspace — which is the IDE root ONLY for a room mounted there. A
# room scoped to one repo (the point of scoping) lost bd, gh, flag-defect,
# host-git, cr, crm and rm.sh outright: the escalation path a governed
# retainer is told to use failed with "No such file". These two binds put the
# socket and the client at fixed paths in EVERY ide room, so reach no longer
# depends on where the room happens to be scoped. The client rides a bind (not
# the image) so updating it still needs no rebuild.
BROKER_MOUNT = "/opt/ide-broker"
BROKER_CLIENT_MOUNT = "/opt/ide-broker-client.py"
BROKER_SOCK_REL = ("data", "broker")
BROKER_CLIENT_REL = ("infra", "scripts", "room-broker-client.py")
# Container-path → host-path map for the room's mounts, published into each
# turn so the broker translates a container cwd against what is ACTUALLY
# mounted instead of assuming /workspace is the IDE root.
MOUNT_MAP_ENV = "RETINUE_WORKSPACE_MOUNTS"


def parse_workspace(value: object) -> str:
    raw = (str(value).strip().lower() if value is not None and str(value).strip() else WORKSPACE_SANDBOX)
    if raw not in WORKSPACE_MODES:
        raise ValueError("workspace must be 'sandbox' or 'ide'")
    return raw


def parse_shared_mode(value: object) -> str:
    raw = (str(value).strip().lower() if value is not None and str(value).strip() else SHARED_MODE_RW)
    if raw not in SHARED_MODES:
        raise ValueError("shared_mode must be 'ro' or 'rw'")
    return raw


def shared_mode_for(room: Room) -> str:
    """Absent means writable. A garbage value on disk stays read-only."""
    raw = (room.shared_mode or "").strip().lower()
    if not raw:
        return SHARED_MODE_RW
    return raw if raw in SHARED_MODES else SHARED_MODE_RO


def room_share_dir(room: Room) -> Optional[str]:
    """Host path for this room's folder under the shared drop, or None."""
    root = resolve_shared_dir()
    if root is None:
        return None
    return os.path.join(root, "rooms", room.id)


def ensure_share_layout(room: Room) -> Optional[str]:
    """Create inbox/ and rooms/<id>/ when the shared drop is configured."""
    root = resolve_shared_dir()
    if root is None:
        return None
    os.makedirs(os.path.join(root, "inbox"), exist_ok=True)
    path = os.path.join(root, "rooms", room.id)
    os.makedirs(path, exist_ok=True)
    return path

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


def room_worktree_repos(room: Room) -> List[str]:
    """Declared worktree repos, but only where they can apply (ide rooms)."""
    if parse_workspace(room.workspace) != WORKSPACE_IDE:
        return []
    return worktrees.parse_worktree_repos(getattr(room, "worktree_repos", None))


def mounts_ide_root(room: Room) -> bool:
    """True when this room's /workspace IS the whole IDE tree.

    A room scoped to one repo is the supported shape, and several behaviours
    (the root-scan fence, the briefing) are only correct for the unscoped
    case. With no configured root there is nothing to compare against, so the
    answer stays the conservative one: assume the big tree.
    """
    if parse_workspace(room.workspace) != WORKSPACE_IDE:
        return False
    root = configured_ide_root()
    if not root:
        return True
    return os.path.realpath(resolve_ide_path(room.ide_path)) == os.path.realpath(root)


def _broker_volumes() -> List[str]:
    """Socket + client binds so the host broker is reachable from any ide room.

    Silent when no IDE root is configured, or when the socket directory does
    not exist yet — a host without the broker service simply has no broker,
    which is the same answer the retainer got before, minus the misleading
    "No such file" from a hard-coded /workspace path.
    """
    root = configured_ide_root()
    if not root:
        return []
    specs: List[str] = []
    sock_dir = os.path.join(root, *BROKER_SOCK_REL)
    if os.path.isdir(sock_dir):
        specs.append(f"{sock_dir}:{BROKER_MOUNT}:rw")
    client = os.path.join(root, *BROKER_CLIENT_REL)
    if os.path.isfile(client):
        specs.append(f"{client}:{BROKER_CLIENT_MOUNT}:ro")
    return specs


def workspace_mount_map(room: Room) -> List[List[str]]:
    """``[[container_path, host_path], ...]`` for *room*, longest prefix first.

    The broker receives this per turn. Without it, it maps ``/workspace/x`` to
    ``<ide_root>/x`` — right only for a room mounted at the IDE root, and
    quietly wrong for a scoped room (host-git would act on the wrong tree) or
    for a worktree room (whose nested bind is not under the IDE root at all).
    """
    if parse_workspace(room.workspace) != WORKSPACE_IDE:
        return []
    path = resolve_ide_path(room.ide_path)
    pairs: List[List[str]] = [[CONTAINER_MOUNT, path]]
    for rel in room_worktree_repos(room):
        pairs.append(
            [
                f"{CONTAINER_MOUNT}/{rel}",
                worktrees.worktree_path(room.id, rel, worktrees.resolve_worktree_root()),
            ]
        )
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _room_overlay_volumes(room: Room) -> List[str]:
    mode = parse_workspace(room.workspace)
    volumes: List[str] = []
    if mode == WORKSPACE_IDE:
        path = resolve_ide_path(room.ide_path)
        volumes.append(f"{path}:{CONTAINER_MOUNT}:rw")
        # After the workspace mount, so each nested bind wins over the tree
        # underneath it (novique-ai/retinue#169).
        repos = room_worktree_repos(room)
        if repos:
            volumes.extend(
                worktrees.worktree_volumes(
                    room.id,
                    repos,
                    worktrees.resolve_worktree_root(),
                    CONTAINER_MOUNT,
                    path,
                )
            )
        volumes.extend(_broker_volumes())
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
    if mounts_ide_root(room):
        # Arms the mount-root search fence for this room's commands
        # (workspace_context.ide_root_scan_refusal, novique-ai/retinue#165).
        # Only for a room mounted at the IDE root: the fence exists because a
        # root scan crosses every repo on the machine. In a room scoped to one
        # repo, `grep -r /workspace` is the correct search, and refusing it
        # (with a message that calls /workspace "the entire host IDE") sent
        # the retainer hunting for paths that are not mounted.
        env[workspace_context.IDE_WORKSPACE_FLAG] = "1"
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
    room: Room,
    *,
    workspace: object = None,
    ide_path: object = None,
    touching_path: bool = False,
    worktree_repos: object = None,
    touching_worktrees: bool = False,
) -> Room:
    """Set workspace mode + resolved ide_path on *room*. Mutates and returns it."""
    mode = parse_workspace(workspace if workspace is not None else room.workspace)
    room.workspace = mode
    if mode == WORKSPACE_IDE:
        raw = ide_path if touching_path or ide_path is not None else room.ide_path
        room.ide_path = resolve_ide_path(None if raw is None else str(raw))
        if touching_worktrees or worktree_repos is not None:
            room.worktree_repos = worktrees.parse_worktree_repos(worktree_repos)
    else:
        room.ide_path = None
        # A sandbox room has no host tree to isolate; keeping the list would
        # silently re-arm on a later switch back to ide.
        room.worktree_repos = []
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
    room: Room, home_dir: Optional[str] = None, *, publish_invariants: bool = True
) -> Iterator[Dict[str, str]]:
    """Bind this room's workspace for one cycle.

    Safe to run concurrently with other rooms: the per-room values ride a
    ContextVar (see tools/workspace_context.py), which is per-asyncio-task and
    is propagated into the worker threads that dispatch tools, so no caller
    needs to serialize on a process-wide lock to keep its mounts.

    ``publish_invariants=False`` binds only the ContextVar overlay and leaves
    ``INVARIANT_ENV`` (``TERMINAL_CWD``) out of ``os.environ``. That write is
    process-global, and cron serialises it under ``_terminal_cwd_lock``
    (cron/scheduler.py) precisely because concurrent jobs share it. A caller
    running on a cron pool thread must not publish it: doing so writes outside
    that lock, and — because ``run_job`` snapshots ``_prior_terminal_cwd``
    *after* the caller has already written — poisons the restore, so the job's
    ``finally`` puts ``/workspace`` back instead of the real prior value and
    the wrong tree leaks into whatever runs next. The gateway loop, which owns
    the process, still publishes normally.
    """
    repos = room_worktree_repos(room)
    if repos:
        # Fail closed. A room that believes it is isolated but is not is worse
        # than a room that refuses to start (novique-ai/retinue#169).
        worktrees.ensure_worktrees(
            room.id,
            resolve_ide_path(room.ide_path),
            repos,
            worktrees.resolve_worktree_root(),
        )
    overlay = overlay_env(room, home_dir)
    ensure_share_layout(room)
    if publish_invariants:
        for key in INVARIANT_ENV:
            value = overlay.get(key)
            if value is not None and os.environ.get(key) != value:
                os.environ[key] = value
    with workspace_context.workspace(overlay):
        yield overlay
