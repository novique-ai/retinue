"""Voice backend selection + STT/TTS dispatch (no live provider)."""

from __future__ import annotations

import pytest

from . import voice


class _FakeResp:
    def __init__(self, status=200, content=b"", json_data=None, text="", headers=None):
        self.status_code = status
        self.content = content
        self._json = json_data
        self.text = text or (content.decode("utf-8", errors="replace") if content else "")
        self.headers = headers or {}
        if json_data is not None and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = "application/json"

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def test_voice_for_staff_and_override(monkeypatch):
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    assert voice.voice_for("scout") == "ursa"
    assert voice.voice_for("admin") == "eve"
    monkeypatch.setenv("RETINUE_VOICE_MAP", "scout:helix,newbie:iris")
    assert voice.voice_for("scout") == "helix"
    assert voice.voice_for("newbie") == "iris"


def test_voice_for_ignores_stored_staff_slug(tmp_path, monkeypatch):
    """A leftover hire/edit save of another agent's slug must not reach xAI."""
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    home = str(tmp_path)
    (tmp_path / "profiles" / "admin").mkdir(parents=True)
    (tmp_path / "profiles" / "admin" / "retinue-agent.json").write_text(
        '{"display_name":"Carlos","slug":"admin","voice":"editor"}',
        encoding="utf-8",
    )
    assert voice.voice_for("admin", home_dir=home) == "eve"
    assert voice.voice_for("admin", stored="scribe") == "eve"
    monkeypatch.setenv("RETINUE_VOICE_MAP", "admin:editor")
    assert voice.voice_for("admin", home_dir=home) == "eve"


def test_status_lists_available_narrators(monkeypatch):
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    st = voice.status()
    assert st["available"] == list(voice.AVAILABLE_VOICES)
    assert "editor" not in st["available"]
    assert "admin" not in st["available"]
    assert set(st["available"]).issubset(voice.NARRATOR_VOICES)


def test_backend_name_aliases(monkeypatch):
    monkeypatch.delenv("RETINUE_VOICE_BACKEND", raising=False)
    assert voice.backend_name() == "xai"
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "sidecar")
    assert voice.backend_name() == "openai"
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "OPENAI")
    assert voice.backend_name() == "openai"


def test_status_openai_requires_base_url(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "openai")
    monkeypatch.delenv("RETINUE_VOICE_BASE_URL", raising=False)
    st = voice.status()
    assert st["backend"] == "openai"
    assert st["ready"] is False
    monkeypatch.setenv("RETINUE_VOICE_BASE_URL", "http://192.0.2.10:8104/v1")
    st = voice.status()
    assert st["ready"] is True
    assert "8104" in st["detail"]


def test_transcribe_xai_posts_stt(monkeypatch, tmp_path):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai", "api_key": "test-key", "base_url": "https://api.x.ai/v1"},
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return _FakeResp(json_data={"text": "hello room"})

    monkeypatch.setattr(voice, "_http_post", fake_post)
    text = voice.transcribe(b"RIFF....", "clip.wav")
    assert text == "hello room"
    assert seen["url"] == "https://api.x.ai/v1/stt"
    assert seen["headers"]["Authorization"] == "Bearer test-key"


