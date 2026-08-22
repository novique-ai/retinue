"""Projects — group rooms above the sidebar's room list.

Persisted as ``$HERMES_HOME/retinue_projects.json``. This file holds project
identity (id, name, archived) and display order only. Membership is *not*
tracked here: it lives solely on ``room.project_id`` (see engine.Room) so
there is one source of truth instead of two lists that can drift apart when
a room is deleted or moved.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from typing import Any, Dict, Iterable, List, Optional

PROJECTS_FILENAME = "retinue_projects.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_lock = threading.Lock()


def _empty() -> Dict[str, Any]:
    return {"projects": [], "order": []}


def slugify(label: str) -> str:
    return _SLUG_RE.sub("-", (label or "project").lower()).strip("-")[:24] or "project"


def new_project_id(name: str, existing: Optional[Iterable[str]] = None) -> str:
    """Stable id from the name when free; otherwise ``slug-xxxx``."""
    slug = slugify(name)
    taken = {str(x) for x in (existing or []) if x}
    if slug not in taken:
        return slug
    return f"{slug}-{uuid.uuid4().hex[:4]}"


def projects_path(home_dir: str) -> str:
    return os.path.join(home_dir, PROJECTS_FILENAME)


def load(home_dir: str) -> Dict[str, Any]:
    path = projects_path(home_dir)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    return normalize(raw)


def save(home_dir: str, data: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = normalize(data)
    path = projects_path(home_dir)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
        os.replace(tmp, path)
    return cleaned


def normalize(raw: Any) -> Dict[str, Any]:
    """Coerce a client/disk payload into the persisted shape."""
    if not isinstance(raw, dict):
        return _empty()
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("projects") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not pid or not name or pid in by_id:
            continue
        by_id[pid] = {"id": pid, "name": name, "archived": bool(item.get("archived"))}

    order: List[str] = []
    seen: set[str] = set()
    for pid in raw.get("order") or []:
        pid = str(pid or "").strip()
        if pid and pid in by_id and pid not in seen:
            order.append(pid)
            seen.add(pid)
    for pid in by_id:
        if pid not in seen:
            order.append(pid)
            seen.add(pid)
    return {"projects": [by_id[pid] for pid in order], "order": order}


def list_projects(home_dir: str) -> List[Dict[str, Any]]:
    return load(home_dir)["projects"]


def reorder(home_dir: str, order: Iterable[str]) -> Dict[str, Any]:
    """Rewrite display order. Unknown ids are dropped; missing ids append.

    Unfiled is not a project and must never appear in ``order``.
    """
    data = load(home_dir)
    data["order"] = [str(pid) for pid in order]
    return save(home_dir, data)


def create_project(home_dir: str, name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("project name is required")
    data = load(home_dir)
    pid = new_project_id(name, [p["id"] for p in data["projects"]])
    project = {"id": pid, "name": name, "archived": False}
    data["projects"].append(project)
    data["order"].append(pid)
    save(home_dir, data)
    return project


def patch_project(home_dir: str, project_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Rename or archive a project. Raises KeyError if it does not exist."""
    data = load(home_dir)
    found: Optional[Dict[str, Any]] = None
    for p in data["projects"]:
        if p["id"] == project_id:
            found = p
            break
    if found is None:
        raise KeyError(project_id)
    touched = False
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("project name is required")
        found["name"] = name
        touched = True
    if "archived" in body:
        found["archived"] = bool(body.get("archived"))
        touched = True
    if not touched:
        raise ValueError("nothing to update")
    save(home_dir, data)
    return found


def delete_project(home_dir: str, project_id: str) -> bool:
    """Remove a project. Returns False if it did not exist.

    Does not touch rooms — the caller (adapter) is responsible for
    unfiling any room whose ``project_id`` pointed here, since that
    membership lives on the room, not in this file.
    """
    data = load(home_dir)
    before = len(data["projects"])
    data["projects"] = [p for p in data["projects"] if p["id"] != project_id]
    data["order"] = [pid for pid in data["order"] if pid != project_id]
    if len(data["projects"]) == before:
        return False
    save(home_dir, data)
    return True
