"""Mirror retainer identity into the profile's upstream ``ui_meta`` block.

Retinue keeps a retainer's identity — the three-field brief (name / job /
how it works) plus avatar, voice and persona — in
``profiles/<slug>/retinue-agent.json``.  That file is **canonical** and stays
so: the hire flow writes it, ``PATCH /agents`` edits it, ``GET /agents``
reads it, and SOUL.md is generated from it.

The problem it leaves is that nothing else on the install can see any of it.
Upstream Hermes carries a small, server-synced ``ui_meta`` block in
``profiles/<slug>/profile.yaml`` — written by the ``profiles.configure`` RPC,
echoed by ``profiles.list`` on every roster paint, and namespaced per
consumer (the desktop's Bot Mode uses ``ui_meta['hermes-bots']``).  Any stock
client pointed at this gateway paints its roster from that call.  A retainer
with no ``profile.yaml`` shows up there as a bare directory name.

So this module write-throughs the identity into ``ui_meta['retinue']`` (plus
the two generic fields upstream reads for *every* client — ``display_name``
and ``description``) on hire, on every identity edit, and once at gateway
start for retainers hired before this existed.  One-way, best-effort, and
idempotent: the rooms store remains the source of truth, the mirror is a
derived projection of it, and a failed mirror never fails a hire.

Deliberate boundaries
---------------------
* **Never write ``ui_meta['hermes-bots']``.**  ``tools/bot_mode_probe`` treats
  the presence of that namespace on *any* profile as "this install is
  Bot-Mode-managed" and starts injecting the teammate-messaging protocol into
  Bot Chat prompts.  Squatting it would fight the real plugin and change
  prompt content.  Our namespace is ``retinue`` and nothing else.
* **Foreign namespaces and unrelated top-level keys are preserved** — the
  block is shared, so the write is a key-wise merge of our namespace only,
  the same shape ``profiles.configure`` applies.
* **Size-capped** like upstream: ``ui_meta`` rides ``profiles.list`` on every
  paint, so the merged block is held under 64 KB (long ``how`` text is
  clamped in the mirror only — the canonical store keeps it whole).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Our slot inside the shared ``ui_meta`` dict.
NAMESPACE = "retinue"

#: Same ceiling ``profiles.configure`` enforces on an incoming ui_meta.
MAX_UI_META_BYTES = 65536

#: Mirror-only clamps.  ``retinue-agent.json`` keeps the untruncated text.
_MAX_JOB = 2_000
_MAX_HOW = 8_000

_PROFILE_YAML = "profile.yaml"


def _sized(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return MAX_UI_META_BYTES + 1


def build_block(meta: Dict[str, Any]) -> Dict[str, Any]:
    """The ``ui_meta['retinue']`` payload for one retainer.

    Derived purely from *meta* (a ``retinue-agent.json`` dict), so the same
    input always produces the same block — no timestamps, which is what makes
    the migration sweep a genuine no-op on a second run.
    """
    from .identity import normalize_persona, persona_is_balanced, resolve_identity

    slug = str(meta.get("slug") or "").strip()
    display_name = str(meta.get("display_name") or slug).strip() or slug
    job = str(meta.get("job") or "").strip()
    how = str(meta.get("how") or "").strip()

    emoji = meta.get("avatar_emoji")
    emoji = emoji.strip() if isinstance(emoji, str) and emoji.strip() else None
    color = meta.get("avatar_color")
    color = color.strip().lower() if isinstance(color, str) and color.strip() else None
    identity = resolve_identity(slug, display_name, emoji, color)

    block: Dict[str, Any] = {
        "schema": 1,
        "source": "retinue-rooms",
        "slug": slug,
        "display_name": display_name,
        "job": job[:_MAX_JOB],
        "how": how[:_MAX_HOW],
        "archived": bool(meta.get("archived")),
        "initial": identity["initial"],
        "avatar_color": identity["color"],
        "avatar_color_source": identity["color_source"],
    }
    if identity["emoji"]:
        block["avatar_emoji"] = identity["emoji"]
    voice = meta.get("voice")
    if isinstance(voice, str) and voice.strip():
        block["voice"] = voice.strip()
    preset = meta.get("model_preset")
    if isinstance(preset, str) and preset.strip():
        block["model_preset"] = preset.strip()
    try:
        persona = normalize_persona(meta.get("persona"))
    except ValueError:
        persona = None
    if persona and not persona_is_balanced(persona):
        block["persona"] = persona
    return block


def read_profile_yaml(profile_dir: str) -> Dict[str, Any]:
    """Parse ``profile.yaml``.  A missing or corrupt file reads as ``{}``.

    Same tolerance as ``hermes_cli.profiles.read_profile_meta``: one unusable
    profile must not break the sweep for the rest.
    """
    path = os.path.join(profile_dir, _PROFILE_YAML)
    try:
        import yaml

        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
    except Exception:  # OSError, or yaml's own parse errors
        return {}
    return loaded if isinstance(loaded, dict) else {}


def project(existing: Dict[str, Any], meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Full desired ``profile.yaml`` content, or ``None`` if it cannot fit.

    Merges our namespace into whatever is already there and leaves every
    other key — foreign ui_meta namespaces included — untouched.
    """
    current = existing.get("ui_meta")
    ui_meta: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}

    block = build_block(meta)
    ui_meta[NAMESPACE] = block
    if _sized(ui_meta) > MAX_UI_META_BYTES:
        # ``how`` is the only field a user can make arbitrarily large.
        block = {k: v for k, v in block.items() if k != "how"}
        ui_meta[NAMESPACE] = block
        if _sized(ui_meta) > MAX_UI_META_BYTES:
            return None

    out = dict(existing)
    # Generic fields every client reads (profiles.list rows, `hermes profile
    # list`, the desktop roster) — the half that makes a retainer legible
    # without any Retinue awareness.
    display_name = block["display_name"]
    if display_name:
        out["display_name"] = display_name
    job = block["job"]
    if job:
        # description_auto=False marks it curated so the profile describer
        # never overwrites the retainer's role line with an LLM summary.
        out["description"] = job
        out["description_auto"] = False
    out["ui_meta"] = ui_meta
    return out


