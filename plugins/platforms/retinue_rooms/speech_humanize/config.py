"""Knobs for the rooms speech-humanization layer.

Rooms voice is env-driven (see ``retinue/VOICE.md``). Optional
``tts.humanize`` in ``config.yaml`` is the same schema; a set env var
wins, matching the other ``RETINUE_VOICE_*`` knobs.

Defaults strongly favor natural speech. Semantic rewriting still only
runs when the complexity detector says it is worth the extra call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional


def _safe_float(raw: object, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _yaml_humanize() -> Mapping[str, Any]:
    try:
        from hermes_cli.config import load_config

        tts = (load_config() or {}).get("tts") or {}
        block = tts.get("humanize") if isinstance(tts, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class HumanizeConfig:
    """Independent on/off switches for the spoken-script pipeline."""

    enabled: bool = True
    deterministic: bool = True
    semantic: bool = True
    spoken_summaries: bool = True
    code_blocks: bool = True
    urls: bool = True
    semantic_timeout: float = 6.0
    spoken_summary_max_chars: int = 2000
    semantic_max_input_chars: int = 6000

    @classmethod
    def load(cls, overrides: Optional[Mapping[str, Any]] = None) -> "HumanizeConfig":
        yaml = _yaml_humanize()
        env = os.environ
        data = {**yaml, **(overrides or {})}

        def flag(name: str, env_name: str, default: bool) -> bool:
            if env_name in env:
                return _truthy(env.get(env_name), default)
            raw = data.get(name)
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return _truthy(raw, default)
            return default

        enabled = flag("enabled", "RETINUE_SPEECH_HUMANIZE", True)
        return cls(
            enabled=enabled,
            deterministic=flag("deterministic", "RETINUE_SPEECH_HUMANIZE_DETERMINISTIC", True),
            semantic=flag("semantic", "RETINUE_SPEECH_HUMANIZE_SEMANTIC", True),
            spoken_summaries=flag(
                "spoken_summaries", "RETINUE_SPEECH_HUMANIZE_SPOKEN_SUMMARY", True
            ),
            code_blocks=flag("code_blocks", "RETINUE_SPEECH_HUMANIZE_CODE_BLOCKS", True),
            urls=flag("urls", "RETINUE_SPEECH_HUMANIZE_URLS", True),
            semantic_timeout=_safe_float(
                env.get("RETINUE_SPEECH_HUMANIZE_SEMANTIC_TIMEOUT")
                or data.get("semantic_timeout"),
                6.0,
            ),
            spoken_summary_max_chars=int(
                data.get("spoken_summary_max_chars") or 2000
            ),
            semantic_max_input_chars=int(
                data.get("semantic_max_input_chars") or 6000
            ),
        )


@dataclass(frozen=True)
class SpeechContext:
    """Optional extras for one humanize call. Never mutates canonical text."""

    spoken_summary: str | None = None
    # Injected rewriter for tests. Signature: (original, deterministic) -> str.
    rewriter: Any = None
