"""Per-room git worktree isolation (novique-ai/retinue#169).

The defect these cover: every ide room bind-mounts one host tree read-write,
turns in different rooms run concurrently, and nothing stopped two agents from
editing the same working tree — or staging each other's files — at once.

The last test is the one that matters: it stages a file in one room's worktree
and asserts the other room's index does not see it. That is the exact failure
observed on 2026-08-20, and it fails without the isolation.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from . import ide, worktrees
from .engine import Room


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture()
def ide_root(tmp_path, monkeypatch):
    """An IDE root that is NOT itself a repo, holding one repo — the real shape."""
    root = tmp_path / "IDE"
    repo = root / "infra"
    repo.mkdir(parents=True)
    _git(str(repo), "init", "-q", "-b", "main")
    _git(str(repo), "config", "user.email", "t@example.com")
    _git(str(repo), "config", "user.name", "T")
    (repo / "keep.txt").write_text("base\n")
    _git(str(repo), "add", "keep.txt")
    _git(str(repo), "commit", "-qm", "base")
    (root / "data").mkdir()
    monkeypatch.setenv(worktrees.WORKTREE_ROOT_ENV, str(tmp_path / "wt"))
    return root


def _room(room_id: str, ide_root, repos):
    return Room(
        id=room_id,
        name=room_id,
        members=["m"],
        workspace="ide",
        ide_path=str(ide_root),
        worktree_repos=list(repos),
    )


# ── field parsing ────────────────────────────────────────────────────────


def test_absent_field_means_shared_tree_as_before():
    assert worktrees.parse_worktree_repos(None) == []
    assert worktrees.parse_worktree_repos([]) == []


def test_paths_are_normalised_and_deduped():
    assert worktrees.parse_worktree_repos(["infra/", "./infra", "projects/x"]) == [
        "infra",
        "projects/x",
    ]


@pytest.mark.parametrize("bad", ["/etc", "../escape", "infra/../.."])
def test_entries_that_escape_the_mount_are_rejected(bad):
    with pytest.raises(ValueError):
        worktrees.parse_worktree_repos([bad])


def test_a_bare_string_is_not_a_repo_list():
    with pytest.raises(ValueError):
        worktrees.parse_worktree_repos("infra")


def test_branch_is_per_room():
    assert worktrees.branch_for("mangus-test-e58cde") == (
        "retinue/room/mangus-test-e58cde"
    )
    assert worktrees.branch_for("a b/c") == "retinue/room/a-b-c"


def test_root_is_deterministic_across_call_sites(monkeypatch):
    # The mount list and the container key are built from different call
    # sites; if the root disagreed between them every turn would churn a new
    # container.
    monkeypatch.delenv(worktrees.WORKTREE_ROOT_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", "/tmp/home-a")
    assert worktrees.resolve_worktree_root() == "/tmp/home-a/worktrees"


# ── mounting ─────────────────────────────────────────────────────────────


def test_worktree_is_layered_over_its_place_in_the_workspace(ide_root):
    volumes = ide._room_overlay_volumes(_room("r1", ide_root, ["infra"]))
    assert volumes[0] == f"{ide_root}:/workspace:rw"
    # After the tree mount, or the nested bind loses.
    assert volumes[1].endswith(":/workspace/infra:rw")
    assert "/wt/r1/infra:" in volumes[1]


def test_undeclared_room_mounts_exactly_as_before(ide_root):
    volumes = ide._room_overlay_volumes(_room("r1", ide_root, []))
    assert volumes == [f"{ide_root}:/workspace:rw"]


def test_two_rooms_get_different_container_keys(ide_root):
    a = ide.container_key_for_room(_room("r1", ide_root, ["infra"]))
    b = ide.container_key_for_room(_room("r2", ide_root, ["infra"]))
    assert a != b


def test_sandbox_room_cannot_carry_worktrees(ide_root):
    room = _room("r1", ide_root, ["infra"])
    ide.apply_workspace_fields(room, workspace="sandbox")
    assert room.worktree_repos == []
    assert ide._room_overlay_volumes(room) == []


# ── creation ─────────────────────────────────────────────────────────────


def test_worktree_is_created_on_its_own_branch(ide_root):
    root = worktrees.resolve_worktree_root()
    path = worktrees.ensure_worktree(str(ide_root), "infra", "r1", root)
    assert os.path.isdir(path)
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "retinue/room/r1"
    assert (
        _git(str(ide_root / "infra"), "rev-parse", "--abbrev-ref", "HEAD") == "main"
    )


def test_ensure_is_idempotent(ide_root):
    root = worktrees.resolve_worktree_root()
    first = worktrees.ensure_worktree(str(ide_root), "infra", "r1", root)
    (os.path.join(first, "scratch.txt"))
    open(os.path.join(first, "scratch.txt"), "w").write("x")
    second = worktrees.ensure_worktree(str(ide_root), "infra", "r1", root)
    assert first == second
    # Reusing must not blow away work in progress.
    assert os.path.exists(os.path.join(second, "scratch.txt"))


def test_non_repo_is_refused_with_an_actionable_message(ide_root):
    root = worktrees.resolve_worktree_root()
    with pytest.raises(worktrees.WorktreeError) as err:
        worktrees.ensure_worktree(str(ide_root), "data", "r1", root)
    assert "not a git repository" in str(err.value)


def test_the_ide_root_itself_is_refused(ide_root):
    # The real trap: /home/clay/IDE holds ~25 repos and is not one itself.
    root = worktrees.resolve_worktree_root()
    with pytest.raises(ValueError):
        worktrees.parse_worktree_repos(["."])
    with pytest.raises(worktrees.WorktreeError):
        worktrees.ensure_worktree(str(ide_root), "nope", "r1", root)


# ── the actual defect ────────────────────────────────────────────────────


def test_one_rooms_edits_and_index_are_invisible_to_another(ide_root):
    """The 2026-08-20 collision, reproduced.

    Two rooms, one repo, concurrent work. Without isolation both write the
    same tree and share one git index, so room B can stage and commit room
    A's in-flight file.
    """
    root = worktrees.resolve_worktree_root()
    a = worktrees.ensure_worktree(str(ide_root), "infra", "room-a", root)
    b = worktrees.ensure_worktree(str(ide_root), "infra", "room-b", root)
    assert a != b

    # Room A starts a change and stages it.
    open(os.path.join(a, "a-work.txt"), "w").write("room a\n")
    _git(a, "add", "a-work.txt")

    # Room B is mid-task on its own file.
    open(os.path.join(b, "b-work.txt"), "w").write("room b\n")

    # B cannot see A's file at all, so no `git add -A` can sweep it up.
    assert not os.path.exists(os.path.join(b, "a-work.txt"))
    assert "a-work.txt" not in _git(b, "status", "--porcelain")
    # And A's staged index is its own.
    assert "a-work.txt" in _git(a, "diff", "--cached", "--name-only")
    assert _git(b, "diff", "--cached", "--name-only") == ""

    # A commits; the shared source repo's checked-out tree is untouched.
    _git(a, "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "a")
    assert not os.path.exists(str(ide_root / "infra" / "a-work.txt"))
    assert _git(str(ide_root / "infra"), "status", "--porcelain") == ""


def test_rooms_cannot_take_the_same_branch(ide_root):
    """Git's own interlock — the reason a room worktree is never on main."""
    root = worktrees.resolve_worktree_root()
    worktrees.ensure_worktree(str(ide_root), "infra", "room-a", root)
    repo = str(ide_root / "infra")
    proc = subprocess.run(
        ["git", "-C", repo, "worktree", "add", str(ide_root / "dup"), "retinue/room/room-a"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