def test_synthesize_xai_posts_tts(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai", "api_key": "test-key", "base_url": "https://api.x.ai/v1"},
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json_body")
        return _FakeResp(content=b"ID3fake-mp3")

    monkeypatch.setattr(voice, "_http_post", fake_post)
    audio = voice.synthesize("Hello from scout", "scout")
    assert audio == b"ID3fake-mp3"
    assert seen["url"] == "https://api.x.ai/v1/tts"
    assert seen["json"]["voice_id"] == "ursa"
    assert seen["json"]["text"] == "Hello from scout"


def test_synthesize_does_not_forward_a_stored_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai", "api_key": "test-key", "base_url": "https://api.x.ai/v1"},
    )
    (tmp_path / "profiles" / "admin").mkdir(parents=True)
    (tmp_path / "profiles" / "admin" / "retinue-agent.json").write_text(
        '{"slug":"admin","voice":"editor"}',
        encoding="utf-8",
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen["json"] = kwargs.get("json_body")
        return _FakeResp(content=b"ID3fake-mp3")

    monkeypatch.setattr(voice, "_http_post", fake_post)
    voice.synthesize("hello", "admin", home_dir=str(tmp_path))
    assert seen["json"]["voice_id"] == "eve"


def test_openai_backend_uses_sidecar_urls(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "openai")
    monkeypatch.setenv("RETINUE_VOICE_BASE_URL", "http://192.0.2.10:8104/v1")
    seen = []

    def fake_post(url, **kwargs):
        seen.append(url)
        if url.endswith("/transcriptions"):
            return _FakeResp(json_data={"text": "local hi"})
        return _FakeResp(content=b"wav-bytes")

    monkeypatch.setattr(voice, "_http_post", fake_post)
    assert voice.transcribe(b"xxxx", "a.wav") == "local hi"
    assert voice.synthesize("reply", "editor") == b"wav-bytes"
    assert seen[0].endswith("/audio/transcriptions")
    assert seen[1].endswith("/audio/speech")


def test_status_xai_survives_unscoped_get_env_value(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai-oauth", "api_key": "oauth-token", "base_url": "https://api.x.ai/v1"},
    )
    st = voice.status()
    assert st["ready"] is True
    assert st["detail"] == "xai-oauth"


def test_empty_audio_and_text_fail_loud():
    with pytest.raises(voice.VoiceError, match="empty audio"):
        voice.transcribe(b"")
    with pytest.raises(voice.VoiceError, match="empty text"):
        voice.synthesize("   ")


def test_post_user_audio_reuses_message_cycle(monkeypatch, tmp_path):
    from .adapter import RetinueRoomsAdapter

    captured = {}

    def fake_transcribe(data, filename):
        captured["data"] = data
        captured["filename"] = filename
        return "hello from the mic"

    monkeypatch.setattr(voice, "transcribe_fn", fake_transcribe)

    class _Cfg:
        extra = {}

    adapter = RetinueRoomsAdapter(_Cfg())
    adapter._loop = object()  # not used when we stub post_user_message

    def fake_post(room_id, text, from_name, wait=False):
        captured["room"] = room_id
        captured["text"] = text
        captured["from"] = from_name
        return {"seq": 3, "planned": ["scout"]}

    adapter.post_user_message = fake_post  # type: ignore[method-assign]
    result = adapter.post_user_audio("r1", b"wav", filename="mic.wav", from_name="Mark")
    assert result["text"] == "hello from the mic"
    assert result["planned"] == ["scout"]
    assert captured["text"] == "hello from the mic"
    assert captured["filename"] == "mic.wav"


def test_post_user_audio_prefixes_composer_draft(monkeypatch, tmp_path):
    from .adapter import RetinueRoomsAdapter

    captured = {}

    monkeypatch.setattr(
        voice, "transcribe_fn", lambda data, filename: "I want you to file the invoice"
    )

    class _Cfg:
        extra = {}

    adapter = RetinueRoomsAdapter(_Cfg())
    adapter._loop = object()

    def fake_post(room_id, text, from_name, wait=False):
        captured["text"] = text
        return {"seq": 4, "planned": ["patty"]}

    adapter.post_user_message = fake_post  # type: ignore[method-assign]
    result = adapter.post_user_audio(
        "r1",
        b"wav",
        filename="mic.wav",
        from_name="Mark",
        draft="@Patty ",
    )
    assert captured["text"] == "@Patty I want you to file the invoice"
    assert result["text"] == captured["text"]
    assert result["planned"] == ["patty"]


def test_post_user_audio_empty_draft_is_unchanged(monkeypatch, tmp_path):
    from .adapter import RetinueRoomsAdapter

    captured = {}
    monkeypatch.setattr(voice, "transcribe_fn", lambda data, filename: "just speech")

    class _Cfg:
        extra = {}

    adapter = RetinueRoomsAdapter(_Cfg())
    adapter._loop = object()
    adapter.post_user_message = (  # type: ignore[method-assign]
        lambda room_id, text, from_name, wait=False: captured.update(text=text)
        or {"seq": 1, "planned": []}
    )
    result = adapter.post_user_audio("r1", b"wav", draft="   ")
    assert captured["text"] == "just speech"
    assert result["text"] == "just speech"


def test_post_user_audio_rewrites_spoken_vocative(monkeypatch, tmp_path):
    from .adapter import RetinueRoomsAdapter
    from .engine import Room
    from .store import RoomStore

    captured = {}
    monkeypatch.setattr(
        voice, "transcribe_fn", lambda data, filename: "at Claude how are you doing"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _Cfg:
        extra = {}

    adapter = RetinueRoomsAdapter(_Cfg())
    adapter._loop = object()
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(
        Room(
            id="r1",
            name="Voice",
            members=["claude", "ellie", "admin"],
            lead="admin",
        )
    )
    adapter._display_names = lambda room: {  # type: ignore[method-assign]
        "claude": "Claude",
        "ellie": "Ellie",
        "admin": "Carlos",
    }
    adapter.post_user_message = (  # type: ignore[method-assign]
        lambda room_id, text, from_name, wait=False: captured.update(text=text)
        or {"seq": 5, "planned": ["claude"]}
    )
    result = adapter.post_user_audio("r1", b"wav")
    assert captured["text"] == "@Claude how are you doing"
    assert result["text"] == captured["text"]


def test_post_user_audio_draft_wins_over_spoken_vocative(monkeypatch, tmp_path):
    from .adapter import RetinueRoomsAdapter
    from .engine import Room
    from .store import RoomStore

    captured = {}
    monkeypatch.setattr(
        voice, "transcribe_fn", lambda data, filename: "at Claude ignore this"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _Cfg:
        extra = {}

    adapter = RetinueRoomsAdapter(_Cfg())
    adapter._loop = object()
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    adapter.store.create(
        Room(id="r1", name="Voice", members=["claude", "ellie"], lead="ellie")
    )
    adapter._display_names = lambda room: {  # type: ignore[method-assign]
        "claude": "Claude",
        "ellie": "Ellie",
    }
    adapter.post_user_message = (  # type: ignore[method-assign]
        lambda room_id, text, from_name, wait=False: captured.update(text=text)
        or {"seq": 6, "planned": ["ellie"]}
    )
    adapter.post_user_audio("r1", b"wav", draft="@Ellie ")
    assert captured["text"] == "@Ellie at Claude ignore this"


def test_sidecar_status_without_binaries(monkeypatch):
    from . import voice_sidecar

    monkeypatch.delenv("RETINUE_VOICE_WHISPER", raising=False)
    monkeypatch.delenv("RETINUE_VOICE_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("RETINUE_VOICE_PIPER", raising=False)
    monkeypatch.setattr(voice_sidecar.shutil, "which", lambda *_a, **_k: None)
    st = voice_sidecar.status()
    assert st["ok"] is False
    assert st["stt"]["ready"] is False


def test_provider_error_is_voice_error(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai", "api_key": "k", "base_url": "https://api.x.ai/v1"},
    )
    monkeypatch.setattr(
        voice,
        "_http_post",
        lambda *a, **k: _FakeResp(status=402, text="spending limit"),
    )
    with pytest.raises(voice.VoiceError, match="402"):
        voice.transcribe(b"data", "x.wav")


# ── Speak Replies gets a spoken script, not raw chat Markdown (#158) ─────


ITINERARY_TURN = """```itinerary
title: Content-classification gap
where: .7 detector is in agent-cloud and tested.
- [doing] infra-2lr0.7 code in tree
- [todo] infra-2lr0.8 localhost send_room
- [done] V1 lock
```

Started `.7`. The list raises a hand; it does not file.

Code: `libs/email_automation/urgency.py`, bank at `config/urgency_phrase_bank.yaml`.
"""

ITINERARY_ONLY = ITINERARY_TURN.split("\n\nStarted")[0] + "\n"


class _StubTTSHandler:
    """Exercise _post_tts without standing up the HTTP server."""

    from .adapter import _RoomsRequestHandler as _Real

    _post_tts = _Real._post_tts
    del _Real

    def __init__(self, adapter, body):
        self._body = body
        self.sent = None
        self.server = type("_Server", (), {"adapter": adapter})()

    def _read_body(self):
        return self._body

    def _json(self, status, payload):
        self.sent = (status, payload)

    def _bytes(self, status, payload, ctype):
        self.sent = (status, ctype)

    def _no_content(self, status=204):
        self.sent = (status, None)


def _capture_tts(monkeypatch):
    monkeypatch.setenv("RETINUE_VOICE_BACKEND", "xai")
    monkeypatch.setattr(
        voice,
        "_xai_creds",
        lambda: {"provider": "xai", "api_key": "test-key", "base_url": "https://api.x.ai/v1"},
    )
    seen = {}

    def fake_post(url, **kwargs):
        seen["json"] = kwargs.get("json_body")
        return _FakeResp(content=b"ID3fake-mp3")

    monkeypatch.setattr(voice, "_http_post", fake_post)
    return seen


def test_synthesize_does_not_speak_the_itinerary_card(monkeypatch):
    """The itinerary is a UI card; read aloud it recites the whole thread."""
    seen = _capture_tts(monkeypatch)
    voice.synthesize(ITINERARY_TURN, "mangus")
    spoken = seen["json"]["text"]
    assert "```" not in spoken
    assert "itinerary" not in spoken
    assert "[doing]" not in spoken
    assert "[todo]" not in spoken
    assert "[done]" not in spoken
    assert "Content-classification gap" not in spoken
    assert "The list raises a hand" in spoken


def test_synthesize_keeps_underscores_in_identifiers(monkeypatch):
    """An intra-word underscore is not Markdown emphasis (#158)."""
    seen = _capture_tts(monkeypatch)
    voice.synthesize(ITINERARY_TURN, "mangus")
    spoken = seen["json"]["text"]
    assert "email_automation" in spoken
    assert "urgency_phrase_bank" in spoken
    assert "emailautomation" not in spoken


def test_spoken_text_drops_a_card_only_turn_and_keeps_prose():
    assert voice.spoken_text(ITINERARY_ONLY) == ""
    assert voice.spoken_text("Plain words.") == "Plain words."


def test_post_tts_returns_204_when_there_is_nothing_to_speak(tmp_path):
    """Silence must not reach Speak Replies as a TTS failure."""
    from .adapter import RetinueRoomsAdapter

    adapter = RetinueRoomsAdapter.__new__(RetinueRoomsAdapter)
    adapter._home_dir = lambda: str(tmp_path)

    handler = _StubTTSHandler(adapter, {"text": ITINERARY_ONLY, "speaker": "mangus"})
    handler._post_tts()
    assert handler.sent == (204, None)


def test_synthesize_dispatch_hands_an_override_the_spoken_script(monkeypatch):
    """Swapping the synthesiser must not smuggle raw Markdown to a provider."""
    seen = {}

    def fake_synth(text, speaker):
        seen["text"] = text
        return b"ID3fake-mp3"

    monkeypatch.setattr(voice, "synthesize_fn", fake_synth)
    voice.synthesize_dispatch(ITINERARY_TURN, "mangus")
    assert "```" not in seen["text"]
    assert "[doing]" not in seen["text"]
    assert "The list raises a hand" in seen["text"]

    with pytest.raises(voice.VoiceError, match="empty text"):
        voice.synthesize_dispatch(ITINERARY_ONLY, "mangus")
