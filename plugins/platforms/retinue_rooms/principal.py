"""Workspace principal — the human in the room, not a hired agent.

Stored at ``$HERMES_HOME/retinue_principal.json``. Agents read the name
and about-you from the room briefing. The human does not take turns.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

FILENAME = "retinue_principal.json"
DEFAULT_NAME = "You"
_MAX_NAME = 80
_MAX_ABOUT = 800


def _path(home_dir: str) -> str:
    return os.path.join(home_dir, FILENAME)


def empty() -> Dict[str, Any]:
    return {"display_name": DEFAULT_NAME, "about": ""}


def load(home_dir: str) -> Dict[str, Any]:
    try:
        data = json.loads(open(_path(home_dir), encoding="utf-8").read())
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    name = str(data.get("display_name") or "").strip()[:_MAX_NAME] or DEFAULT_NAME
    about = str(data.get("about") or "").strip()[:_MAX_ABOUT]
    return {"display_name": name, "about": about}


def save(home_dir: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("display_name") or body.get("name") or "").strip()
    if not name:
        raise ValueError("display name is required")
    if len(name) > _MAX_NAME:
        raise ValueError("display name is too long")
    about = str(body.get("about") or "").strip()
    if len(about) > _MAX_ABOUT:
        raise ValueError("about is too long")
    payload = {"display_name": name[:_MAX_NAME], "about": about[:_MAX_ABOUT]}
    dest = _path(home_dir)
    tmp = dest + ".tmp"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)
    return payload


def speaker_name(home_dir: str, raw: str = "") -> str:
    """Name to stamp on a user line. Empty / You / User → principal."""
    given = (raw or "").strip()
    principal = load(home_dir)
    name = str(principal.get("display_name") or DEFAULT_NAME)
    if not given or given in {DEFAULT_NAME, "User"}:
        return name
    return given
