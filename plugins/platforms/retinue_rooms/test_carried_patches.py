"""Drift guards for the fork's carried patches (retinue/FORK-POLICY.md).

An upstream sync that clobbers a carried patch reintroduces the original
defect silently — these tests turn that into a red build instead. Each is a
source-level assertion on the patched region (the hermes-claude-token-sync
precedent), demonstrated to fail against the unpatched upstream text.
"""

from __future__ import annotations

import os

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_skills_guidance_content_filter_patch_present():
    """#82154: the stock first sentence trips Anthropic's content filter."""
    src = _read("agent/prompt_builder.py")
    assert "5+ tool calls" not in src, (
        "SKILLS_GUIDANCE carried patch was clobbered (likely by an upstream "
        "sync) — the stock sentence causes billing-shaped 400s on "
        "subscription-OAuth tokens. Reapply per retinue/FORK-POLICY.md."
    )
    assert "record it with skill_manage" in src


def test_shared_container_key_patch_present():
    """#84671: workspace-computer container identity override."""
    src = _read("tools/environments/docker.py")
    marker = 'os.getenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY"'
    assert marker in src, (
        "shared-container-key carried patch was clobbered (likely by an "
        "upstream sync) — room members would silently fall back to "
        "one container per profile. Reapply per retinue/FORK-POLICY.md."
    )
    # The override must feed the same variable used for BOTH create-labels
    # and the reuse probe — guard the exact composition.
    assert "_sanitize_label_value(_shared_key or _get_active_profile_name())" in src
