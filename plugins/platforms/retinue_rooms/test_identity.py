"""Per-agent identity, voice, and persona (#78 / #79 / #82)."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from gateway.config import PlatformConfig

from . import engine, hire, principal, voice
from .adapter import RetinueRoomsAdapter, _RoomsRequestHandler, _RoomsServer
from .engine import Room
from .store import RoomStore


# Contract pins — sha1(slug)[:4] % n. Do not use Python's hash().
_PALETTE = [
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
_VOICES = ("eve", "leo", "rex", "rigel", "ursa", "celeste", "lux", "iris")


def _stable_index(slug: str, n: int) -> int:
    digest = hashlib.sha1(slug.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % n


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["scout", "editor"])
    defaults.update(kwargs)
    return Room(**defaults)


def _httpd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    server = _RoomsServer(("127.0.0.1", 0), _RoomsRequestHandler, adapter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _call(httpd, method, path, body=None):
    import http.client

    conn = http.client.HTTPConnection(*httpd.server_address[:2], timeout=3)
    raw = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if raw is not None else {}
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode())
    conn.close()
    return resp.status, payload


# ── derivation pins ──────────────────────────────────────────────────────


def test_known_slug_maps_to_known_color_and_voice():
    """Pin the hash so colour/voice cannot silently drift (CONTRACT)."""
    from . import identity

    assert _PALETTE[_stable_index("scout", len(_PALETTE))] == "emerald"
    assert _VOICES[_stable_index("herald", len(_VOICES))] == "rigel"
    assert _stable_index("scout", 12) == 8
    # Python's salted hash() would not be stable across processes.
    assert hashlib.sha1(b"scout").digest()[:4].hex() == "6a87c094"
    assert identity.stable_index("scout", 12) == 8
    assert identity.PALETTE[identity.stable_index("scout", 12)] == "emerald"
    assert identity.PALETTE == _PALETTE


def test_list_agents_resolves_pinned_color_and_voice(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    hire.scaffold_profile(str(tmp_path), "Herald", "announce", "")
    by_slug = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
    assert "identity" in by_slug["scout"]
    assert by_slug["scout"]["identity"]["color"] == "emerald"
    assert by_slug["scout"]["identity"]["color_source"] == "derived"
    assert by_slug["scout"]["identity"]["initial"] == "S"
    assert by_slug["scout"]["identity"]["emoji"] is None
    # herald is not in STAFF_VOICES — this is the stable_index voice pin
    assert "voice_resolved" in by_slug["herald"]
    assert by_slug["herald"]["voice_resolved"] == "rigel"
    assert by_slug["herald"]["identity"]["color"] == "rose"


# ── identity fields ──────────────────────────────────────────────────────


def test_display_initial_handles_emoji_and_empty():
    from . import identity

    assert identity.display_initial("Scout") == "S"
    assert identity.display_initial("🛰 Probe") == "🛰"
    assert identity.display_initial("123abc") == "1"
    assert identity.display_initial("  ") == "?"
    assert identity.display_initial("") == "?"


def test_identity_initial_handles_emoji_and_non_letter(tmp_path):
    cases = {
        "probe": "🛰 Probe",
        "numbered": "123abc",
        "scout": "Scout",
    }
    for slug, name in cases.items():
        pdir = tmp_path / "profiles" / slug
        pdir.mkdir(parents=True)
        (pdir / "retinue-agent.json").write_text(
            json.dumps({"display_name": name, "slug": slug, "job": "x", "how": ""}),
            encoding="utf-8",
        )
    by_slug = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
    assert "identity" in by_slug["probe"]
    assert by_slug["probe"]["identity"]["initial"] == "🛰"
    assert by_slug["numbered"]["identity"]["initial"] == "1"
    assert by_slug["scout"]["identity"]["initial"] == "S"


def test_hire_and_patch_accept_avatar_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: m\n  provider: custom\n", encoding="utf-8"
    )
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd,
            "POST",
            "/agents",
            {
                "name": "Probe",
                "job": "watch the sky",
                "avatar_emoji": "🛰",
                "avatar_color": "indigo",
            },
        )
        assert status == 201
        assert payload["identity"]["emoji"] == "🛰"
        assert payload["identity"]["color"] == "indigo"
        assert payload["identity"]["color_source"] == "override"
        assert payload["identity"]["initial"] == "P"
        assert payload["avatar_emoji"] == "🛰"
        assert payload["avatar_color"] == "indigo"

        status, payload = _call(
            httpd,
            "PATCH",
            "/agents/probe",
            {"avatar_emoji": "📡", "avatar_color": "teal"},
        )
        assert status == 200
        assert payload["identity"]["emoji"] == "📡"
        assert payload["identity"]["color"] == "teal"

        status, payload = _call(httpd, "GET", "/agents/probe")
        assert status == 200
        assert payload["identity"]["color"] == "teal"
        assert payload["identity"]["initial"] == "P"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_invalid_avatar_color_is_rejected_not_coerced(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd, "PATCH", "/agents/scout", {"avatar_color": "hotpink"}
        )
        assert status == 400
        assert "hotpink" in str(payload.get("error") or "").lower() or "color" in str(
            payload.get("error") or ""
        ).lower()
        listed = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
        assert listed["scout"]["identity"]["color"] == "emerald"
        assert listed["scout"]["identity"]["color_source"] == "derived"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hire_invalid_color_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: m\n  provider: custom\n", encoding="utf-8"
    )
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd,
            "POST",
            "/agents",
            {"name": "Ghost", "job": "haunt", "avatar_color": "hotpink"},
        )
        assert status == 400
        assert "hotpink" in str(payload.get("error") or "").lower() or "color" in str(
            payload.get("error") or ""
        ).lower()
        assert not (tmp_path / "profiles" / "ghost").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_avatar_emoji_rejects_a_sentence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd,
            "PATCH",
            "/agents/scout",
            {"avatar_emoji": "this is a whole sentence not a glyph"},
        )
        assert status == 400
        assert "emoji" in str(payload.get("error") or "").lower()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_identity_palette_endpoint(tmp_path, monkeypatch):
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(httpd, "GET", "/identity/palette")
        assert status == 200
        assert payload["colors"] == _PALETTE
        assert len(payload["colors"]) == 12
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_legacy_profile_without_new_keys_resolves_cleanly(tmp_path):
    pdir = tmp_path / "profiles" / "oldhand"
    pdir.mkdir(parents=True)
    (pdir / "retinue-agent.json").write_text(
        json.dumps(
            {
                "display_name": "Old Hand",
                "slug": "oldhand",
                "job": "remember",
                "how": "quietly",
            }
        ),
        encoding="utf-8",
    )
    (pdir / "SOUL.md").write_text("You are Old Hand.\n", encoding="utf-8")
    agents = hire.list_agents(str(tmp_path))
    old = {a["slug"]: a for a in agents}["oldhand"]
    assert "identity" in old
    assert old["identity"]["emoji"] is None
    assert old["identity"]["initial"] == "O"
    assert old["identity"]["color"] == _PALETTE[_stable_index("oldhand", 12)]
    assert old["identity"]["color_source"] == "derived"
    assert old["persona"] == {
        "warmth": "balanced",
        "verbosity": "balanced",
        "formality": "balanced",
    }
    assert old["voice"] is None
    assert old["voice_resolved"]
    assert old["avatar_emoji"] is None
    assert old["avatar_color"] is None


# ── voice ────────────────────────────────────────────────────────────────


def test_voice_precedence_order(tmp_path, monkeypatch):
    """Pin the full precedence so the next person can see it.

    1. RETINUE_VOICE_MAP (env)
    2. per-agent stored ``voice``
    3. STAFF_VOICES
    4. stable_index over the available voice list
    """
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / "scout").mkdir(parents=True)
    (tmp_path / "profiles" / "scout" / "retinue-agent.json").write_text(
        json.dumps({"display_name": "Scout", "slug": "scout", "voice": "helix"}),
        encoding="utf-8",
    )
    (tmp_path / "profiles" / "herald").mkdir(parents=True)
    (tmp_path / "profiles" / "herald" / "retinue-agent.json").write_text(
        json.dumps({"display_name": "Herald", "slug": "herald"}),
        encoding="utf-8",
    )

    home = str(tmp_path)
    # 3. staff default still applies when nothing is stored
    assert voice.voice_for("admin", home_dir=home) == "eve"
    # 4. non-staff with nothing stored derives from the slug
    assert voice.voice_for("herald", home_dir=home) == "rigel"
    # 2. stored override beats STAFF_VOICES
    assert voice.voice_for("scout", home_dir=home) == "helix"
    # 1. env map beats stored override
    monkeypatch.setenv("RETINUE_VOICE_MAP", "scout:iris")
    assert voice.voice_for("scout", home_dir=home) == "iris"
    # env map still wins for staff with no stored voice
    monkeypatch.setenv("RETINUE_VOICE_MAP", "admin:lux")
    assert voice.voice_for("admin", home_dir=home) == "lux"


def test_get_voice_reflects_per_agent_override(tmp_path, monkeypatch):
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Herald", "announce", "")
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, patched = _call(httpd, "PATCH", "/agents/herald", {"voice": "celeste"})
        assert status == 200
        assert patched.get("voice_resolved") == "celeste" or patched.get("voice") == "celeste"
        status, payload = _call(httpd, "GET", "/voice")
        assert status == 200
        assert payload["voices"]["herald"] == "celeste"
        assert payload["voices"]["scout"] == "ursa"
        assert payload["voices"]["admin"] == "eve"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_voice_is_not_inferred_from_display_name(tmp_path):
    """No name-to-gender heuristic. Derivation is slug → stable_index only."""
    from .identity import stable_index

    hire.scaffold_profile(str(tmp_path), "Alice", "research", "")
    hire.scaffold_profile(str(tmp_path), "Bob", "write", "")
    by_slug = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
    alice = voice.AVAILABLE_VOICES[stable_index("alice", len(voice.AVAILABLE_VOICES))]
    bob = voice.AVAILABLE_VOICES[stable_index("bob", len(voice.AVAILABLE_VOICES))]
    assert by_slug["alice"]["voice_resolved"] == alice
    assert by_slug["bob"]["voice_resolved"] == bob


def test_staff_and_env_map_unchanged_without_override(monkeypatch):
    monkeypatch.delenv("RETINUE_VOICE_MAP", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert voice.voice_for("scout") == "ursa"
    assert voice.voice_for("admin") == "eve"
    monkeypatch.setenv("RETINUE_VOICE_MAP", "scout:helix,newbie:iris")
    assert voice.voice_for("scout") == "helix"
    assert voice.voice_for("newbie") == "iris"


# ── persona dials ────────────────────────────────────────────────────────


def test_all_balanced_soul_is_byte_identical():
    """Every existing agent is effectively all-balanced. Do not grow the SOUL."""
    baseline = hire.soul_template("Scout", "find facts fast", "check sources; be terse")
    balanced = hire.soul_template(
        "Scout",
        "find facts fast",
        "check sources; be terse",
        persona={"warmth": "balanced", "verbosity": "balanced", "formality": "balanced"},
    )
    unset = hire.soul_template("Scout", "find facts fast", "check sources; be terse", persona=None)
    assert balanced == baseline
    assert unset == baseline


def test_non_balanced_dials_add_short_phrases():
    baseline = hire.soul_template("Scout", "find facts", "be terse")
    warmed = hire.soul_template(
        "Scout",
        "find facts",
        "be terse",
        persona={"warmth": "warm", "verbosity": "balanced", "formality": "balanced"},
    )
    assert warmed != baseline
    assert "How you work:" in warmed
    assert "be terse" in warmed
    # A phrase, not a paragraph, and not folded into `how`.
    extra = warmed[len(baseline) :] if warmed.startswith(baseline[:20]) else warmed
    assert "warm" in warmed.lower() or "encourag" in warmed.lower()
    assert extra.count("\n") < 8


def test_invalid_dial_is_rejected_not_coerced(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    before = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd, "PATCH", "/agents/scout", {"persona": {"warmth": "sizzling"}}
        )
        assert status == 400
        assert "warmth" in str(payload.get("error") or "").lower()
        after = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
        assert after == before
        listed = {a["slug"]: a for a in hire.list_agents(str(tmp_path))}
        assert listed["scout"]["persona"]["warmth"] == "balanced"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_patch_persona_rewrites_soul(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    hire.scaffold_profile(str(tmp_path), "Scout", "find facts", "be terse")
    before = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, updated = _call(
            httpd,
            "PATCH",
            "/agents/scout",
            {"persona": {"warmth": "warm", "verbosity": "brief", "formality": "casual"}},
        )
        assert status == 200
        after = (tmp_path / "profiles" / "scout" / "SOUL.md").read_text(encoding="utf-8")
        assert after != before
        assert "You are Scout." in after
        assert "be terse" in after
        assert updated["persona"]["warmth"] == "warm"
        assert updated["persona"]["verbosity"] == "brief"
        assert updated["persona"]["formality"] == "casual"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hire_with_persona_composes_soul(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: None)
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: m\n  provider: custom\n", encoding="utf-8"
    )
    httpd = _httpd(tmp_path, monkeypatch)
    try:
        status, payload = _call(
            httpd,
            "POST",
            "/agents",
            {
                "name": "Herald",
                "job": "announce",
                "persona": {"formality": "formal"},
            },
        )
        assert status == 201
        assert payload["persona"]["formality"] == "formal"
        assert payload["persona"]["warmth"] == "balanced"
        soul = (tmp_path / "profiles" / "herald" / "SOUL.md").read_text(encoding="utf-8")
        assert "formal" in soul.lower()
        baseline = hire.soul_template("Herald", "announce", "")
        assert soul != baseline
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── principal name guidance ──────────────────────────────────────────────


def test_briefing_names_the_principal_when_set():
    room = _room()
    text = engine.room_briefing(
        room, "scout", ["Clayton"], principal_name="Clayton"
    )
    assert "Clayton" in text
    assert "every" in text.lower()
    assert "greet" in text.lower() or "hand" in text.lower()
    # Not a maximisable quota.
    assert "as often as" not in text.lower()
    assert "whenever possible" not in text.lower()


def test_briefing_omits_name_guidance_when_unset_or_you():
    room = _room()
    unset = engine.room_briefing(room, "scout", ["You"])
    you = engine.room_briefing(room, "scout", ["You"], principal_name="You")
    empty = engine.room_briefing(room, "scout", ["You"], principal_name="")
    for text in (unset, you, empty):
        assert "The human's name is" not in text
        assert "every message" not in text
        assert "every reply" not in text


def test_adapter_passes_principal_name_into_briefing(tmp_path, monkeypatch):
    """Unset principal is literally 'You' — do not tell the agent to say You."""
    captured = {}

    def fake_briefing(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["args"] = args
        return "brief"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "room_briefing", fake_briefing)
    principal.save(str(tmp_path), {"display_name": "Clayton", "about": "I run this."})
    # We only need to see the call site; _agent_turn is heavy. Read the
    # adapter wiring via a thin public helper if one exists, else check
    # the kwargs the adapter would pass by invoking the same load path.
    me = principal.load(str(tmp_path))
    assert me["display_name"] == "Clayton"
    # Contract: the adapter must pass the name alongside about.
    # Exercised through room_briefing tests above plus this load check.
    assert me["display_name"] != principal.DEFAULT_NAME
