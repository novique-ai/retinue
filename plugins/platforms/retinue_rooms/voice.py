"""Transcript-preserving voice I/O for Retinue rooms.

A room stays a shared transcript. This module turns audio into text (STT)
and text into audio (TTS). It does not talk to the turn engine.

Backends (``RETINUE_VOICE_BACKEND``):

- ``xai`` (Track A, default) — ``POST /v1/stt`` and ``POST /v1/tts``.
  Credentials: ``XAI_API_KEY`` if set, else Hermes xAI OAuth
  (``tools.xai_http.resolve_xai_http_credentials``).
- ``openai`` (Track B) — OpenAI-compatible
  ``/audio/transcriptions`` + ``/audio/speech`` at
  ``RETINUE_VOICE_BASE_URL`` (e.g. claymore-1 sidecar ``http://10.44.0.13:8104/v1``).

No upstream-core edits. The rooms adapter is the only caller.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

XAI_DEFAULT_BASE = "https://api.x.ai/v1"
DEFAULT_LANGUAGE = "en"

# Distinct built-in xAI voices so six staff don't share one mouth.
# Track B sidecar may ignore the id and still return audio.
STAFF_VOICES: Dict[str, str] = {
    "admin": "eve",
    "envoy": "rigel",
    "janitor": "lux",
    "scout": "ursa",
    "editor": "leo",
    "scribe": "celeste",
}

_FALLBACK_VOICES = ("eve", "leo", "rex", "rigel", "ursa", "celeste", "lux", "iris")


class VoiceError(ValueError):
    """User-facing voice failure (empty audio, provider 4xx, missing creds)."""


def backend_name() -> str:
    raw = (os.getenv("RETINUE_VOICE_BACKEND") or "xai").strip().lower()
    if raw in ("openai", "local", "sidecar"):
        return "openai"
    return "xai"


def openai_base_url() -> str:
    return (os.getenv("RETINUE_VOICE_BASE_URL") or "").strip().rstrip("/")


def voice_for(speaker: str) -> str:
    slug = (speaker or "").strip().lower()
    override = _voice_map_override()
    if slug in override:
        return override[slug]
    if slug in STAFF_VOICES:
        return STAFF_VOICES[slug]
    if not slug:
        return _FALLBACK_VOICES[0]
    return _FALLBACK_VOICES[sum(ord(c) for c in slug) % len(_FALLBACK_VOICES)]


def _voice_map_override() -> Dict[str, str]:
    raw = (os.getenv("RETINUE_VOICE_MAP") or "").strip()
    out: Dict[str, str] = {}
    if not raw:
        return out
    for part in raw.split(","):
        if ":" not in part:
            continue
        key, val = part.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key and val:
            out[key] = val
    return out


def status() -> Dict[str, Any]:
    name = backend_name()
    ready = True
    detail = ""
    if name == "openai":
        base = openai_base_url()
        ready = bool(base)
        detail = base or "RETINUE_VOICE_BASE_URL is empty"
    else:
        try:
            creds = _xai_creds()
            ready = bool(creds.get("api_key"))
            detail = creds.get("provider") or ""
            if not ready:
                detail = "no xAI credentials (XAI_API_KEY or xai-oauth)"
        except Exception as e:
            ready = False
            detail = str(e)
    voices = dict(STAFF_VOICES)
    voices.update(_voice_map_override())
    return {
        "backend": name,
        "ready": ready,
        "detail": detail,
        "voices": voices,
    }


def transcribe(data: bytes, filename: str = "speech.wav") -> str:
    """Return transcript text. Raises VoiceError on failure."""
    if not data:
        raise VoiceError("empty audio")
    suffix = Path(filename or "speech.wav").suffix or ".wav"
    fd, path = tempfile.mkstemp(prefix="retinue-stt-", suffix=suffix)
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        if backend_name() == "openai":
            return _transcribe_openai(path, filename)
        return _transcribe_xai(path, filename)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


def synthesize(text: str, speaker: str = "") -> bytes:
    """Return audio bytes (mp3 or wav). Raises VoiceError on failure."""
    spoken = (text or "").strip()
    if not spoken:
        raise VoiceError("empty text")
    voice = voice_for(speaker)
    if backend_name() == "openai":
        return _synthesize_openai(spoken, voice)
    return _synthesize_xai(spoken, voice)


def _xai_creds() -> Dict[str, str]:
    """Prefer an explicit API key — OAuth may 402 billed STT/TTS endpoints."""
    try:
        from hermes_cli.config import get_env_value
    except Exception:
        get_env_value = lambda _k: os.getenv(_k)  # noqa: E731

    direct = str(get_env_value("XAI_API_KEY") or os.getenv("XAI_API_KEY") or "").strip()
    if direct:
        base = str(
            get_env_value("XAI_BASE_URL") or os.getenv("XAI_BASE_URL") or XAI_DEFAULT_BASE
        ).strip().rstrip("/")
        return {"provider": "xai", "api_key": direct, "base_url": base}
    try:
        from tools.xai_http import resolve_xai_http_credentials

        creds = resolve_xai_http_credentials()
        return {
            "provider": str(creds.get("provider") or "xai-oauth"),
            "api_key": str(creds.get("api_key") or "").strip(),
            "base_url": str(creds.get("base_url") or XAI_DEFAULT_BASE).rstrip("/"),
        }
    except Exception as e:
        logger.debug("Retinue voice: xAI credential resolve failed: %s", e)
        return {"provider": "", "api_key": "", "base_url": XAI_DEFAULT_BASE}


def _user_agent() -> str:
    try:
        from tools.xai_http import hermes_xai_user_agent

        return hermes_xai_user_agent()
    except Exception:
        return "retinue-rooms/voice"


def _http_post(
    url: str,
    *,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Any:
    """Thin requests wrapper so tests can monkeypatch ``_http_post``."""
    import requests

    if files is not None:
        return requests.post(url, headers=headers, files=files, data=json_body or {}, timeout=timeout)
    return requests.post(url, headers=headers, json=json_body, timeout=timeout)


def _transcribe_xai(path: str, filename: str) -> str:
    creds = _xai_creds()
    if not creds.get("api_key"):
        raise VoiceError("no xAI credentials for STT")
    base = creds["base_url"] or XAI_DEFAULT_BASE
    headers = {
        "Authorization": f"Bearer {creds['api_key']}",
        "User-Agent": _user_agent(),
    }
    with open(path, "rb") as fh:
        resp = _http_post(
            f"{base}/stt",
            headers=headers,
            files={"file": (filename or "speech.wav", fh, "application/octet-stream")},
            json_body={"language": DEFAULT_LANGUAGE, "format": "true"},
            timeout=90.0,
        )
    if resp.status_code >= 400:
        raise VoiceError(_provider_error("xAI STT", resp))
    return _extract_transcript(resp)


def _synthesize_xai(text: str, voice_id: str) -> bytes:
    creds = _xai_creds()
    if not creds.get("api_key"):
        raise VoiceError("no xAI credentials for TTS")
    base = creds["base_url"] or XAI_DEFAULT_BASE
    headers = {
        "Authorization": f"Bearer {creds['api_key']}",
        "User-Agent": _user_agent(),
        "Content-Type": "application/json",
    }
    resp = _http_post(
        f"{base}/tts",
        headers=headers,
        json_body={"text": text, "voice_id": voice_id, "language": DEFAULT_LANGUAGE},
        timeout=90.0,
    )
    if resp.status_code >= 400:
        raise VoiceError(_provider_error("xAI TTS", resp))
    audio = resp.content or b""
    if not audio:
        raise VoiceError("xAI TTS returned empty audio")
    return audio


def _transcribe_openai(path: str, filename: str) -> str:
    base = openai_base_url()
    if not base:
        raise VoiceError("RETINUE_VOICE_BASE_URL is not set")
    headers = _openai_headers()
    with open(path, "rb") as fh:
        resp = _http_post(
            f"{base}/audio/transcriptions",
            headers=headers,
            files={"file": (filename or "speech.wav", fh, "application/octet-stream")},
            json_body={"model": os.getenv("RETINUE_VOICE_STT_MODEL") or "whisper-1"},
            timeout=120.0,
        )
    if resp.status_code >= 400:
        raise VoiceError(_provider_error("local STT", resp))
    return _extract_transcript(resp)


def _synthesize_openai(text: str, voice: str) -> bytes:
    base = openai_base_url()
    if not base:
        raise VoiceError("RETINUE_VOICE_BASE_URL is not set")
    headers = _openai_headers()
    headers["Content-Type"] = "application/json"
    resp = _http_post(
        f"{base}/audio/speech",
        headers=headers,
        json_body={
            "model": os.getenv("RETINUE_VOICE_TTS_MODEL") or "tts-1",
            "voice": voice,
            "input": text,
        },
        timeout=90.0,
    )
    if resp.status_code >= 400:
        raise VoiceError(_provider_error("local TTS", resp))
    audio = resp.content or b""
    if not audio:
        raise VoiceError("local TTS returned empty audio")
    return audio


def _openai_headers() -> Dict[str, str]:
    key = (os.getenv("RETINUE_VOICE_API_KEY") or "not-needed").strip()
    return {"Authorization": f"Bearer {key}"}


def _extract_transcript(resp: Any) -> str:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    raw = resp.content or b""
    if "application/json" in ctype or raw[:1] in (b"{", b"["):
        try:
            payload = resp.json()
        except Exception:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        text = (
            payload.get("text")
            or payload.get("transcript")
            or payload.get("transcription")
            or ""
        )
        if isinstance(text, dict):
            text = text.get("text") or ""
        text = str(text).strip()
    else:
        text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise VoiceError("STT returned empty transcript")
    return text


def _provider_error(label: str, resp: Any) -> str:
    body = ""
    try:
        body = (resp.text or "")[:300]
    except Exception:
        body = ""
    return f"{label} HTTP {resp.status_code}: {body or 'no body'}"


# Optional hook for tests that want to stub the whole backend.
transcribe_fn: Optional[Callable[[bytes, str], str]] = None
synthesize_fn: Optional[Callable[[str, str], bytes]] = None


def transcribe_dispatch(data: bytes, filename: str = "speech.wav") -> str:
    fn = transcribe_fn
    return fn(data, filename) if fn is not None else transcribe(data, filename)


def synthesize_dispatch(text: str, speaker: str = "") -> bytes:
    fn = synthesize_fn
    return fn(text, speaker) if fn is not None else synthesize(text, speaker)
