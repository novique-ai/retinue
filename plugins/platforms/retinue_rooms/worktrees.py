"""Per-room git worktrees — isolating concurrent ide rooms (#169).

An ``ide`` room bind-mounts a host tree at ``/workspace``. Most of them mount
the *same* tree, and turns in different rooms run concurrently (serialisation
is per member, not per tree). Two agents therefore edit one working tree at
once: a lost edit, a mixed ``git commit``, or a test run against a state that
never existed, all silent.

A room may declare ``worktree_repos`` — repo paths *relative to its own
``ide_path``*. Each one gets a private ``git worktree`` checked out on
``retinue/room/<room-id>``, bind-mounted over the matching location inside
``/workspace``. The agent still sees ``/workspace/infra``; it is simply theirs
alone. Everything not declared stays exactly as before, so this is inert until
a room opts in.

Why a branch and not ``main``: git refuses to check out one branch in two
worktrees, which is the same interlock we want anyway. The room commits to its
own branch and the driver merges on the host, so no agent writes to ``main``
directly.

Path computation here is pure — ``worktree_volumes`` only says where a
worktree *would* live, so container keys and mount lists stay side-effect
free. ``ensure_worktrees`` is the one function that touches the disk, and it
runs once per cycle from ``ide.apply_room_workspace``.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import List, Optional, Tuple

WORKTREE_ROOT_ENV = "RETINUE_WORKTREE_ROOT"
BRANCH_PREFIX = "retinue/room"
_GIT_TIMEOUT = 60


class WorktreeError(RuntimeError):
    """A declared worktree could not be prepared.

    Raised rather than falling back to the shared tree: silently sharing is
    the defect this module exists to remove, and a room that believes it is
    isolated when it is not is worse than a room that refuses to start.
    """


def parse_worktree_repos(value: object) -> List[str]:
    """Normalise the room field to a list of clean relative paths.

    ``None``/empty means "not opted in". Absolute paths and anything that
    escapes the mount are rejected here rather than at mount time, so a bad
    value fails at create/patch with a message the operator can act on.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ValueError("worktree_repos must be a list of repo paths, not a string")
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("worktree_repos must be a list of repo paths")

    cleaned: List[str] = []
    for item in items:
        raw = str(item or "").strip().strip("/")
        if not raw:
            continue
        if os.path.isabs(str(item)):
            raise ValueError(
                f"worktree_repos entries are relative to the room's ide_path: {item!r}"
            )
        norm = os.path.normpath(raw)
        if norm == "." or norm.startswith(".."):
            raise ValueError(f"worktree_repos entry escapes the workspace: {item!r}")
        posix = norm.replace(os.sep, "/")
        if posix not in cleaned:
            cleaned.append(posix)
    return cleaned


def branch_for(room_id: str) -> str:
    """Branch this room's worktrees check out. One branch per room, all repos.

    Room ids are already slug+hex, but a stray character would produce an
    invalid refname and a confusing git error, so squash anything unexpected.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (room_id or "room").strip()).strip("-.")
    return f"{BRANCH_PREFIX}/{safe or 'room'}"


def resolve_worktree_root() -> str:
    """Where room worktrees live on the host.

    Deliberately outside the mounted IDE tree: worktrees are the isolation
    boundary, so parking them inside the tree they isolate would put one
    room's private checkout inside every other room's mount.

    Takes no ``home_dir`` on purpose. The mount list and the container key are
    computed from different call sites, one of which has a home dir and one of
    which does not; deriving the root from a parameter would let those two
    disagree and silently churn a fresh container every turn.
    """
    configured = (os.getenv(WORKTREE_ROOT_ENV) or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    home = (os.getenv("HERMES_HOME") or "").strip() or os.path.expanduser("~/.hermes")
    return os.path.join(os.path.abspath(os.path.expanduser(home)), "worktrees")


def worktree_path(room_id: str, rel: str, root: str) -> str:
    """Host path of one room's worktree for *rel*. Pure — no disk access."""
    return os.path.join(root, room_id, rel.replace("/", os.sep))


def _run_git(repo: str, *args: str) -> Tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _repo_root(path: str) -> Optional[str]:
    """Absolute toplevel of the repo at *path*, or None if it is not one."""
    if not os.path.isdir(path):
        return None
    rc, out = _run_git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not out:
        return None
    return os.path.abspath(out.splitlines()[-1].strip())


def _branch_exists(repo: str, branch: str) -> bool:
    rc, _ = _run_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return rc == 0


