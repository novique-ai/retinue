"""Speech humanization pipeline for Retinue rooms TTS.

Canonical agent text is never mutated. This module produces a separate
spoken script for Chatterbox / Kokoro / xAI / sidecar TTS.

Priority:
  1. A valid agent-supplied spoken_summary, if enabled.
  2. Deterministic normalization.
  3. Optional semantic rewrite when the turn is technically dense.
  4. On any failure, the previous successful stage (or a last-resort
     readable fallback) is spoken. TTS must not fail because the
     humanizer failed.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .config import HumanizeConfig, SpeechContext
from .deterministic import normalize_deterministic
from .semantic import rewrite_for_speech

logger = logging.getLogger(__name__)

# Cheap signals that a turn will sound like a screen reader if spoken raw.
_CODE_FENCE = re.compile(r"```")
_URL = re.compile(r"https?://", re.I)
_PATHY = re.compile(
    r"(?:src|lib|apps?|tests?|config|scripts?)/[A-Za-z0-9_./-]+\.[A-Za-z]{1,8}"
)
_ENV = re.compile(r"\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b")
_SNAKE = re.compile(r"\b[a-z]+(_[a-z0-9]+)+\b")
_JSONISH = re.compile(r"\{[^{}]{0,200}:[^{}]{0,200}\}")
_MD_HEAVY = re.compile(r"(^|\n)\s{0,3}#{1,6}\s|(^|\n)\s*[-*+]\s|\*\*", re.M)
_CMD = re.compile(r"\b(?:npm|pnpm|yarn|git|docker|curl|pytest|cargo)\b")
_HTTP = re.compile(r"\bHTTP\s*[1-5]\d{2}\b|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/")

_SPOKEN_SUMMARY_BAD = re.compile(r"```|https?://\S+\?\S+|forward slash", re.I)


def complexity_score(text: str) -> int:
    """How much a naive TTS engine would mangle this turn. 0 = speak as-is."""
    if not text:
        return 0
    score = 0
    if _CODE_FENCE.search(text):
        score += 3
    if _URL.search(text):
        score += 2
    if _PATHY.search(text):
        score += 2
    if _JSONISH.search(text):
        score += 2
    if _ENV.search(text):
        score += 1
    if _SNAKE.search(text):
        score += 1
    if _MD_HEAVY.search(text):
        score += 1
    if _CMD.search(text):
        score += 1
    if _HTTP.search(text):
        score += 1
    punct = sum(1 for ch in text if ch in "`{}[]<>|/\\$_*")
    if punct >= 8:
        score += 1
    if len(text) > 400:
        score += 1
    return score


def needs_semantic(text: str, deterministic: str = "") -> bool:
    if complexity_score(text) >= 3:
        return True
    if deterministic and any(
        token in deterministic.lower()
        for token in ("forward slash", "backslash", "underscore", "backtick")
    ):
        return True
    if deterministic and re.search(r"https?://|```|\{[\"']", deterministic):
        return True
    return False


def _validate_spoken_summary(
    summary: str, original: str, config: HumanizeConfig
) -> Optional[str]:
    text = (summary or "").strip()
    if not text:
        return None
    if len(text) > config.spoken_summary_max_chars:
        return None
    if _SPOKEN_SUMMARY_BAD.search(text):
        return None
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 8:
        return None
    try:
        from .semantic import validate_rewrite

        if validate_rewrite(original, text, text, config) is None:
            return None
    except Exception:
        return None
    try:
        cleaned = normalize_deterministic(text, config)
    except Exception:
        cleaned = text
    return cleaned or None


def _legacy_clean(text: str) -> str:
    try:
        from tools.tts_text_normalize import prepare_spoken_text

        return (prepare_spoken_text(text, max_chars=None) or "").strip()
    except Exception:
        return (text or "").strip()


def humanize_for_speech(
    text: str,
    context: SpeechContext | None = None,
    config: HumanizeConfig | None = None,
) -> str:
    """Return the spoken form of *text*. Never mutates *text*. Never raises."""
    raw = text if isinstance(text, str) else str(text or "")
    ctx = context or SpeechContext()
    try:
        cfg = config or HumanizeConfig.load()
    except Exception:
        # Keep env master-switch even if a numeric knob is garbage.
        try:
            cfg = HumanizeConfig.load(overrides={"semantic_timeout": 6.0})
        except Exception:
            cfg = HumanizeConfig()

    if not raw.strip():
        return ""

    if not cfg.enabled:
        return _legacy_clean(raw)

    try:
        if cfg.spoken_summaries and ctx.spoken_summary:
            accepted = _validate_spoken_summary(ctx.spoken_summary, raw, cfg)
            if accepted:
                return accepted
    except Exception:
        logger.debug("speech humanize: spoken_summary rejected", exc_info=True)

    if not cfg.deterministic:
        spoken = _legacy_clean(raw)
    else:
        try:
            spoken = normalize_deterministic(raw, cfg)
        except Exception:
            logger.debug("speech humanize: deterministic failed", exc_info=True)
            spoken = _legacy_clean(raw)

    # Empty is a valid script (an itinerary-only turn). Do not treat it as
    # failure and resurrect the canonical Markdown.
    if spoken is None:
        spoken = _legacy_clean(raw)

    if (
        cfg.semantic
        and spoken
        and needs_semantic(raw, spoken)
    ):
        try:
            rewritten = rewrite_for_speech(raw, spoken, cfg, ctx)
            if rewritten:
                spoken = rewritten
        except Exception:
            logger.debug("speech humanize: semantic failed", exc_info=True)

    return (spoken or "").strip()
