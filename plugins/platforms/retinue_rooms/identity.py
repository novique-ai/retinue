"""Per-agent identity: palette, derivation, and stored-field validation.

The backend owns the palette keys and the hash so every surface (sidebar,
transcript, mention chip, hire dropdown) agrees without the frontend
duplicating it. See CONTRACT-identity.md.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

# Twelve keys, this order. Do not reorder — derivation is index-into-this-list.
# Collisions are accepted. Stability beats distinctness: an agent's colour
# must not change because somebody else was hired.
PALETTE: List[str] = [
    "indigo",
    "teal",
    "amber",
    "rose",
    "violet",
    "lime",
    "cyan",
    "orange",
    "emerald",
    "fuchsia",
    "sky",
    "red",
]
_PALETTE_SET = frozenset(PALETTE)

DIALS = {
    "warmth": ("terse", "balanced", "warm"),
    "verbosity": ("brief", "balanced", "thorough"),
    "formality": ("casual", "balanced", "formal"),
}
DEFAULT_PERSONA: Dict[str, str] = {key: "balanced" for key in DIALS}

# One short phrase per non-balanced dial. All-balanced contributes nothing.
_PERSONA_PHRASES = {
    "warmth": {
        "terse": "Stay matter-of-fact; skip small talk.",
        "warm": "Be warm and encouraging.",
    },
    "verbosity": {
        "brief": "Keep replies short.",
        "thorough": "Be thorough; cover the relevant detail.",
    },
    "formality": {
        "casual": "Write casually.",
        "formal": "Write formally.",
    },
}

_MAX_EMOJI = 16
_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def stable_index(slug: str, n: int) -> int:
    """Deterministic index in ``range(n)`` from *slug*.

    Uses SHA-1 of the UTF-8 slug, not Python's ``hash()``. ``hash()`` is
    salted per process, so the same agent would get a different colour
    every time the gateway restarts.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    digest = hashlib.sha1((slug or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % n


def display_initial(display_name: str) -> str:
    """First character of *display_name*, uppercased. Always non-empty."""
    name = (display_name or "").strip()
    if not name:
        return "?"
    return name[0].upper()


def derived_color(slug: str) -> str:
    if not slug:
        return PALETTE[0]
    return PALETTE[stable_index(slug, len(PALETTE))]


def normalize_avatar_emoji(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("avatar_emoji must be a string or null")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_EMOJI or any(ch.isspace() for ch in text):
        raise ValueError("avatar_emoji must be a short glyph, not a sentence")
    return text


def normalize_avatar_color(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("avatar_color must be a palette key or null")
    text = value.strip().lower()
    if not text:
        return None
    if text not in _PALETTE_SET:
        raise ValueError(
            f"invalid avatar_color {value!r} (must be a palette key or null)"
        )
    return text


def normalize_voice(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("voice must be a voice id or null")
    text = value.strip()
    if not text:
        return None
    if not _VOICE_RE.fullmatch(text):
        raise ValueError("voice must be a short voice id or null")
    return text.lower()


def normalize_persona(value: Any) -> Dict[str, str]:
    if value is None:
        return dict(DEFAULT_PERSONA)
    if not isinstance(value, dict):
        raise ValueError("persona must be an object")
    out = dict(DEFAULT_PERSONA)
    for key, allowed in DIALS.items():
        if key not in value or value[key] is None:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(f"invalid {key} {raw!r}")
        text = raw.strip().lower()
        if text not in allowed:
            raise ValueError(
                f"invalid {key} {raw!r} (allowed: {', '.join(allowed)})"
            )
        out[key] = text
    return out


def persona_is_balanced(persona: Optional[Dict[str, str]]) -> bool:
    if not persona:
        return True
    return all(persona.get(key, "balanced") == "balanced" for key in DIALS)


def persona_soul_lines(persona: Optional[Dict[str, Any]]) -> List[str]:
    """Phrases for non-balanced dials. Empty when all-balanced or unset."""
    if not persona:
        return []
    lines: List[str] = []
    for key in DIALS:
        phrase = _PERSONA_PHRASES[key].get(str(persona.get(key) or "balanced"))
        if phrase:
            lines.append(phrase)
    return lines


def resolve_identity(
    slug: str,
    display_name: str,
    avatar_emoji: Any = None,
    avatar_color: Any = None,
) -> Dict[str, Any]:
    emoji = None
    if isinstance(avatar_emoji, str) and avatar_emoji.strip():
        emoji = avatar_emoji.strip()
    stored_color = ""
    if isinstance(avatar_color, str):
        stored_color = avatar_color.strip().lower()
    if stored_color in _PALETTE_SET:
        color = stored_color
        source = "override"
    else:
        color = derived_color(slug)
        source = "derived"
    return {
        "emoji": emoji,
        "initial": display_initial(display_name or slug),
        "color": color,
        "color_source": source,
    }


def palette_payload() -> Dict[str, List[str]]:
    return {"colors": list(PALETTE)}
