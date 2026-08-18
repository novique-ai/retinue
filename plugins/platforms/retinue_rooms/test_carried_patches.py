"""Drift guards for the fork's carried patches (retinue/FORK-POLICY.md).

An upstream sync that clobbers a carried patch reintroduces the original
defect silently — these tests turn that into a red build instead. Each is a
source-level assertion on the patched region (the hermes-claude-token-sync
precedent), demonstrated to fail against the unpatched upstream text.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_skills_guidance_avoids_the_content_filter_wording():
    """#82154: the stock first sentence trips Anthropic's content filter.

    No longer a carried patch — upstream adopted the reword and documented it
    with a NOTE citing the same issue, so we dropped ours at the 2026-08-16
    sync. This stays as a regression guard, because the failure it prevents is
    remote, expensive and misleading: a billing-shaped HTTP 400 ("out of extra
    usage") on subscription-OAuth tokens, which sends users to buy quota they
    do not need.

    Asserts on the CONSTANT, not on the file. The previous version scanned
    prompt_builder.py's whole source for the bad phrasing, which broke the
    moment upstream's explanatory comment quoted that phrasing to say what NOT
    to use. A guard that fires on a comment is a guard that gets muted.
    """
    from agent.prompt_builder import SKILLS_GUIDANCE

    assert "5+ tool calls" not in SKILLS_GUIDANCE, (
        "the content-filter wording is back in SKILLS_GUIDANCE — this causes "
        "billing-shaped 400s on subscription-OAuth tokens. See "
        "NousResearch/hermes-agent#82154 and retinue/FORK-POLICY.md."
    )
    assert "record it with skill_manage" in SKILLS_GUIDANCE


def test_shared_container_key_patch_present():
    """#84671: workspace-computer container identity override."""
    src = _read("tools/environments/docker.py")
    # Reads through workspace_context, not os.getenv: the key is carried
    # per-context so concurrent rooms do not race process env (#67).
    marker = "workspace_context.shared_container_key()"
    assert marker in src, (
        "shared-container-key carried patch was clobbered (likely by an "
        "upstream sync) — room members would silently fall back to "
        "one container per profile. Reapply per retinue/FORK-POLICY.md."
    )
    # The override must feed the same variable used for BOTH create-labels
    # and the reuse probe — guard the exact composition.
    assert "_sanitize_label_value(_shared_key or _get_active_profile_name())" in src


def test_shared_container_key_is_the_env_cache_key_patch_present():
    """#84671 / retinue#16: container identity and env cache must agree.

    Identity is keyed by TERMINAL_DOCKER_SHARED_CONTAINER_KEY at creation
    time; if the cache still collapses to "default", the environment built
    for the first workspace is reused for every later one and a sandbox
    room's container serves an IDE room's turn.
    """
    src = _read("tools/terminal_tool.py")
    assert "workspace_context.shared_container_key()" in src, (
        "shared-container-key cache patch was clobbered (likely by an "
        "upstream sync) — room turns would collapse back onto one "
        '"default" environment and cross the sandbox/IDE boundary. '
        "Reapply per retinue/FORK-POLICY.md."
    )
    # Must sit in the cache-key resolver, after the isolation branches, so
    # per-session isolation still wins and the CLI still lands on "default".
    resolver = src.split("def _resolve_container_task_id(")[1].split("\ndef ")[0]
    assert "_shared_key" in resolver
    assert resolver.index("_docker_session_isolation_enabled") < resolver.index(
        "_shared_key = workspace_context.shared_container_key()"
    )
    assert resolver.rstrip().endswith('return "default"')


def test_workspace_values_are_carried_per_context_not_in_process_env():
    """#67: the carrier is a ContextVar, and the process-wide lock is gone.

    These two facts are one fact. The lock existed only because the container
    key travelled through os.environ, so a revert of either half silently
    reintroduces the other's problem: put the key back in process env and
    concurrent rooms cross containers; keep the ContextVar but re-add the lock
    and every room blocks on the slowest turn again.
    """
    ide_src = _read("plugins/platforms/retinue_rooms/ide.py")
    assert "workspace_context.workspace(overlay)" in ide_src, (
        "the per-room workspace overlay is no longer bound to a ContextVar — "
        "concurrent room cycles will race each other's mounts. "
        "Reapply per retinue/FORK-POLICY.md."
    )
    assert "os.environ.update(overlay)" not in ide_src, (
        "the room overlay is being written into process env again — that is "
        "the carrier #67 removed."
    )

    adapter_src = _read("plugins/platforms/retinue_rooms/adapter.py")
    assert "_workspace_env_lock" not in adapter_src, (
        "the process-wide workspace lock is back — one room turn again blocks "
        "every other room for the length of the turn (up to the local-model "
        "timeout). See novique-ai/retinue#67."
    )


def test_media_path_translation_reads_the_room_volumes():
    """#67: media paths must resolve against the ROOM's mounts.

    ``_translate_docker_container_media_path`` maps a container path back to a
    host path by longest-prefix match over the configured volumes. Volumes are
    per-room and no longer in process env, so reading os.getenv there would
    silently fail to resolve an IDE room's /workspace file.
    """
    src = _read("gateway/platforms/base.py")
    assert 'workspace_context.getenv("TERMINAL_DOCKER_VOLUMES"' in src, (
        "media path translation reverted to process env — an IDE room's "
        "/workspace attachments will stop resolving to their host path. "
        "Reapply per retinue/FORK-POLICY.md."
    )


def test_cron_delivery_live_transport_patch_present():
    """A resolved live transport without a config block remains deliverable."""
    src = _read("cron/scheduler.py")
    region = src.split("def _deliver_result(", 1)[1].split("\ndef ", 1)[0]
    gate = region[: region.index("elif not pconfig or not pconfig.enabled:")]
    branch = gate[gate.rindex("if transport is not None") :]
    assert branch.startswith("if transport is not None:"), (
        "the deliver=origin carried patch was clobbered: the branch guarding "
        "'elif not pconfig or not pconfig.enabled' is back to the upstream "
        "relay-only form. Reapply it per retinue/FORK-POLICY.md — "
        "novique-ai/retinue#112 and NousResearch/hermes-agent#89302."
    )
    assert "PlatformConfig(enabled=True)" in branch


def test_cron_scheduler_patch_is_confined_to_deliver_result():
    """The carried scheduler delta may not grow beyond _deliver_result."""
    try:
        diff = subprocess.run(
            [
                "git",
                "diff",
                "-U0",
                "56516ec7faa075bab1b1c321962bde38ff79f292",
                "--",
                "cron/scheduler.py",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pytest.skip("git is unavailable")
    if not diff.stdout:
        pytest.skip("scheduler has no carried-patch diff")

    lines = _read("cron/scheduler.py").splitlines()
    start = next(i for i, line in enumerate(lines, 1) if line.startswith("def _deliver_result("))
    end = next(
        (
            i
            for i, line in enumerate(lines, 1)
            if i > start and re.match(r"^(def |class |@)", line)
        ),
        len(lines) + 1,
    )
    hunks = [
        (int(match.group(1)), int(match.group(2) or 1))
        for match in re.finditer(
            r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff.stdout, re.MULTILINE
        )
    ]
    outside = [
        (line, count)
        for line, count in hunks
        if line < start or line + max(count, 1) - 1 >= end
    ]
    assert not outside, f"cron scheduler hunks outside _deliver_result: {outside}"
