"""The hire flow — turn a three-field brief into a Hermes profile.

Grok Bot's creation UX, the part worth copying: an agent is born from a
name, one primary job, and a description of how it should work. Here that
becomes a profile directory (persona SOUL.md + model config), which the
multiplexing gateway serves as a room member.

Pure filesystem logic — unit-testable without a gateway (see test_hire.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RESERVED = {"default", "profiles"}

AGENT_META_FILENAME = "retinue-agent.json"

# Workspace-configurable model presets: $HERMES_HOME/retinue_models/<name>.yaml,
# each holding a literal ``model:`` block that a hire copies verbatim (the same
# text-level semantics as the root-config fallback below).
MODELS_DIRNAME = "retinue_models"


def slugify_name(display_name: str) -> str:
    """Mention token for a display name: lowercase, alnum + hyphens."""
    slug = _SLUG_RE.sub("-", (display_name or "").lower()).strip("-")
    return slug[:32]


def soul_template(display_name: str, job: str, how: str) -> str:
    parts = [f"You are {display_name}.", "", f"Your job: {job.strip()}"]
    how = (how or "").strip()
    if how:
        parts += ["", "How you work:", how]
    parts += [
        "",
        f"Identity: your name is {display_name} — that is who you are in "
        "every reply. The language model and any developer tooling underneath "
        "(e.g. \"Claude\", \"Claude Code\", \"GPT\") are the engine you run "
        "on, not who you are; never introduce or describe yourself by an "
        "engine name.",
        "",
        "Stay in character and within your job. If a request belongs to a "
        "teammate's specialty, say so briefly instead of guessing.",
    ]
    return "\n".join(parts) + "\n"


def _read_model_block(path: str) -> str:
    """Extract the ``model:`` block from a YAML file, verbatim. Text-level
    extraction on purpose: it preserves comments and never needs a YAML
    dependency at import time. Returns "" when the file has no block."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    block: List[str] = []
    inside = False
    for line in lines:
        if line.startswith("model:"):
            inside = True
            block.append(line)
            continue
        if inside:
            if line.strip() and not line.startswith((" ", "\t", "#")):
                break
            block.append(line)
    return "".join(block).rstrip() + "\n" if block else ""


def _extract_model_block(root_config_path: str) -> str:
    """The workspace-default model block: the root config's, or a fallback."""
    return _read_model_block(root_config_path) or (
        "model:\n  default: claude-haiku-4-5\n  provider: anthropic\n"
    )


