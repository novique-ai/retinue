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
from typing import Any, Dict, List

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


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _normalize_item(raw: Any) -> Dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    text = _clip(raw.get("text"), _MAX_TEXT)
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


def briefing_lines(plan: Dict[str, Any] | None, *, is_lead: bool) -> List[str]:
    if not plan:
        return []
    items = plan.get("items") or []
    summary = str(plan.get("summary") or "").strip()
    title = str(plan.get("title") or "").strip()
    if not items and not summary and not title:
        if is_lead:
            return [
                "You are this room's lead. Keep a short itinerary of the work "
                "current (the user also edits it in the Itinerary pane). "
                "It is empty right now."
            ]
        return []
    lines = ["Room itinerary (living outline, not the transcript):"]
    if title:
        lines.append(f"Title: {title}")
    if summary:
        lines.append(f"Where we are: {summary}")
    for item in items:
        lines.append(f"- [{item.get('status') or 'todo'}] {item.get('text')}")
    if is_lead:
        lines.append(
            "You own this itinerary. Keep it current as work moves — "
            "the user also edits it in the right-hand Itinerary pane."
        )
    return lines
