"""The ide-room mount-root search fence (novique-ai/retinue#165).

An ide room bind-mounts the operator's ENTIRE IDE at /workspace. One
recursive grep of that root takes minutes under the exclusive room lock and
stuffs ~13k tokens of dump into the member's session — the fuel for the
2026-08-20 session-poisoning failure (#164). The fence refuses the explicit
mount-root scans observed live; it is a product fence against an honest but
wasteful pattern, not a security boundary (a determined `cd /workspace &&
grep -r .` still passes — the briefing owns that education).
"""

from __future__ import annotations

from tools import workspace_context

from . import engine, ide
from .engine import Room


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Fence", members=["dev"], lead="dev")
    defaults.update(kwargs)
    return Room(**defaults)


def _ide_room(tmp_path) -> Room:
    return _room(workspace="ide", ide_path=str(tmp_path))


# --- detector ---------------------------------------------------------------


def test_detector_refuses_the_observed_root_scans():
    for cmd in (
        "grep -rn 'timer' /workspace",
        "grep -R pattern /workspace/",
        "grep --recursive pattern /workspace",
        "rg pattern /workspace",
        "find /workspace -name '*.py'",
        "cd /tmp && grep -rn x /workspace",
        "echo hi; rg foo /workspace",
    ):
        assert workspace_context.scans_ide_root(cmd), cmd


def test_detector_allows_scoped_and_benign_commands():
    for cmd in (
        "grep -rn 'timer' /workspace/infra",
        "grep -rn 'timer' /workspace/infra/scripts",
        "rg pattern /workspace/projects/retinue",
        "find /workspace/infra -name '*.py'",
        "ls /workspace",
        "ls -la /workspace/",
        "cat /workspace/infra/README.md",
        "grep -n plain /workspace/infra/file.py",
        "git -C /workspace/infra status",
        "du -sh /workspace",
    ):
        assert not workspace_context.scans_ide_root(cmd), cmd


def test_detector_survives_unparseable_shell():
    assert workspace_context.scans_ide_root("grep -rn 'unterminated /workspace") in (
        True,
        False,
    )


# --- overlay gating ---------------------------------------------------------


def test_refusal_only_fires_inside_an_ide_room_overlay(tmp_path):
    cmd = "grep -rn pattern /workspace"
    # No overlay: CLI / desktop / sandbox-room behavior is unchanged.
    assert workspace_context.ide_root_scan_refusal(cmd) is None
    with workspace_context.workspace(ide.overlay_env(_ide_room(tmp_path))):
        message = workspace_context.ide_root_scan_refusal(cmd)
        assert message is not None
        assert "/workspace" in message
        assert "entire" in message.lower()
    assert workspace_context.ide_root_scan_refusal(cmd) is None


def test_sandbox_room_overlay_does_not_fence(tmp_path):
    with workspace_context.workspace(ide.overlay_env(_room(workspace="sandbox"))):
        assert workspace_context.ide_root_scan_refusal("rg x /workspace") is None


def test_ide_overlay_carries_the_flag(tmp_path):
    env = ide.overlay_env(_ide_room(tmp_path))
    assert env.get(workspace_context.IDE_WORKSPACE_FLAG) == "1"
    assert workspace_context.IDE_WORKSPACE_FLAG not in ide.overlay_env(
        _room(workspace="sandbox")
    )


# --- terminal wire-in (source guard, carried-patch style) -------------------


def test_terminal_tool_wires_the_fence_before_execution():
    """The refusal must gate the terminal execute path — including force=True.

    Source-level drift guard (test_carried_patches.py precedent): an upstream
    sync that drops the call silently reopens #165.
    """
    import os

    repo = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    with open(os.path.join(repo, "tools", "terminal_tool.py"), encoding="utf-8") as f:
        src = f.read()
    assert "ide_root_scan_refusal" in src


# --- briefing ---------------------------------------------------------------


def test_ide_briefing_teaches_the_workspace_scope(tmp_path):
    text = engine.room_briefing(_ide_room(tmp_path), "dev", ["Mark"])
    assert "ENTIRE IDE" in text
    assert "refused" in text


def test_sandbox_briefing_does_not_mention_the_fence():
    text = engine.room_briefing(_room(workspace="sandbox"), "dev", ["Mark"])
    assert "ENTIRE IDE" not in text