def _fields_from_model_block(block: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in (block or "").splitlines()[1:]:
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition(":")
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def _summarize_model_block(block: str) -> str:
    """One-line human summary of a model block, e.g. 'custom · local/auto'."""
    fields = _fields_from_model_block(block)
    model = fields.get("default") or fields.get("model") or "?"
    provider = fields.get("provider") or "auto"
    return f"{provider} · {model}"


_CLOUD_TURN_DEFAULT = 300.0
_LOCAL_TURN_DEFAULT = 1800.0
_LOCAL_PROVIDERS = {
    "custom",
    "local",
    "ollama",
    "llamacpp",
    "llama.cpp",
    "lmstudio",
    "vllm",
    "openai-compatible",
}


def _url_is_private(url: str) -> bool:
    host = (url or "").strip().lower()
    if not host:
        return False
    host = host.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    if host.endswith(".ts.net"):
        return True
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def model_block_is_local(block: str) -> bool:
    """True when this model: block is a self-hosted / local-LLM endpoint.

    Local generations on claymore-1 routinely take minutes (and longer when
    two room members share one llama-server), so they must not share the
    cloud turn budget.
    """
    fields = _fields_from_model_block(block)
    model = (fields.get("default") or fields.get("model") or "").lower()
    provider = (fields.get("provider") or "").lower()
    base = fields.get("base_url") or ""
    if model.startswith("local/") or model.startswith("local-"):
        return True
    if _url_is_private(base):
        return True
    if provider in {"ollama", "llamacpp", "llama.cpp", "lmstudio", "vllm"}:
        return True
    if provider == "custom" and not base.lower().startswith("https://api."):
        return True
    if provider in _LOCAL_PROVIDERS and _url_is_private(base):
        return True
    return False


def profile_uses_local_llm(home_dir: str, slug: str) -> bool:
    """Look at profiles/<slug>/config.yaml, then the workspace root.

    Unknown / unreadable profiles fail safe to local (longer wait) so a
    slow first turn is not cut off.
    """
    slug = (slug or "default").strip() or "default"
    if slug == "default":
        path = os.path.join(home_dir, "config.yaml")
    else:
        path = os.path.join(home_dir, "profiles", slug, "config.yaml")
    block = _read_model_block(path)
    if not block and slug != "default":
        block = _read_model_block(os.path.join(home_dir, "config.yaml"))
    if not block:
        return True
    return model_block_is_local(block)


def _env_timeout(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(5.0, float(raw))
    except (ValueError, TypeError):
        return default


def cloud_turn_timeout() -> float:
    return _env_timeout("RETINUE_ROOMS_TURN_TIMEOUT", _CLOUD_TURN_DEFAULT)


def local_turn_timeout() -> float:
    raw = (os.getenv("RETINUE_ROOMS_LOCAL_TURN_TIMEOUT") or "").strip()
    if raw:
        return _env_timeout("RETINUE_ROOMS_LOCAL_TURN_TIMEOUT", _LOCAL_TURN_DEFAULT)
    return max(_LOCAL_TURN_DEFAULT, cloud_turn_timeout())


def turn_timeout_for(home_dir: str, slug: str) -> float:
    if profile_uses_local_llm(home_dir, slug):
        return local_turn_timeout()
    return cloud_turn_timeout()


def _models_dir(home_dir: str) -> str:
    return os.path.join(home_dir, MODELS_DIRNAME)


def list_model_presets(home_dir: str) -> List[Dict[str, str]]:
    """Workspace model presets a hire can choose from. A preset is a YAML
    file in ``retinue_models/`` whose ``model:`` block is copied verbatim
    into the new profile; files without a ``model:`` block are skipped."""
    presets: List[Dict[str, str]] = []
    try:
        names = sorted(os.listdir(_models_dir(home_dir)))
    except OSError:
        names = []
    for fn in names:
        stem, ext = os.path.splitext(fn)
        if ext not in (".yaml", ".yml") or not stem:
            continue
        block = _read_model_block(os.path.join(_models_dir(home_dir), fn))
        if block:
            presets.append({"name": stem, "summary": _summarize_model_block(block)})
    return presets


def _preset_model_block(home_dir: str, preset: str) -> str:
    """The model block for a named preset. Raises ValueError on an unknown
    or malformed preset (the caller surfaces this as a 400)."""
    for ext in (".yaml", ".yml"):
        path = os.path.join(_models_dir(home_dir), preset + ext)
        if os.path.isfile(path):
            block = _read_model_block(path)
            if not block:
                raise ValueError(
                    f"model preset {preset!r} has no 'model:' block ({path})"
                )
            return block
    known = ", ".join(p["name"] for p in list_model_presets(home_dir)) or "none"
    raise ValueError(f"unknown model preset {preset!r} (available: {known})")


def scaffold_profile(
    home_dir: str,
    display_name: str,
    job: str,
    how: str,
    model_preset: Optional[str] = None,
) -> Dict[str, Any]:
    """Create ``profiles/<slug>/`` under *home_dir*. Raises ValueError on bad
    input (including an unknown *model_preset*), FileExistsError if the
    profile already exists."""
    display_name = (display_name or "").strip()
    job = (job or "").strip()
    model_preset = (model_preset or "").strip() or None
    if not display_name:
        raise ValueError("agent name is required")
    if not job:
        raise ValueError("primary job is required")
    slug = slugify_name(display_name)
    if not slug or slug in _RESERVED:
        raise ValueError(f"cannot derive a usable profile name from {display_name!r}")

    # Resolve the model BEFORE creating anything, so a bad preset leaves no
    # half-made profile behind.
    if model_preset:
        model_block = _preset_model_block(home_dir, model_preset)
    else:
        model_block = _extract_model_block(os.path.join(home_dir, "config.yaml"))

    profile_dir = os.path.join(home_dir, "profiles", slug)
    if os.path.isdir(profile_dir):
        raise FileExistsError(slug)
    os.makedirs(profile_dir)

    with open(os.path.join(profile_dir, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(soul_template(display_name, job, how))

    with open(os.path.join(profile_dir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(model_block + "agent:\n  tool_choice: auto\n")

    # Credentials: a profile's secret scope reads its own .env and auth store;
    # seed both from the workspace root so a freshly hired agent can reach any
    # provider the workspace owner has configured or OAuth-logged-into.
    for cred, mode in ((".env", 0o600), ("auth.json", 0o600)):
        root_cred = os.path.join(home_dir, cred)
        if os.path.isfile(root_cred):
            shutil.copy(root_cred, os.path.join(profile_dir, cred))
            os.chmod(os.path.join(profile_dir, cred), mode)

    meta = {
        "display_name": display_name,
        "slug": slug,
        "job": job,
        "how": (how or "").strip(),
        "model_preset": model_preset,
        "created_at": time.time(),
    }
    with open(os.path.join(profile_dir, AGENT_META_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return meta


def activate_hired_profile(slug: str, runner: Any = None) -> Dict[str, Any]:
    """Hot-register a just-scaffolded profile into a live multiplexer.

    The gateway snapshots pairing stores / busy modes / runtime status at
    startup. Room turns themselves resolve the profile home from disk, but
    without this registration ``hermes status`` omits the hire and any
    non-internal path that consults ``pairing_stores`` treats it as unserved.

    Plugin-shaped: uses the runner's existing seams, no upstream edits.
    Returns ``{"online": bool, "activation": str}``.
    """
    slug = (slug or "").strip()
    if not slug:
        return {
            "online": False,
            "activation": "will come online the next time the gateway starts",
        }
    if runner is None:
        return {
            "online": False,
            "activation": "will come online the next time the gateway starts",
        }

    stores = getattr(runner, "pairing_stores", None)
    if isinstance(stores, dict) and slug not in stores:
        try:
            from gateway.pairing import PairingStore

            stores[slug] = PairingStore(profile=slug)
        except Exception:
            logger.debug("Retinue hire: pairing store for %s failed", slug, exc_info=True)

    adapters = getattr(runner, "_profile_adapters", None)
    if isinstance(adapters, dict):
        adapters.setdefault(slug, {})

    snapshot = getattr(runner, "_snapshot_profile_busy_modes", None)
    if callable(snapshot):
        try:
            snapshot(slug, {})
        except Exception:
            logger.debug(
                "Retinue hire: busy-mode snapshot for %s failed", slug, exc_info=True
            )

    try:
        from gateway.status import write_runtime_status

        served = {"default", slug}
        if isinstance(stores, dict):
            served.update(str(name) for name in stores if name)
        if isinstance(adapters, dict):
            served.update(str(name) for name in adapters if name)
        write_runtime_status(served_profiles=sorted(served))
    except Exception:
        logger.debug(
            "Retinue hire: runtime status update for %s failed", slug, exc_info=True
        )

    return {"online": True, "activation": "online"}


def list_agents(home_dir: str) -> List[Dict[str, Any]]:
    """Roster of hire-able room members: every profile directory, with hire
    metadata when present (hand-made profiles get a slug-only entry)."""
    profiles_dir = os.path.join(home_dir, "profiles")
    agents: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        names = []
    for name in names:
        pdir = os.path.join(profiles_dir, name)
        if not os.path.isdir(pdir):
            continue
        meta: Optional[Dict[str, Any]] = None
        try:
            with open(os.path.join(pdir, AGENT_META_FILENAME), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None
        if meta is None:
            meta = {"display_name": name, "slug": name, "job": "", "how": ""}
        meta["has_soul"] = os.path.isfile(os.path.join(pdir, "SOUL.md"))
        meta["local_llm"] = profile_uses_local_llm(home_dir, name)
        meta["turn_timeout"] = int(turn_timeout_for(home_dir, name))
        agents.append(meta)
    return agents
