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
BUNDLED_PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_presets")


def slugify_name(display_name: str) -> str:
    """Mention token for a display name: lowercase, alnum + hyphens."""
    slug = _SLUG_RE.sub("-", (display_name or "").lower()).strip("-")
    return slug[:32]


def _persona_manner_lines(persona: Optional[Dict[str, Any]]) -> List[str]:
    """Phrases for non-balanced dials. Empty when all-balanced or unset."""
    from .identity import persona_soul_lines

    return persona_soul_lines(persona)


def soul_template(
    display_name: str,
    job: str,
    how: str,
    persona: Optional[Dict[str, Any]] = None,
) -> str:
    parts = [f"You are {display_name}.", "", f"Your job: {job.strip()}"]
    how = (how or "").strip()
    if how:
        parts += ["", "How you work:", how]
    # Non-balanced persona dials only. All-balanced must add nothing —
    # every existing agent is effectively all-balanced, so the SOUL has
    # to stay byte-identical to what it is today.
    manner = _persona_manner_lines(persona)
    if manner:
        parts += ["", *manner]
    parts += [
        "",
        f"Identity: your name is {display_name} — that is who you are in "
        "every reply. The language model and any developer tooling underneath "
        "(e.g. \"Claude\", \"Claude Code\", \"GPT\") are the engine you run "
        "on, not who you are; never introduce or describe yourself by an "
        "engine name.",
        "",
        "Stay in character and within your job.",
        "",
        "Rooms: you speak only as yourself. Never write another teammate's "
        "lines. To hand work to a teammate, @ them by the name the user "
        "would type (their first / display name) in your own prose — not "
        "inside a fenced draft. One sentence of what you need, then stop "
        "so they can take the next turn. Do not guess at another specialty. "
        "Do not @-spam the roster.",
        "",
        "If you are this room's lead (you answer when nobody is @mentioned), "
        "you own the itinerary. On the first turn of a project, and whenever "
        "the plan changes, include a fenced itinerary block in your reply:",
        "```itinerary",
        "title: short name",
        "where: one or two sentences on current progress",
        "- [doing] the active step",
        "- [todo] next step",
        "- [done] finished step",
        "```",
        "Do not wait for the user to open the Itinerary pane. If you are not "
        "the lead, do not write that block.",
        "",
        "Work you make stays in the room. Write files under /workspace and "
        "put the /workspace/... path in your reply so it shows on the "
        "transcript. When asked for something you made earlier, reuse that "
        "path. If you cannot find it, say so in one sentence — never go "
        "silent.",
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
# An IDE room is attached to a real project tree, so a turn is not one
# model call — it is terminal work: reading files, running bd, editing a
# skill. Five minutes is a sandbox chat budget and it cuts that work off
# mid-flight. Sandbox rooms keep the cloud default.
_IDE_TURN_DEFAULT = 900.0
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


def ide_turn_timeout() -> float:
    raw = (os.getenv("RETINUE_ROOMS_IDE_TURN_TIMEOUT") or "").strip()
    if raw:
        return _env_timeout("RETINUE_ROOMS_IDE_TURN_TIMEOUT", _IDE_TURN_DEFAULT)
    return max(_IDE_TURN_DEFAULT, cloud_turn_timeout())


def turn_timeout_for(home_dir: str, slug: str, workspace: str = "") -> float:
    if profile_uses_local_llm(home_dir, slug):
        return local_turn_timeout()
    if (workspace or "") == "ide":
        return ide_turn_timeout()
    return cloud_turn_timeout()


def _models_dir(home_dir: str) -> str:
    return os.path.join(home_dir, MODELS_DIRNAME)


def _preset_entry(stem: str, block: str) -> Dict[str, Any]:
    fields = _fields_from_model_block(block)
    model = fields.get("default") or fields.get("model") or ""
    provider = fields.get("provider") or ""
    return {
        "name": stem,
        "summary": _summarize_model_block(block),
        "provider": provider,
        "model": model,
        "local": model_block_is_local(block),
    }


_VERSION_SUFFIX = re.compile(r"^\d+(?:[.\-]\d+)*$")


def _is_generic_alias(name: str, names: set[str]) -> bool:
    """``grok`` is an alias once ``grok-4.5`` / ``grok-4.6`` exist — and so is
    ``claude-sonnet`` once ``claude-sonnet-5`` does.

    The test is "a sibling extends this stem by a *version*". The suffix has to
    parse as one: ``claude-opus-5-thinking`` names a different model from
    ``claude-opus-5`` rather than a newer cut of it, so a plain prefix match
    would hide a real choice. Family stems are themselves hyphenated outside
    xAI's naming, which is why this cannot key off "the stem has no hyphen".
    """
    prefix = name + "-"
    return any(
        other != name
        and other.startswith(prefix)
        and _VERSION_SUFFIX.match(other[len(prefix):])
        for other in names
    )


def list_model_presets(
    home_dir: str, include_aliases: bool = False
) -> List[Dict[str, Any]]:
    """Workspace model presets a hire can choose from. A preset is a YAML
    file in ``retinue_models/`` whose ``model:`` block is copied verbatim
    into the new profile; files without a ``model:`` block are skipped.

    Generic stems (``grok``) are hidden when versioned siblings
    (``grok-4.5``, ``grok-4.6``) exist, so the hire dropdown lists specific
    cloud models instead of a single bucket. Pass ``include_aliases=True``
    to resolve a legacy ``model: grok`` hire.
    """
    presets: List[Dict[str, Any]] = []
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
            presets.append(_preset_entry(stem, block))
    presets.sort(key=lambda p: p["name"])
    if not include_aliases:
        listed = {p["name"] for p in presets}
        presets = [p for p in presets if not _is_generic_alias(p["name"], listed)]
    return presets


def ensure_bundled_cloud_presets(home_dir: str) -> List[str]:
    """Write shipped versioned cloud presets that the workspace lacks.

    Never overwrites. If a legacy ``grok.yaml`` is present and ``grok-4.5``
    is not, copy it so existing pin/comments survive. Local / LAN presets
    stay operator-owned (they carry a host-specific ``base_url``).
    """
    dest = _models_dir(home_dir)
    os.makedirs(dest, exist_ok=True)
    written: List[str] = []

    alias = os.path.join(dest, "grok.yaml")
    promoted = os.path.join(dest, "grok-4.5.yaml")
    if os.path.isfile(alias) and not os.path.isfile(promoted):
        shutil.copy(alias, promoted)
        written.append("grok-4.5")

    try:
        bundled = sorted(os.listdir(BUNDLED_PRESETS_DIR))
    except OSError:
        bundled = []
    for fn in bundled:
        stem, ext = os.path.splitext(fn)
        if ext not in (".yaml", ".yml") or not stem:
            continue
        target = os.path.join(dest, fn)
        if os.path.isfile(target):
            continue
        src = os.path.join(BUNDLED_PRESETS_DIR, fn)
        if not os.path.isfile(src):
            continue
        shutil.copy(src, target)
        written.append(stem)
    return written


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
    known = ", ".join(p["name"] for p in list_model_presets(home_dir, include_aliases=True)) or "none"
    raise ValueError(f"unknown model preset {preset!r} (available: {known})")


def _replace_model_block(config_text: str, new_block: str) -> str:
    """Swap the top-level ``model:`` block; leave every other key in place."""
    new_block = (new_block or "").rstrip() + "\n"
    lines = (config_text or "").splitlines(keepends=True)
    start: Optional[int] = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None:
            if line.startswith("model:"):
                start = i
            continue
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = i
            break
    if start is None:
        rest = config_text or ""
        if rest and not rest.endswith("\n"):
            rest += "\n"
        return new_block + rest
    return "".join(lines[:start]) + new_block + "".join(lines[end:])


def _infer_preset_name(home_dir: str, block: str) -> Optional[str]:
    """Match a live ``model:`` block to a named preset (prefer versioned)."""
    if not block:
        return None
    fields = _fields_from_model_block(block)
    model = fields.get("default") or fields.get("model") or ""
    provider = fields.get("provider") or ""
    if not model:
        return None
    for include_aliases in (False, True):
        for preset in list_model_presets(home_dir, include_aliases=include_aliases):
            if preset.get("model") != model:
                continue
            if provider and preset.get("provider") and preset["provider"] != provider:
                continue
            return str(preset["name"])
    return None


def apply_model_preset(home_dir: str, slug: str, preset: str) -> Dict[str, Any]:
    """Rewrite ``profiles/<slug>/config.yaml``'s ``model:`` block in place.

    Used to switch an already-hired agent without a hand-edit. Raises
    ``ValueError`` on a bad slug/preset and ``KeyError`` if the profile
    directory does not exist. Does not touch SOUL.md or credentials.
    """
    slug = (slug or "").strip()
    preset = (preset or "").strip()
    if not slug or slug in _RESERVED:
        raise ValueError(f"cannot change model for profile {slug!r}")
    if not preset:
        raise ValueError("model preset is required")
    profile_dir = os.path.join(home_dir, "profiles", slug)
    if not os.path.isdir(profile_dir):
        raise KeyError(slug)
    block = _preset_model_block(home_dir, preset)
    cfg_path = os.path.join(profile_dir, "config.yaml")
    try:
        existing = open(cfg_path, encoding="utf-8").read()
        mode = os.stat(cfg_path).st_mode
    except OSError:
        existing = "agent:\n  tool_choice: auto\n"
        mode = None
    new_text = _replace_model_block(existing, block)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, cfg_path)
    if mode is not None:
        os.chmod(cfg_path, mode)

    meta_path = os.path.join(profile_dir, AGENT_META_FILENAME)
    meta: Dict[str, Any]
    try:
        with open(meta_path, encoding="utf-8") as f:
            loaded = json.load(f)
        meta = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        meta = {"display_name": slug, "slug": slug, "job": "", "how": ""}
    meta["slug"] = slug
    meta["model_preset"] = preset
    meta["model_switched_at"] = time.time()
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    for agent in list_agents(home_dir):
        if agent.get("slug") == slug:
            return agent
    raise KeyError(slug)


