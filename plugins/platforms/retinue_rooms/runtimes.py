"""Agent runtime registry — which harness executes a member's turn.

Retinue members historically all executed through one path: the Hermes
agent loop (``adapter._agent_turn`` injecting a MessageEvent into the
gateway).  Grok Build introduces a second, architecturally different
path: xAI's own agent harness, driven over ACP (``grokbuild.py``), which
owns its whole tool loop — Retinue observes it rather than running one
model turn per tool call.

This module is the single place that knows which runtimes exist and what
each is capable of.  Everything else — hire validation, timeout
selection, the turn dispatch in ``adapter._agent_turn``, the web UI —
asks this registry instead of hard-coding ``if runtime == "grok-build"``
checks.

A member's runtime is stored in its ``retinue-agent.json`` under the
``runtime`` key; absent means Hermes, so every pre-existing profile keeps
working unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RUNTIME_HERMES = "hermes"
RUNTIME_GROK_BUILD = "grok-build"

GROK_BUILD_PICKER_PREFIX = "grok-build:"
HERMES_PICKER_PREFIX = "hermes:"

_META_FILENAME = "retinue-agent.json"


@dataclass(frozen=True)
class RuntimeInfo:
    """Static description of one agent runtime."""

    id: str
    label: str
    description: str
    # Capability flags are advisory metadata for the UI and for future
    # runtimes; they are not a permission system.  Keys are stable strings
    # so a new runtime can declare a subset without code changes elsewhere.
    capabilities: Dict[str, bool] = field(default_factory=dict)
    # Where the model list comes from (#236): workspace presets, or the
    # runtime's own catalog (``grok models``).
    model_source: str = "presets"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "capabilities": dict(self.capabilities),
            "model_source": self.model_source,
        }


_REGISTRY: Dict[str, RuntimeInfo] = {
    RUNTIME_HERMES: RuntimeInfo(
        id=RUNTIME_HERMES,
        label="Hermes",
        description=(
            "The built-in Hermes agent loop: any supported model provider, "
            "room tools in the shared workspace container."
        ),
        capabilities={
            "streaming": True,
            "tool_activity": False,  # Hermes room turns surface only the final reply
            "filesystem": True,
            "shell": True,
            "mcp": True,
            "model_choice": True,  # workspace retinue_models/ presets
            "session_resume": True,
            "approvals": False,  # room turns resolve approvals by config (#208)
            "containerized": True,
            "subagents": False,
        },
        model_source="presets",
    ),
    RUNTIME_GROK_BUILD: RuntimeInfo(
        id=RUNTIME_GROK_BUILD,
        label="Grok Build",
        description=(
            "xAI's native agent harness around Grok 4.6, driven over ACP. "
            "Grok Build owns the whole tool loop; Retinue streams its "
            "activity, answers permission requests, and posts the result. "
            "Tools run directly on this machine in the room's project tree, "
            "not in the room container."
        ),
        capabilities={
            "streaming": True,
            "tool_activity": True,
            "filesystem": True,
            "shell": True,
            "mcp": True,  # workspace-declared servers via grokbuild/mcp.json (#220)
            "model_choice": True,  # Grok's own catalog, not workspace presets
            "session_resume": True,
            "approvals": True,  # session/request_permission answered by policy
            "containerized": False,
            "subagents": True,
        },
        model_source="runtime",
    ),
}


def known_runtimes() -> List[str]:
    return list(_REGISTRY)


def runtime_info(runtime_id: str) -> Optional[RuntimeInfo]:
    return _REGISTRY.get(normalize_runtime(runtime_id))


def normalize_runtime(value: Any) -> str:
    """Map free-form input onto a registry id. Unknown/absent -> hermes."""
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in ("", RUNTIME_HERMES):
        return RUNTIME_HERMES
    if raw in (RUNTIME_GROK_BUILD, "grokbuild", "grok"):
        return RUNTIME_GROK_BUILD
    return raw


def validate_runtime(value: Any) -> str:
    """Return the normalized runtime id, raising ValueError when unknown."""
    runtime = normalize_runtime(value)
    if runtime not in _REGISTRY:
        raise ValueError(
            f"unknown runtime {value!r}; expected one of {sorted(_REGISTRY)}"
        )
    return runtime


def runtime_for_member(home_dir: str, slug: str) -> str:
    """The runtime a member executes on, from its ``retinue-agent.json``.

    Missing profile / meta / key all mean Hermes — the runtime axis is
    additive and must never break a pre-existing member.
    """
    slug = (slug or "default").strip() or "default"
    if slug == "default":
        return RUNTIME_HERMES
    path = os.path.join(home_dir, "profiles", slug, _META_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return RUNTIME_HERMES
    runtime = normalize_runtime(meta.get("runtime"))
    return runtime if runtime in _REGISTRY else RUNTIME_HERMES


def parse_picker_value(value: str) -> tuple[str, str]:
    """Decode a roster/hire picker value into ``(runtime, model)``.

    Qualified values keep the axes separate on the wire:

    * ``grok-build:grok-4.5`` → Grok Build catalog id
    * ``hermes:grok-4.5`` → Hermes workspace preset
    * ``grok-4.5`` (bare) → Hermes preset, the pre-#236 PATCH body
    """
    raw = str(value or "").strip()
    if raw.startswith(GROK_BUILD_PICKER_PREFIX):
        return RUNTIME_GROK_BUILD, raw[len(GROK_BUILD_PICKER_PREFIX) :].strip()
    if raw.startswith(HERMES_PICKER_PREFIX):
        return RUNTIME_HERMES, raw[len(HERMES_PICKER_PREFIX) :].strip()
    return RUNTIME_HERMES, raw


def format_picker_value(runtime: str, model: str) -> str:
    """Encode ``(runtime, model)`` for the roster <select> value."""
    runtime = normalize_runtime(runtime)
    model = (model or "").strip()
    if runtime == RUNTIME_GROK_BUILD:
        return f"{GROK_BUILD_PICKER_PREFIX}{model}"
    return model


def list_runtimes(home_dir: str) -> List[Dict[str, Any]]:
    """Registry + live availability, for ``GET /runtimes`` and the hire UI."""
    entries: List[Dict[str, Any]] = []
    for info in _REGISTRY.values():
        entry = info.to_dict()
        if info.id == RUNTIME_GROK_BUILD:
            from . import grokbuild

            entry["health"] = grokbuild.health(home_dir)
            cat = grokbuild.catalog(home_dir)
            entry["models"] = list(cat.get("models") or [])
            entry["default_model"] = cat.get("default") or ""
        else:
            entry["health"] = {"status": "available"}
            entry["models"] = []
            entry["default_model"] = ""
        entries.append(entry)
    return entries
