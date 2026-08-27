"""TTS preprocessing for Retinue rooms — speak the meaning, not the serialization.

The UI, logs, and agent history keep the canonical response. Chatterbox,
Kokoro, xAI, and the OpenAI-compatible sidecar consume only the spoken
script produced here.

    humanize_for_speech(text, context=None) -> str

See ``retinue/VOICE.md`` (Speech humanization) for configuration, fallback,
and how to add a rule.
"""

from .config import HumanizeConfig, SpeechContext
from .pipeline import complexity_score, humanize_for_speech, needs_semantic

__all__ = [
    "HumanizeConfig",
    "SpeechContext",
    "complexity_score",
    "humanize_for_speech",
    "needs_semantic",
]