def _cache_key_for_slug(key: Any, slug: str) -> bool:
    return slug in str(key).split(":")


def evict_profile_agent_cache(runner: Any, slug: str) -> int:
    """Drop cached AIAgents whose session key names this profile.

    Room session keys carry the member as ``thread_id`` (and sometimes as
    the multiplex namespace). Plugin-shaped: uses the runner's existing
    ``_evict_cached_agent`` seam when present.
    """
    if runner is None or not slug:
        return 0
    cache = getattr(runner, "_agent_cache", None)
    if cache is None:
        return 0
    keys = [k for k in list(cache) if _cache_key_for_slug(k, slug)]
    evict = getattr(runner, "_evict_cached_agent", None)
    n = 0
    for key in keys:
        try:
            if callable(evict):
                evict(key)
            else:
                cache.pop(key, None)
            n += 1
        except Exception:
            logger.debug("Retinue: evict cache key %s failed", key, exc_info=True)
    return n


def scaffold_profile(
    home_dir: str,
    display_name: str,
    job: str,
    how: str,
    model_preset: Optional[str] = None,
    avatar_emoji: Any = None,
    avatar_color: Any = None,
    voice: Any = None,
    persona: Any = None,
) -> Dict[str, Any]:
    """Create ``profiles/<slug>/`` under *home_dir*. Raises ValueError on bad
    input (including an unknown *model_preset*), FileExistsError if the
    profile already exists."""
    from .identity import (
        normalize_avatar_color,
        normalize_avatar_emoji,
        normalize_persona,
        normalize_voice,
        persona_is_balanced,
    )

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

    # Validate identity/persona BEFORE creating anything, so a bad colour
    # leaves no half-made profile behind.
    emoji = normalize_avatar_emoji(avatar_emoji)
    color = normalize_avatar_color(avatar_color)
    voice_id = normalize_voice(voice)
    persona_obj = normalize_persona(persona)

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
        f.write(soul_template(display_name, job, how, persona=persona_obj))

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

    meta: Dict[str, Any] = {
        "display_name": display_name,
        "slug": slug,
        "job": job,
        "how": (how or "").strip(),
        "model_preset": model_preset,
        "archived": False,
        "created_at": time.time(),
    }
    if emoji is not None:
        meta["avatar_emoji"] = emoji
    if color is not None:
        meta["avatar_color"] = color
    if voice_id is not None:
        meta["voice"] = voice_id
    if not persona_is_balanced(persona_obj):
        meta["persona"] = persona_obj
    with open(os.path.join(profile_dir, AGENT_META_FILENAME), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return enrich_agent(dict(meta))


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
    listed = {p["name"] for p in list_model_presets(home_dir)}
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
        meta["archived"] = bool(meta.get("archived"))
        meta["has_soul"] = os.path.isfile(os.path.join(pdir, "SOUL.md"))
        meta["local_llm"] = profile_uses_local_llm(home_dir, name)
        meta["turn_timeout"] = int(turn_timeout_for(home_dir, name))
        block = _read_model_block(os.path.join(pdir, "config.yaml"))
        if not block:
            block = _read_model_block(os.path.join(home_dir, "config.yaml"))
        meta["model_summary"] = _summarize_model_block(block) if block else ""
        stored = (meta.get("model_preset") or "").strip()
        inferred = _infer_preset_name(home_dir, block)
        if stored and stored in listed:
            meta["model_preset"] = stored
        elif inferred:
            meta["model_preset"] = inferred
        elif stored:
            meta["model_preset"] = stored
        agents.append(enrich_agent(meta))
    return agents


def enrich_agent(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Add resolved identity / voice / persona. Safe on pre-change profiles."""
    from . import voice
    from .identity import normalize_persona, resolve_identity

    slug = str(meta.get("slug") or "")
    display = str(meta.get("display_name") or slug)
    emoji = meta.get("avatar_emoji")
    color = meta.get("avatar_color")
    stored_voice = meta.get("voice")
    meta["avatar_emoji"] = (
        emoji.strip() if isinstance(emoji, str) and emoji.strip() else None
    )
    meta["avatar_color"] = (
        color.strip().lower() if isinstance(color, str) and color.strip() else None
    )
    meta["voice"] = (
        stored_voice.strip()
        if isinstance(stored_voice, str) and stored_voice.strip()
        else None
    )
    try:
        meta["persona"] = normalize_persona(meta.get("persona"))
    except ValueError:
        from .identity import DEFAULT_PERSONA

        meta["persona"] = dict(DEFAULT_PERSONA)
    meta["identity"] = resolve_identity(
        slug, display, meta["avatar_emoji"], meta["avatar_color"]
    )
    meta["voice_resolved"] = voice.voice_for(slug, stored=meta["voice"])
    return meta


_SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _profile_dir(home_dir: str, slug: str) -> str:
    """Resolved profiles/<slug>/ — refuses reserved names and traversal."""
    slug = (slug or "").strip()
    if not slug or slug in _RESERVED:
        raise ValueError(f"cannot change profile {slug!r}")
    if not _SLUG_OK.fullmatch(slug):
        raise ValueError(f"invalid profile name {slug!r}")
    root = os.path.realpath(os.path.join(home_dir, "profiles"))
    path = os.path.realpath(os.path.join(root, slug))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError(f"invalid profile name {slug!r}")
    return path


def _load_meta(profile_dir: str, slug: str) -> Dict[str, Any]:
    path = os.path.join(profile_dir, AGENT_META_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            loaded.setdefault("display_name", slug)
            loaded.setdefault("slug", slug)
            loaded.setdefault("job", "")
            loaded.setdefault("how", "")
            return loaded
    except (OSError, ValueError):
        pass
    return {"display_name": slug, "slug": slug, "job": "", "how": ""}


_RESPONSE_ONLY = {
    "identity",
    "voice_resolved",
    "has_soul",
    "local_llm",
    "turn_timeout",
    "model_summary",
    "team",
    "busy",
    "cache_evicted",
    "online",
    "activation",
    "auth_status",
    "auth_provider",
    "auth_error",
}


def _write_meta(profile_dir: str, meta: Dict[str, Any]) -> None:
    path = os.path.join(profile_dir, AGENT_META_FILENAME)
    payload = {k: v for k, v in meta.items() if k not in _RESPONSE_ONLY}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _agent_or_raise(home_dir: str, slug: str) -> Dict[str, Any]:
    for agent in list_agents(home_dir):
        if agent.get("slug") == slug:
            return agent
    raise KeyError(slug)


_UNSET = object()


def update_agent(
    home_dir: str,
    slug: str,
    *,
    display_name: Optional[str] = None,
    job: Optional[str] = None,
    how: Optional[str] = None,
    archived: Optional[bool] = None,
    avatar_emoji: Any = _UNSET,
    avatar_color: Any = _UNSET,
    voice: Any = _UNSET,
    persona: Any = _UNSET,
) -> Dict[str, Any]:
    """Rewrite SOUL + meta for an existing hire. Slug stays put.

    Pass only the fields that should change. ``archived=True`` hides the
    bot from the hire-into-room list and the sidebar without deleting
    ``profiles/<slug>/``.
    """
    from .identity import (
        normalize_avatar_color,
        normalize_avatar_emoji,
        normalize_persona,
        normalize_voice,
        persona_is_balanced,
    )

    profile_dir = _profile_dir(home_dir, slug)
    if not os.path.isdir(profile_dir):
        raise KeyError(slug)
    if (
        display_name is None
        and job is None
        and how is None
        and archived is None
        and avatar_emoji is _UNSET
        and avatar_color is _UNSET
        and voice is _UNSET
        and persona is _UNSET
    ):
        raise ValueError("nothing to update")

    meta = _load_meta(profile_dir, slug)
    persona_changed = False
    if display_name is not None:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("agent name is required")
        meta["display_name"] = display_name
        persona_changed = True
    if job is not None:
        job = job.strip()
        if not job:
            raise ValueError("primary job is required")
        meta["job"] = job
        persona_changed = True
    if how is not None:
        meta["how"] = (how or "").strip()
        persona_changed = True
    if archived is not None:
        meta["archived"] = bool(archived)
    if avatar_emoji is not _UNSET:
        emoji = normalize_avatar_emoji(avatar_emoji)
        if emoji is None:
            meta.pop("avatar_emoji", None)
        else:
            meta["avatar_emoji"] = emoji
    if avatar_color is not _UNSET:
        color = normalize_avatar_color(avatar_color)
        if color is None:
            meta.pop("avatar_color", None)
        else:
            meta["avatar_color"] = color
    if voice is not _UNSET:
        voice_id = normalize_voice(voice)
        if voice_id is None:
            meta.pop("voice", None)
        else:
            meta["voice"] = voice_id
    if persona is not _UNSET:
        persona_obj = normalize_persona(persona)
        if persona_is_balanced(persona_obj):
            meta.pop("persona", None)
        else:
            meta["persona"] = persona_obj
        persona_changed = True
    meta["slug"] = slug
    meta["updated_at"] = time.time()

    if persona_changed:
        stored_persona = None
        if meta.get("persona"):
            try:
                stored_persona = normalize_persona(meta.get("persona"))
            except ValueError:
                stored_persona = None
        soul_path = os.path.join(profile_dir, "SOUL.md")
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(
                soul_template(
                    str(meta.get("display_name") or slug),
                    str(meta.get("job") or ""),
                    str(meta.get("how") or ""),
                    persona=stored_persona,
                )
            )
    _write_meta(profile_dir, meta)
    return _agent_or_raise(home_dir, slug)


def delete_agent(home_dir: str, slug: str) -> str:
    """Remove ``profiles/<slug>/``. Never touches the default profile."""
    profile_dir = _profile_dir(home_dir, slug)
    if not os.path.isdir(profile_dir):
        raise KeyError(slug)
    shutil.rmtree(profile_dir)
    return slug


def deactivate_hired_profile(slug: str, runner: Any = None) -> Dict[str, Any]:
    """Reverse of ``activate_hired_profile``: evict cache + pairing.

    Plugin-shaped: uses the runner's existing seams. Safe if the runner
    is missing — the profile is already gone from disk.
    """
    slug = (slug or "").strip()
    evicted = evict_profile_agent_cache(runner, slug)
    if runner is None or not slug:
        return {"evicted": evicted, "deregistered": False}

    stores = getattr(runner, "pairing_stores", None)
    if isinstance(stores, dict):
        stores.pop(slug, None)

    adapters = getattr(runner, "_profile_adapters", None)
    if isinstance(adapters, dict):
        adapters.pop(slug, None)

    try:
        from gateway.status import write_runtime_status

        served = {"default"}
        if isinstance(stores, dict):
            served.update(str(name) for name in stores if name)
        if isinstance(adapters, dict):
            served.update(str(name) for name in adapters if name)
        served.discard(slug)
        write_runtime_status(served_profiles=sorted(served))
    except Exception:
        logger.debug(
            "Retinue: runtime status update after evicting %s failed",
            slug,
            exc_info=True,
        )
    return {"evicted": evicted, "deregistered": True}
