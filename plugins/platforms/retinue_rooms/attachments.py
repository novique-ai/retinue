"""Composer attachments stored with the room (issue #38).

Files live under ``$HERMES_HOME/retinue_rooms/attachments/<room_id>/`` and
are addressed as ``/workspace/uploads/<name>`` so the existing transcript
renderer and ``GET /rooms/{id}/files`` path work without the sandbox up.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from . import workspace

UPLOAD_PREFIX = "/workspace/uploads/"
MAX_ATTACHMENT = 8 * 1024 * 1024
_MAX_NAME = 80
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg)$", re.I)


def safe_name(filename: str) -> str:
    base = os.path.basename(filename or "file")
    cleaned = _NAME_RE.sub("_", base).strip("._") or "file"
    return cleaned[:_MAX_NAME]


def _dir(home_dir: str, room_id: str) -> str:
    return os.path.join(home_dir, "retinue_rooms", "attachments", room_id)


def host_path(home_dir: str, room_id: str, name: str) -> str:
    return os.path.join(_dir(home_dir, room_id), safe_name(name))


def public_path(name: str) -> str:
    return UPLOAD_PREFIX + safe_name(name)


def save(home_dir: str, room_id: str, filename: str, data: bytes) -> Dict[str, Any]:
    if not data:
        raise ValueError("empty attachment")
    if len(data) > MAX_ATTACHMENT:
        raise ValueError("attachment too large")
    name = safe_name(filename)
    folder = _dir(home_dir, room_id)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, name)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return {
        "name": name,
        "path": public_path(name),
        "bytes": len(data),
        "image": bool(_IMAGE_EXT.search(name)),
    }


def read_upload(home_dir: str, room_id: str, raw_path: str) -> Optional[tuple[bytes, str]]:
    """If *raw_path* is a room upload, return bytes + content-type."""
    try:
        path = workspace.normalize_workspace_path(raw_path)
    except workspace.WorkspaceFileError:
        return None
    if not path.startswith(UPLOAD_PREFIX.rstrip("/") + "/"):
        return None
    name = safe_name(path[len(UPLOAD_PREFIX) :])
    dest = os.path.join(_dir(home_dir, room_id), name)
    if not os.path.isfile(dest):
        raise workspace.WorkspaceFileError(404, "attachment not found")
    size = os.path.getsize(dest)
    if size > MAX_ATTACHMENT:
        raise workspace.WorkspaceFileError(413, "attachment too large")
    with open(dest, "rb") as f:
        data = f.read()
    return data, workspace.content_type_for(path)
