"""Composer attachments stored with the room (issue #38).

Files live under ``$HERMES_HOME/retinue_rooms/attachments/<room_id>/`` and
are addressed as ``/workspace/uploads/<name>`` so the existing transcript
renderer and ``GET /rooms/{id}/files`` path work without the sandbox up.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from . import workspace

UPLOAD_PREFIX = "/workspace/uploads/"
_UPLOAD_PATH_RE = re.compile(
    r"/workspace/uploads/[A-Za-z0-9._+/-]+",
)
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


def list_uploads(home_dir: str, room_id: str) -> List[Dict[str, Any]]:
    """Room catalog — everything already published for this room."""
    folder = _dir(home_dir, room_id)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for name in names:
        if name.endswith(".tmp") or name.startswith("."):
            continue
        dest = os.path.join(folder, name)
        if not os.path.isfile(dest):
            continue
        out.append(
            {
                "name": name,
                "path": public_path(name),
                "bytes": os.path.getsize(dest),
                "image": bool(_IMAGE_EXT.search(name)),
            }
        )
    return out


def publish_file(home_dir: str, room_id: str, src: str) -> Optional[Dict[str, Any]]:
    """Copy a host file into the room catalog. None if unreadable/empty."""
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    if size <= 0 or size > MAX_ATTACHMENT:
        return None
    name = safe_name(os.path.basename(src))
    dest = os.path.join(_dir(home_dir, room_id), name)
    if os.path.isfile(dest) and os.path.getsize(dest) == size:
        return {
            "name": name,
            "path": public_path(name),
            "bytes": size,
            "image": bool(_IMAGE_EXT.search(name)),
        }
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        return save(home_dir, room_id, name, data)
    except ValueError:
        return None


def _profile_output_dirs(home_dir: str, slug: str) -> List[str]:
    root = os.path.join(home_dir, "profiles", slug)
    return [
        os.path.join(root, "image_cache"),
        os.path.join(root, "images"),
        os.path.join(root, "attachments"),
        os.path.join(root, "cache", "images"),
    ]


def _iter_files(folder: str) -> Iterable[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith(".") or name.endswith(".tmp"):
            continue
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            yield path


def _mentioned(name: str, text: str) -> bool:
    stem, _ext = os.path.splitext(name)
    if not stem:
        return False
    blob = (text or "").lower()
    token = stem.lower().replace("_", " ").replace("-", " ")
    if stem.lower() in blob or token in blob:
        return True
    bits = [b for b in re.split(r"[_\-\s]+", stem.lower()) if len(b) >= 5]
    if not bits:
        return False
    hits = sum(1 for b in bits if b in blob)
    return hits >= min(2, len(bits))


def harvest(
    home_dir: str,
    room_id: str,
    slug: str,
    *,
    since: float,
    reply: str = "",
) -> List[Dict[str, Any]]:
    """Publish work this member made so the room can show and recall it.

    *since* is the room's created_at (or the turn start). Files in the
    member's image/output dirs at or after that time are copied into the
    room catalog. A file whose name is mentioned in *reply* is taken even
    if it is older (recall by name).
    """
    published: List[Dict[str, Any]] = []
    seen: set[str] = {item["name"] for item in list_uploads(home_dir, room_id)}
    for folder in _profile_output_dirs(home_dir, slug):
        for src in _iter_files(folder):
            name = safe_name(os.path.basename(src))
            try:
                mtime = os.path.getmtime(src)
            except OSError:
                continue
            if mtime < since and not _mentioned(os.path.basename(src), reply):
                continue
            meta = publish_file(home_dir, room_id, src)
            if meta is None:
                continue
            if meta["name"] in seen:
                continue
            seen.add(meta["name"])
            published.append(meta)
    return published


def matching_uploads(home_dir: str, room_id: str, text: str) -> List[Dict[str, Any]]:
    """Already-published files whose names match the user's ask."""
    if not (text or "").strip():
        return []
    return [item for item in list_uploads(home_dir, room_id) if _mentioned(item["name"], text)]


def upload_paths_in(text: str) -> List[str]:
    """``/workspace/uploads/...`` mentions in a transcript line."""
    seen: set[str] = set()
    out: List[str] = []
    for match in _UPLOAD_PATH_RE.finditer(text or ""):
        path = match.group(0)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def host_media_for_text(
    home_dir: str, room_id: str, text: str
) -> tuple[List[str], List[str]]:
    """Host files + MIME types for upload paths in *text* (vision / docs)."""
    urls: List[str] = []
    types: List[str] = []
    for raw in upload_paths_in(text):
        name = safe_name(raw[len(UPLOAD_PREFIX) :])
        dest = host_path(home_dir, room_id, name)
        if not os.path.isfile(dest):
            continue
        urls.append(dest)
        types.append(workspace.content_type_for(raw))
    return urls, types


def sync_uploads_into_room(home_dir: str, room: Any) -> int:
    """Copy room attachments into the live container ``/workspace/uploads``.

    New turns also bind-mount that dir. This copy covers containers that
    were created before the mount existed.
    """
    from .engine import Room

    if not isinstance(room, Room):
        return 0
    folder = _dir(home_dir, room.id)
    try:
        names = [n for n in os.listdir(folder) if not n.startswith(".") and not n.endswith(".tmp")]
    except OSError:
        return 0
    files = [os.path.join(folder, n) for n in names if os.path.isfile(os.path.join(folder, n))]
    if not files:
        return 0
    copied = 0
    if (room.workspace or "sandbox") == "ide" and room.ide_path:
        dest_dir = os.path.join(room.ide_path, "uploads")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            dest_dir = ""
        if dest_dir:
            for src in files:
                dest = os.path.join(dest_dir, os.path.basename(src))
                try:
                    if not os.path.isfile(dest) or os.path.getsize(dest) != os.path.getsize(src):
                        with open(src, "rb") as inf, open(dest, "wb") as outf:
                            outf.write(inf.read())
                    copied += 1
                except OSError:
                    continue
    runtime = workspace._runtime()
    if not runtime:
        return copied
    try:
        cids = workspace._container_ids_for_room(room)
    except workspace.WorkspaceFileError:
        return copied
    for cid in cids:
        try:
            subprocess.run(
                [runtime, "start", cid],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
            subprocess.run(
                [runtime, "exec", cid, "mkdir", "-p", "/workspace/uploads"],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for src in files:
            name = os.path.basename(src)
            try:
                proc = subprocess.run(
                    [runtime, "cp", src, f"{cid}:/workspace/uploads/{name}"],
                    check=False,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0:
                copied += 1
    return copied


def with_published_paths(text: str, published: List[Dict[str, Any]]) -> str:
    """Ensure the reply names every published /workspace path (UI inline)."""
    extra = [p["path"] for p in published if p.get("path") and p["path"] not in (text or "")]
    if not extra:
        return text or ""
    body = (text or "").rstrip()
    block = "\n".join(extra)
    return f"{body}\n\n{block}" if body else block
