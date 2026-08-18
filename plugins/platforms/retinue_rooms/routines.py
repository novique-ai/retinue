"""Learn-by-demonstration routines: save a room's user turns, replay later.

A routine is the user's half of a demonstration — the prompts that produced
a useful agent run. Replaying posts those prompts into a room in order and
waits for each cycle to finish (the agents redo the work). A schema-2 routine
may also link to a profile-scoped Hermes cron job and generated skill draft.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from .engine import KIND_USER, RoomMessage

_SLUG_RE = re.compile(r"[^a-z0-9]+")
ROUTINES_DIRNAME = "routines"


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return slug[:48]


def _dir(home_dir: str) -> str:
    return os.path.join(home_dir, "retinue_rooms", ROUTINES_DIRNAME)


def _path(home_dir: str, slug: str) -> str:
    return os.path.join(_dir(home_dir), f"{slug}.json")


def user_prompts_from_messages(
    messages: List[RoomMessage],
    since: int = 0,
    until: Optional[int] = None,
) -> List[str]:
    """Extract the demonstration: user texts in seq order, optionally bounded."""
    prompts: List[str] = []
    for msg in messages:
        if msg.kind != KIND_USER:
            continue
        if msg.seq <= since:
            continue
        if until is not None and msg.seq > until:
            continue
        text = (msg.text or "").strip()
        if text:
            prompts.append(text)
    return prompts


def save_routine(
    home_dir: str,
    name: str,
    messages: List[str],
    source_room: str = "",
    *,
    owner: str = "",
    skill: str = "",
    expected_output: str = "",
    job_id: str | None = None,
) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("routine name is required")
    prompts = [m.strip() for m in messages if (m or "").strip()]
    if not prompts:
        raise ValueError("a routine needs at least one user prompt")
    slug = slugify(name)
    if not slug:
        raise ValueError(f"cannot derive a routine id from {name!r}")
    os.makedirs(_dir(home_dir), exist_ok=True)
    path = _path(home_dir, slug)
    if os.path.isfile(path):
        raise FileExistsError(slug)
    meta = {
        "name": name,
        "slug": slug,
        "source_room": source_room,
        "messages": prompts,
        "steps": list(prompts),
        "owner": str(owner or ""),
        "skill": str(skill or ""),
        "expected_output": str(expected_output or ""),
        "job_id": job_id,
        "schema": 2,
        "created_at": time.time(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)
    return meta


def _normalise(meta: Dict[str, Any]) -> Dict[str, Any]:
    record = dict(meta)
    messages = record.get("messages")
    steps = record.get("steps")
    record["messages"] = list(messages) if isinstance(messages, list) else []
    record["steps"] = list(steps) if isinstance(steps, list) else list(record["messages"])
    record.setdefault("owner", "")
    record.setdefault("skill", "")
    record.setdefault("expected_output", "")
    record.setdefault("job_id", None)
    record.setdefault("schema", 1)
    return record


def update_routine(home_dir: str, slug: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    current = get_routine(home_dir, slug)
    if current is None:
        raise KeyError(slug)
    updated = {**current, **dict(updates)}
    path = _path(home_dir, slug)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=2)
    os.replace(tmp, path)
    return _normalise(updated)


def list_routines(home_dir: str) -> List[Dict[str, Any]]:
    try:
        names = sorted(os.listdir(_dir(home_dir)))
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_dir(home_dir), fn), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("slug"):
            out.append(_normalise(meta))
    return out


def get_routine(home_dir: str, slug: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(home_dir, slug), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return _normalise(meta) if isinstance(meta, dict) else None


def delete_routine(home_dir: str, slug: str) -> bool:
    try:
        os.remove(_path(home_dir, slug))
        return True
    except FileNotFoundError:
        return False
