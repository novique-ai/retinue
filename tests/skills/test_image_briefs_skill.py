"""Invariant tests for the bundled image-briefs skill.

Covers skills/creative/image-briefs — prose that teaches an agent how to turn
a conversational request into one ``image_generate`` call.

The skill ships no scripts, so there is no behaviour to exercise. What CAN
rot, silently, is the binding between the prose and the tool it describes: if
``image_generate`` renames a parameter or changes its aspect-ratio enum, the
skill keeps confidently teaching the old one and nothing fails. These tests
pin that binding, plus the section order the authoring standards require.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SKILL = REPO / "skills" / "creative" / "image-briefs" / "SKILL.md"


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_exists():
    assert SKILL.is_file(), f"missing skill file: {SKILL}"


# ---------------------------------------------------------------------------
# The prose/tool binding — the failure mode this file exists for
# ---------------------------------------------------------------------------


def test_taught_parameters_exist_in_the_tool_schema(body):
    """Every parameter the skill names must be a real image_generate param.

    Guards the silent-staleness case: a renamed parameter leaves the skill
    teaching a name the tool will reject, with no other test failing.
    """
    from tools.image_generation_tool import IMAGE_GENERATE_SCHEMA

    real = set(IMAGE_GENERATE_SCHEMA["parameters"]["properties"])
    taught = {"prompt", "aspect_ratio", "upscale"}

    missing = sorted(taught - real)
    assert not missing, (
        f"image-briefs teaches parameters image_generate no longer has: {missing}. "
        f"Update the skill (and any table referencing them)."
    )


def test_taught_aspect_ratios_match_the_enum(body):
    """The Quick Reference table must not name a ratio the tool rejects."""
    from tools.image_generation_tool import VALID_ASPECT_RATIOS

    for ratio in VALID_ASPECT_RATIOS:
        assert f"`{ratio}`" in body, (
            f"aspect ratio {ratio!r} is valid but the skill never mentions it"
        )

    # And the converse: nothing presented as a VALUE may be outside the enum.
    # Only body rows count — the header cells name the parameters themselves.
    table = body.split("## Quick Reference", 1)[-1]
    table = re.split(r"^## Procedure\s*$", table, maxsplit=1, flags=re.MULTILINE)[0]

    body_rows: list[str] = []
    seen_separator = False
    for line in table.splitlines():
        stripped = line.strip()
        if re.match(r"^\|[\s|:-]+\|$", stripped):  # the |---|---| header rule
            seen_separator = True
            continue
        if not stripped.startswith("|"):
            seen_separator = False
            continue
        if seen_separator:
            body_rows.append(stripped)

    assert body_rows, "Quick Reference tables have no parsable body rows"

    quoted = {q for row in body_rows for q in re.findall(r"`([a-z_]+)`", row)}
    allowed = set(VALID_ASPECT_RATIOS) | {"true"}  # `true` is the upscale value
    stray = sorted(quoted - allowed)
    assert not stray, f"skill presents non-enum aspect_ratio values: {stray}"


def test_upscale_is_described_as_optional(body):
    """`upscale` has no default in the schema — omitting it is the fast path.

    If the skill ever started implying it is required, every draft request
    would pay the hi-res cost.
    """
    from tools.image_generation_tool import IMAGE_GENERATE_SCHEMA

    props = IMAGE_GENERATE_SCHEMA["parameters"]["properties"]
    required = IMAGE_GENERATE_SCHEMA["parameters"].get("required") or []

    assert "upscale" in props
    assert "upscale" not in required, (
        "upscale became required in the schema — the skill's 'omit it' guidance is now wrong"
    )
    assert "omit" in body.lower()


def test_only_prompt_is_required(body):
    """The skill tells the agent to always send a prompt; keep that true."""
    from tools.image_generation_tool import IMAGE_GENERATE_SCHEMA

    assert IMAGE_GENERATE_SCHEMA["parameters"]["required"] == ["prompt"]


# ---------------------------------------------------------------------------
# Authoring standards that the mechanical sweep does not cover
# ---------------------------------------------------------------------------

_REQUIRED_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]


def test_modern_section_order(body):
    """AGENTS.md standard 5 — sections present and in the prescribed order."""
    positions = []
    for section in _REQUIRED_SECTIONS:
        # Line-anchored: the prose cross-references section names inline, and
        # a bare find() would match the reference instead of the heading.
        match = re.search(rf"^{re.escape(section)}\s*$", body, re.MULTILINE)
        assert match is not None, f"missing required section: {section}"
        positions.append((section, match.start()))

    ordered = sorted(positions, key=lambda pair: pair[1])
    assert [s for s, _ in ordered] == _REQUIRED_SECTIONS, (
        f"sections out of order: {[s for s, _ in ordered]}"
    )


def test_does_not_headline_shell_utilities(body):
    """AGENTS.md standard 2 — point at native tools, not wrapped shell verbs."""
    banned = re.findall(r"`(grep|cat|head|tail|sed|awk|find|ls)`", body)
    assert not banned, f"skill names wrapped shell utilities: {sorted(set(banned))}"


def test_stays_within_the_simple_skill_length_target(body):
    """AGENTS.md standard 5 — ~100 lines for a simple skill; 200 is the ceiling."""
    lines = body.splitlines()
    assert len(lines) <= 200, f"{len(lines)} lines — trim toward the ~100-line target"