def ensure_worktree(ide_path: str, rel: str, room_id: str, root: str) -> str:
    """Create or reuse this room's worktree for *rel*; return its host path.

    Idempotent: an existing worktree is verified and handed back, so this is
    safe to call at the start of every cycle.
    """
    source = os.path.join(ide_path, rel.replace("/", os.sep))
    repo = _repo_root(source)
    if repo is not None and os.path.isfile(os.path.join(repo, ".git")):
        # `.git` as a FILE means this repo is itself a linked worktree, so its
        # real git dir lives somewhere else entirely and the single bind-mount
        # below would not cover it. Refuse rather than hand the room a
        # checkout whose git cannot resolve (novique-ai/retinue#172).
        raise WorktreeError(
            f"{rel!r} is itself a linked git worktree ({source}); isolate the "
            f"main checkout instead. A room worktree needs the source repo's "
            f"own .git directory to bind-mount."
        )
    if repo is None:
        raise WorktreeError(
            f"worktree_repos entry {rel!r} is not a git repository "
            f"({source}). A room can only isolate a repo, not an arbitrary "
            f"directory — the IDE root itself is usually a container of many "
            f"repos, so name the repo (e.g. 'infra')."
        )

    target = worktree_path(room_id, rel, root)
    branch = branch_for(room_id)

    if os.path.isdir(os.path.join(target, ".git")) or os.path.isfile(
        os.path.join(target, ".git")
    ):
        # Already prepared. Confirm git still owns it — a hand-deleted admin
        # dir leaves a directory that looks fine and mounts as a normal folder.
        if _repo_root(target) is None:
            raise WorktreeError(
                f"worktree for {rel!r} at {target} is no longer a valid git "
                f"worktree; remove it and let the room recreate it "
                f"(git -C {repo} worktree prune)."
            )
        return target

    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Stale administrative entries survive a manual `rm -rf` of the directory
    # and make `worktree add` fail with "already registered".
    _run_git(repo, "worktree", "prune")

    args = ["worktree", "add"]
    if _branch_exists(repo, branch):
        # Re-attaching a room to a repo it worked before: keep its history.
        args += [target, branch]
    else:
        args += ["-b", branch, target]
    rc, out = _run_git(repo, *args)
    if rc != 0:
        raise WorktreeError(f"could not create worktree for {rel!r} in {repo}: {out}")
    return target


def ensure_worktrees(
    room_id: str, ide_path: str, repos: List[str], root: str
) -> List[Tuple[str, str]]:
    """Prepare every declared worktree. Returns (rel, host path) pairs."""
    prepared: List[Tuple[str, str]] = []
    for rel in repos:
        prepared.append((rel, ensure_worktree(ide_path, rel, room_id, root)))
    return prepared


def source_git_dir(ide_path: str, rel: str) -> str:
    """Host path of the source repo's ``.git`` for *rel*."""
    return os.path.join(ide_path, rel.replace("/", os.sep), ".git")


def worktree_volumes(
    room_id: str, repos: List[str], root: str, container_mount: str, ide_path: str
) -> List[str]:
    """Mount specs layering each worktree over its place in the workspace.

    Ordered after the workspace mount by the caller so the nested bind wins.

    Each worktree also needs the SOURCE repo's ``.git`` bound at its own host
    path (novique-ai/retinue#172). A linked worktree's ``.git`` is a file
    holding an absolute pointer — ``gitdir: /home/clay/IDE/infra/.git/
    worktrees/<name>`` — and ``commondir`` resolves relative to that. Inside
    the container that path does not exist: only the IDE tree is mounted, at
    ``/workspace``. Worse, the worktree bind sits ON TOP of
    ``/workspace/<rel>``, hiding the very ``.git`` it depends on. Without this
    second mount every git command in an isolated room fails with
    ``fatal: not a git repository`` — isolation that works and git that does
    not, which is worse than the shared tree it replaced.

    Binding it at the identical host path (rather than rewriting the pointer
    to a container path) keeps the absolute gitdir valid on BOTH sides, so the
    operator can still merge the room's branch from the host.
    """
    specs: List[str] = []
    for rel in repos:
        host = worktree_path(room_id, rel, root)
        specs.append(f"{host}:{container_mount}/{rel}:rw")
        git_dir = source_git_dir(ide_path, rel)
        specs.append(f"{git_dir}:{git_dir}:rw")
    return specs
