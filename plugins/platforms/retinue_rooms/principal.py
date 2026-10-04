"""Workspace principal — the human in the room, not a hired agent.

Stored at ``$HERMES_HOME/retinue_principal.json``. Agents read the name
and about-you from the room briefing. The human does not take turns.

``trusted_senders`` names speakers (the name stamped on a user line, not
a retainer slug) who may @mention a room member and start that turn
while the room is paused on the principal. The pause stays up. Empty —
the default, and what a save omits — holds every non-principal post.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

FILENAME = "retinue_principal.json"
DEFAULT_NAME = "You"
_MAX_NAME = 80
_MAX_ABOUT = 800
_MAX_TRUSTED_SENDERS = 32
_MAX_SENDER_NAME = 80


def _path(home_dir: str) -> str:
    return os.path.join(home_dir, FILENAME)


def empty() -> Dict[str, Any]:
    return {"display_name": DEFAULT_NAME, "about": "", "trusted_senders": []}


def _clean_trusted_senders(raw: Any, *, strict: bool) -> List[str]:
    """Speaker names, de-duplicated case-insensitively, first spelling kept.

    Save is strict: a wrong type, an over-long name, or more than
    ``_MAX_TRUSTED_SENDERS`` unique names is an error, and nothing is
    written. Load is lenient so a hand-edited file cannot wedge the room;
    junk entries are dropped. ``None`` is an empty list either way.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("trusted senders must be a list of speaker names")
        return []
    seen: set[str] = set()
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            if strict:
                raise ValueError("trusted senders must be speaker names")
            continue
        name = item.strip()
        if not name:
            continue
        if len(name) > _MAX_SENDER_NAME:
            if strict:
                raise ValueError("a trusted sender name is too long")
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    if len(out) > _MAX_TRUSTED_SENDERS:
        if strict:
            raise ValueError("too many trusted senders")
        return out[:_MAX_TRUSTED_SENDERS]
    return out


def load(home_dir: str) -> Dict[str, Any]:
    try:
        data = json.loads(open(_path(home_dir), encoding="utf-8").read())
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    name = str(data.get("display_name") or "").strip()[:_MAX_NAME] or DEFAULT_NAME
    about = str(data.get("about") or "").strip()[:_MAX_ABOUT]
    trusted = _clean_trusted_senders(data.get("trusted_senders"), strict=False)
    return {"display_name": name, "about": about, "trusted_senders": trusted}


def save(home_dir: str, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("display_name") or body.get("name") or "").strip()
    if not name:
        raise ValueError("display name is required")
    if len(name) > _MAX_NAME:
        raise ValueError("display name is too long")
    about = str(body.get("about") or "").strip()
    if len(about) > _MAX_ABOUT:
        raise ValueError("about is too long")
    # Full replace, same as about: a body that omits the list stores [].
    trusted = _clean_trusted_senders(body.get("trusted_senders"), strict=True)
    payload = {
        "display_name": name[:_MAX_NAME],
        "about": about[:_MAX_ABOUT],
        "trusted_senders": trusted,
    }
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
