"""Workspace-computer status for the take-over view.

The shared container is identified by the carried
TERMINAL_DOCKER_SHARED_CONTAINER_KEY label (hermes-profile=<sanitized key>).
A full noVNC take-over is a later increment; this module exposes whether
the computer is up and how the operator attaches.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from . import ide
from .engine import Room

_LABEL_OK = re.compile(r"[^a-zA-Z0-9_.-]+")


def sanitize_label(value: str) -> str:
    cleaned = _LABEL_OK.sub("_", value or "")[:63]
    return cleaned or "unknown"


def _runtime() -> Optional[str]:
    forced = (os.getenv("HERMES_DOCKER_BINARY") or "").strip()
    if forced:
        return forced
    return shutil.which("podman") or shutil.which("docker")


def workspace_status() -> Dict[str, Any]:
    key = (os.getenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY") or "").strip()
    runtime = _runtime()
    status: Dict[str, Any] = {
        "enabled": bool(key),
        "key": key or None,
        "runtime": runtime,
        "container": None,
        "running": False,
        "attach": None,
        "detail": None,
        "ide_root": ide.configured_ide_root(),
    }
    if not key:
        status["detail"] = (
            "Set TERMINAL_DOCKER_SHARED_CONTAINER_KEY to enable the shared "
            "workspace computer (see retinue/ROOMS.md)."
        )
        return status
    if not runtime:
        status["detail"] = "neither podman nor docker is on PATH"
        return status
    label = sanitize_label(key)
    try:
        proc = subprocess.run(
            [
                runtime,
                "ps",
                "-a",
                "--filter",
                "label=hermes-agent=1",
                "--filter",
                f"label=hermes-profile={label}",
                "--format",
                "{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        status["detail"] = f"{runtime} inspect failed: {e}"
        return status
    rows: List[Dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, name, st = parts[0], parts[1], parts[2]
        image = parts[3] if len(parts) > 3 else ""
        running = st.lower().startswith("up")
        rows.append(
            {
                "id": cid,
                "name": name,
                "status": st,
                "image": image,
                "running": str(running),
            }
        )
        if running and status["container"] is None:
            status["container"] = {
                "id": cid,
                "name": name,
                "status": st,
                "image": image,
            }
            status["running"] = True
            status["attach"] = f"{runtime} exec -it {name} /bin/sh"
    if not rows:
        status["detail"] = (
            f"no hermes workspace container with label hermes-profile={label} "
            "— it is created on the first terminal tool call"
        )
    elif not status["running"]:
        status["container"] = {
            "id": rows[0]["id"],
            "name": rows[0]["name"],
            "status": rows[0]["status"],
            "image": rows[0]["image"],
        }
        status["detail"] = "workspace container exists but is not running"
        status["attach"] = f"{runtime} start {rows[0]['name']}"
    return status


# ── workspace files (served into the room UI) ────────────────────────────

MAX_WORKSPACE_FILE = 8 * 1024 * 1024
_WORKSPACE_ROOT = "/workspace"
_PATH_OK = re.compile(r"^[A-Za-z0-9._+-]+$")
_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".sh": "text/x-shellscript; charset=utf-8",
}


class WorkspaceFileError(Exception):
    """HTTP-shaped failure: ``status`` is 400 / 404 / 413 / 503."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


def normalize_workspace_path(raw: str) -> str:
    """Return a posix path under ``/workspace``. Raise ``WorkspaceFileError``."""
    text = (raw or "").strip().replace("\\", "/")
    if text.startswith("workspace/"):
        text = "/" + text
    if not text.startswith(_WORKSPACE_ROOT + "/") and text != _WORKSPACE_ROOT:
        raise WorkspaceFileError(400, "path must be under /workspace")
    if "\x00" in text or "//" in text:
        raise WorkspaceFileError(400, "invalid workspace path")
    parts = [p for p in text.split("/") if p]
    if parts[:1] != ["workspace"] or any(p in (".", "..") or not _PATH_OK.fullmatch(p) for p in parts):
        raise WorkspaceFileError(400, "invalid workspace path")
    if len(parts) < 2:
        raise WorkspaceFileError(400, "path must name a file under /workspace")
    return "/" + "/".join(parts)


def content_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _TYPES.get(ext, "application/octet-stream")


def _container_ids_for_room(room: Room) -> List[str]:
    runtime = _runtime()
    if not runtime:
        raise WorkspaceFileError(503, "neither podman nor docker is on PATH")
    label = sanitize_label(ide.container_key(room.id, room.workspace or "sandbox"))
    try:
        proc = subprocess.run(
            [
                runtime,
                "ps",
                "-a",
                "--filter",
                "label=hermes-agent=1",
                "--filter",
                f"label=hermes-profile={label}",
                "--format",
                "{{.ID}}\t{{.Status}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceFileError(503, f"container inspect failed: {e}") from e
    running: List[str] = []
    stopped: List[str] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        cid = parts[0].strip()
        st = (parts[1] if len(parts) > 1 else "").lower()
        (running if st.startswith("up") else stopped).append(cid)
    return running + stopped


def _read_from_container(cid: str, path: str) -> bytes:
    runtime = _runtime()
    if not runtime:
        raise WorkspaceFileError(503, "neither podman nor docker is on PATH")
    try:
        proc = subprocess.run(
            [runtime, "exec", cid, "cat", path],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceFileError(503, f"container read failed: {e}") from e
    if proc.returncode != 0:
        raise WorkspaceFileError(404, "file not found in workspace")
    data = proc.stdout or b""
    if len(data) > MAX_WORKSPACE_FILE:
        raise WorkspaceFileError(413, "workspace file too large")
    return data


def read_workspace_file(room: Room, raw_path: str) -> tuple[bytes, str]:
    """Bytes + content-type for a path the member wrote under /workspace."""
    path = normalize_workspace_path(raw_path)
    rel = path[len(_WORKSPACE_ROOT) :].lstrip("/")
    if (room.workspace or "sandbox") == "ide":
        root = ide.resolve_ide_path(room.ide_path)
        host = os.path.realpath(os.path.join(root, rel))
        if host != root and not host.startswith(root + os.sep):
            raise WorkspaceFileError(400, "invalid workspace path")
        if not os.path.isfile(host):
            raise WorkspaceFileError(404, "file not found in workspace")
        size = os.path.getsize(host)
        if size > MAX_WORKSPACE_FILE:
            raise WorkspaceFileError(413, "workspace file too large")
        with open(host, "rb") as f:
            return f.read(), content_type_for(path)
    last_err: Optional[WorkspaceFileError] = None
    for cid in _container_ids_for_room(room):
        try:
            return _read_from_container(cid, path), content_type_for(path)
        except WorkspaceFileError as e:
            last_err = e
            if e.status != 404:
                raise
    if last_err is not None:
        raise last_err
    raise WorkspaceFileError(404, "workspace computer is not running")
