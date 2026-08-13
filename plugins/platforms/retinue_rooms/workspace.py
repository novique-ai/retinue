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