def mirror(profile_dir: str, meta: Dict[str, Any]) -> bool:
    """Write the projection for *meta* into ``profile_dir/profile.yaml``.

    Returns True when the file was actually rewritten.  A no-op when the
    content already matches — that is what keeps the sweep idempotent and
    keeps mtime stable for anything watching the file.
    """
    if not os.path.isdir(profile_dir):
        return False
    existing = read_profile_yaml(profile_dir)
    desired = project(existing, meta)
    if desired is None:
        logger.debug(
            "Retinue ui_meta: projection for %s exceeds the %d byte cap — skipped",
            profile_dir,
            MAX_UI_META_BYTES,
        )
        return False
    if desired == existing and os.path.isfile(os.path.join(profile_dir, _PROFILE_YAML)):
        return False
    from utils import atomic_yaml_write

    atomic_yaml_write(os.path.join(profile_dir, _PROFILE_YAML), desired, sort_keys=False)
    return True


def mirror_quietly(profile_dir: str, meta: Dict[str, Any]) -> bool:
    """``mirror`` that never raises — the write-through call sites use this.

    A projection is a convenience for other clients; it must not be able to
    fail a hire, an edit or a model switch.
    """
    try:
        return mirror(profile_dir, meta)
    except Exception:
        logger.debug("Retinue ui_meta: mirror for %s failed", profile_dir, exc_info=True)
        return False


def sync_all(home_dir: str) -> List[str]:
    """Back-fill every retainer's ``ui_meta`` under *home_dir*.

    Only profiles carrying a ``retinue-agent.json`` are touched: a hand-made
    Hermes profile is not a retainer and gets no ``profile.yaml`` from us.
    Returns the slugs whose file actually changed — empty on a second run.
    """
    from .hire import AGENT_META_FILENAME

    profiles_dir = os.path.join(home_dir, "profiles")
    changed: List[str] = []
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        return changed
    for name in names:
        pdir = os.path.join(profiles_dir, name)
        meta_path = os.path.join(pdir, AGENT_META_FILENAME)
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        loaded.setdefault("slug", name)
        loaded.setdefault("display_name", name)
        if mirror_quietly(pdir, loaded):
            changed.append(name)
    return changed
