"""The hire flow — turn a three-field brief into a Hermes profile.

Grok Bot's creation UX, the part worth copying: an agent is born from a
name, one primary job, and a description of how it should work. Here that
becomes a profile directory (persona SOUL.md + model config), which the
multiplexing gateway serves as a room member.

Pure filesystem logic — unit-testable without a gateway (see test_hire.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RESERVED = {"default", "profiles"}

AGENT_META_FILENAME = "retinue-agent.json"


def slugify_name(display_name: str) -> str:
    """Mention token for a display name: lowercase, alnum + hyphens."""
    slug = _SLUG_RE.sub("-", (display_name or "").lower()).strip("-")
    return slug[:32]


def soul_template(display_name: str, job: str, how: str) -> str:
    parts = [f"You are {display_name}.", "", f"Your job: {job.strip()}"]
    how = (how or "").strip()
    if how:
        parts += ["", "How you work:", how]
    parts += [
        "",
        "Stay in character and within your job. If a request belongs to a "
        "teammate's specialty, say so briefly instead of guessing.",
    ]
    return "\n".join(parts) + "\n"


def _extract_model_block(root_config_path: str) -> str:
    """Copy the root config's ``model:`` block verbatim so a new agent uses
    the workspace's default provider. Text-level extraction on purpose: it
    preserves comments and never needs a YAML dependency at import time."""
    try:
        with open(root_config_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    block: List[str] = []
    inside = False
    for line in lines:
        if line.startswith("model:"):
            inside = True
            block.append(line)
            continue
        if inside:
            if line.strip() and not line.startswith((" ", "\t", "#")):
                break
            block.append(line)
    if block:
        return "".join(block).rstrip() + "\n"
    return "model:\n  default: claude-haiku-4-5\n  provider: anthropic\n"


def scaffold_profile(home_dir: str, display_name: str, job: str, how: str) -> Dict[str, Any]:
    """Create ``profiles/<slug>/`` under *home_dir*. Raises ValueError on bad
    input, FileExistsError if the profile already exists."""
    display_name = (display_name or "").strip()
    job = (job or "").strip()
    if not display_name:
        raise ValueError("agent name is required")
    if not job:
        raise ValueError("primary job is required")
    slug = slugify_name(display_name)
    if not slug or slug in _RESERVED:
        raise ValueError(f"cannot derive a usable profile name from {display_name!r}")

    profile_dir = os.path.join(home_dir, "profiles", slug)
    if os.path.isdir(profile_dir):
        raise FileExistsError(slug)
    os.makedirs(profile_dir)

    with open(os.path.join(profile_dir, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(soul_template(display_name, job, how))

    model_block = _extract_model_block(os.path.join(home_dir, "config.yaml"))
    with open(os.path.join(profile_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(model_block + "agent:\n  tool_choice: auto\n")

    # Credentials: a profile's secret scope reads its own .env; seed it from
    # the workspace root so a freshly hired agent can reach the same provider.
    root_env = os.path.join(home_dir, ".env")
    if os.path.isfile(root_env):
        shutil.copy(root_env, os.path.join(profile_dir, ".env"))
        os.chmod(os.path.join(profile_dir, ".env"), 0o600)

    meta = {
        "display_name": display_name,
        "slug": slug,
        "job": job,
        "how": (how or "").strip(),
        "created_at": time.time(),
    }
    with open(os.path.join(profile_dir, AGENT_META_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


def list_agents(home_dir: str) -> List[Dict[str, Any]]:
    """Roster of hire-able room members: every profile directory, with hire
    metadata when present (hand-made profiles get a slug-only entry)."""
    profiles_dir = os.path.join(home_dir, "profiles")
    agents: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        names = []
    for name in names:
        pdir = os.path.join(profiles_dir, name)
        if not os.path.isdir(pdir):
            continue
        meta: Optional[Dict[str, Any]] = None
        try:
            with open(os.path.join(pdir, AGENT_META_FILENAME), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None
        if meta is None:
            meta = {"display_name": name, "slug": name, "job": "", "how": ""}
        meta["has_soul"] = os.path.isfile(os.path.join(pdir, "SOUL.md"))
        agents.append(meta)
    return agents
