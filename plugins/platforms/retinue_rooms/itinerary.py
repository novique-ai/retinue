"""Per-room itinerary — a short living outline, not a second transcript.

Persisted next to the room meta as ``<room_id>.itinerary.json``. The lead
is expected to keep it current; the user can also edit it in the right pane.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

STATUSES = ("todo", "doing", "done")
_MAX_ITEMS = 40
_MAX_TEXT = 240
_MAX_SUMMARY = 500
_MAX_TITLE = 80
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


def empty(room_id: str) -> Dict[str, Any]:
    return {
        "room_id": room_id,
        "title": "",
        "summary": "",
        "items": [],
        "updated_at": 0.0,
        "updated_by": "",
    }


def _path(home_dir: str, room_id: str) -> str:
    return os.path.join(home_dir, "retinue_rooms", f"{room_id}.itinerary.json")


def _clip(value: Any, limit: int, *, strip: bool = False) -> str:
    """Truncate to *limit*. Do not strip trailing spaces unless asked —
    the itinerary pane saves on every keystroke, so stripping a trailing
    space makes it impossible to type the next word."""
    text = str(value or "")
    if strip:
        text = text.strip()
    return text[:limit]


def _normalize_item(raw: Any) -> Dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    text = _clip(raw.get("text"), _MAX_TEXT, strip=True)
    if not text:
        return None
    status = str(raw.get("status") or "todo").strip().lower()
    if status not in STATUSES:
        status = "todo"
    item_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(item_id):
        item_id = uuid.uuid4().hex[:10]
    return {"id": item_id, "text": text, "status": status}


def normalize(room_id: str, body: Dict[str, Any] | None, *, updated_by: str = "") -> Dict[str, Any]:
    src = body if isinstance(body, dict) else {}
    items: List[Dict[str, str]] = []
    for raw in src.get("items") or []:
        item = _normalize_item(raw)
        if item:
            items.append(item)
        if len(items) >= _MAX_ITEMS:
            break
    return {
        "room_id": room_id,
        "title": _clip(src.get("title"), _MAX_TITLE),
        "summary": _clip(src.get("summary"), _MAX_SUMMARY),
        "items": items,
        "updated_at": time.time(),
        "updated_by": _clip(updated_by, 64),
    }


def load(home_dir: str, room_id: str) -> Dict[str, Any]:
    try:
        with open(_path(home_dir, room_id), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty(room_id)
    if not isinstance(data, dict):
        return empty(room_id)
    items = []
    for raw in data.get("items") or []:
        item = _normalize_item(raw)
        if item:
            items.append(item)
    return {
        "room_id": room_id,
        "title": _clip(data.get("title"), _MAX_TITLE),
        "summary": _clip(data.get("summary"), _MAX_SUMMARY),
        "items": items,
        "updated_at": float(data.get("updated_at") or 0.0),
        "updated_by": _clip(data.get("updated_by"), 64),
    }


def save(
    home_dir: str,
    room_id: str,
    body: Dict[str, Any] | None,
    *,
    updated_by: str = "",
) -> Dict[str, Any]:
    meta = normalize(room_id, body, updated_by=updated_by)
    path = _path(home_dir, room_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return meta


_FENCE_RE = re.compile(r"```(?:itinerary|itin)\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_ITEM_RE = re.compile(
    r"^[-*]\s*(?:\[(?P<mark>done|doing|todo|x|~|\s*)\]\s*)?(?P<text>.+)$",
    re.IGNORECASE,
)


def parse_fence(text: str) -> Optional[Dict[str, Any]]:
    """Pull a lead-authored `` ```itinerary `` block out of a reply."""
    match = _FENCE_RE.search(text or "")
    if not match:
        return None
    title = ""
    summary = ""
    items: List[Dict[str, str]] = []
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("title:"):
            title = line.split(":", 1)[1].strip()
            continue
        if low.startswith("where we are:") or low.startswith("where:"):
            summary = line.split(":", 1)[1].strip()
            continue
        item_match = _ITEM_RE.match(line)
        if not item_match:
            continue
        mark = (item_match.group("mark") or "").strip().lower()
        if mark in ("done", "x"):
            status = "done"
        elif mark in ("doing", "~"):
            status = "doing"
        else:
            status = "todo"
        body = (item_match.group("text") or "").strip()
        if body:
            items.append({"text": body, "status": status})
    if not title and not summary and not items:
        return None
    return {"title": title, "summary": summary, "items": items}


def briefing_lines(plan: Dict[str, Any] | None, *, is_lead: bool) -> List[str]:
    if is_lead:
        lines = [
            "You own this room's itinerary. You write it — do not wait for the "
            "user to open a pane. On the first turn of a project, and whenever "
            "the plan changes, include a fenced block in your reply:",
            "```itinerary",
            "title: short name",
            "where: one or two sentences on current progress",
            "- [doing] the active step",
            "- [todo] next step",
            "- [done] finished step",
            "```",
        ]
    else:
        lines = []
    if not plan:
        return lines
    items = plan.get("items") or []
    summary = str(plan.get("summary") or "").strip()
    title = str(plan.get("title") or "").strip()
    if not items and not summary and not title:
        return lines
    lines.append("Current itinerary (update the fence if this is wrong):")
    if title:
        lines.append(f"Title: {title}")
    if summary:
        lines.append(f"Where we are: {summary}")
    for item in items:
        lines.append(f"- [{item.get('status') or 'todo'}] {item.get('text')}")
    return lines
