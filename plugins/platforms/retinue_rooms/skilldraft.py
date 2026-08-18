"""Deterministic per-retainer skill drafts generated from room routines."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from . import cronjobs

DESCRIPTION = "Replays a routine captured from a Retinue room."


def _inline(value: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(text.split())


def skill_dir(home_dir: str, owner: str, slug: str) -> str:
    value = str(slug or "").strip()
    if (
        not value
        or ".." in value
        or os.path.isabs(value)
        or value != os.path.basename(value)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value)
    ):
        raise ValueError("invalid skill slug")
    owner_root = cronjobs.owner_home(home_dir, owner)
    raw_skills_root = os.path.join(owner_root, "skills")
    raw_target = os.path.join(raw_skills_root, value)
    if os.path.islink(raw_skills_root) or os.path.islink(raw_target):
        raise ValueError("skill path may not use symlinks")
    skills_root = os.path.realpath(raw_skills_root)
    target = os.path.realpath(raw_target)
    if os.path.commonpath([skills_root, target]) != skills_root:
        raise ValueError("skill path escapes the owning profile")
    return target


def render_skill_draft(
    *,
    slug: str,
    name: str,
    steps: Sequence[str],
    expected_output: str,
    source_room: str,
) -> str:
    title = _inline(name) or _inline(slug)
    safe_slug = _inline(slug)
    room = _inline(source_room)
    procedure = "\n".join(
        f"{index}. {str(step).strip()}" for index, step in enumerate(steps, 1)
    )
    verification = str(expected_output or "").strip() or (
        "Not captured — describe the expected result here."
    )
    return f"""---
name: {safe_slug}
description: {DESCRIPTION}
version: 1.0.0
author: Retinue
license: MIT
metadata:
  hermes:
    tags:
      - retinue
      - routine
    category: productivity
---

# {title} Skill

Replays the demonstration captured in Retinue room `{room}`. It performs the
recorded steps in order and reports the result; it does not decide when to run —
the routine's cron job owns the schedule.

## When to Use

Use when the routine `{safe_slug}` is due, either from its cron job or when a
room member asks for it by name.

## Prerequisites

- The room `{room}` still exists in this workspace.
- Any credentials the recorded steps need are already present in this profile.

## How to Run

Follow `## Procedure` in order. Report the outcome as your final response; the
cron job's `deliver=origin` puts it back on the room transcript.

## Quick Reference

| Field | Value |
|---|---|
| Routine | `{safe_slug}` |
| Source room | `{room}` |
| Steps | `{len(steps)}` |

## Procedure

{procedure}

## Pitfalls

- This is a **draft** generated from a demonstration. Edit it — the steps are
  the user's raw prompts, not polished instructions.
- Editing the routine's name or schedule later does NOT rewrite this file.

## Verification

{verification}
"""


def write_skill_draft(
    home_dir: str,
    owner: str,
    *,
    slug: str,
    name: str,
    steps: Sequence[str],
    expected_output: str,
    source_room: str,
) -> dict:
    target = skill_dir(home_dir, owner, slug)
    skills_root = os.path.dirname(target)
    if os.path.islink(skills_root) or os.path.islink(target):
        raise ValueError("skill path may not use symlinks")
    if os.path.exists(os.path.join(target, "SKILL.md")):
        raise FileExistsError(slug)
    os.makedirs(target, exist_ok=False)
    path = os.path.join(target, "SKILL.md")
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(
                render_skill_draft(
                    slug=slug,
                    name=name,
                    steps=steps,
                    expected_output=expected_output,
                    source_room=source_room,
                )
            )
    except Exception:
        try:
            os.rmdir(target)
        except OSError:
            pass
        raise
    return {"skill": slug, "path": path, "owner": owner}
