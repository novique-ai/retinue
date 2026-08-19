"""Retainer identity mirrored into the profile's ``ui_meta`` block (#137).

The rooms store (``profiles/<slug>/retinue-agent.json``) stays canonical;
these tests pin the write-through mirror into upstream's server-synced
``profile.yaml`` so a stock Hermes client (desktop Bots pane) sees names,
roles and avatars without knowing anything about Retinue.
"""

from __future__ import annotations

import json
import os

import yaml

from . import hire, uimeta


def _profile_yaml(tmp_path, slug: str):
    return tmp_path / "profiles" / slug / "profile.yaml"


def _load(tmp_path, slug: str) -> dict:
    path = _profile_yaml(tmp_path, slug)
    assert path.is_file(), f"no profile.yaml for {slug}"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── hire writes ui_meta ──────────────────────────────────────────────────


def test_hire_writes_retinue_ui_meta(tmp_path):
    hire.scaffold_profile(
        str(tmp_path), "Data Scout", "research things", "check sources; be terse"
    )
    data = _load(tmp_path, "data-scout")

    block = data["ui_meta"][uimeta.NAMESPACE]
    assert block["slug"] == "data-scout"
    assert block["display_name"] == "Data Scout"
    assert block["job"] == "research things"
    assert block["how"] == "check sources; be terse"
    assert block["archived"] is False
    assert block["source"] == "retinue-rooms"

    # Generic fields upstream reads for ANY client (profiles.list rows,
    # hermes profile list) — this is what makes a retainer legible in a
    # stock Bots pane.
    assert data["display_name"] == "Data Scout"
    assert data["description"] == "research things"
    # Curated, so the auto-describer never overwrites the role line.
    assert data["description_auto"] is False


def test_hire_mirrors_resolved_avatar(tmp_path):
    hire.scaffold_profile(
        str(tmp_path), "Scout", "research", "", avatar_emoji="🔭", avatar_color="teal"
    )
    block = _load(tmp_path, "scout")["ui_meta"][uimeta.NAMESPACE]
    assert block["avatar_emoji"] == "🔭"
    assert block["avatar_color"] == "teal"
    assert block["avatar_color_source"] == "override"
    assert block["initial"] == "S"


def test_hire_mirrors_derived_avatar_colour(tmp_path):
    from .identity import derived_color

    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    block = _load(tmp_path, "scout")["ui_meta"][uimeta.NAMESPACE]
    assert "avatar_emoji" not in block
    assert block["avatar_color"] == derived_color("scout")
    assert block["avatar_color_source"] == "derived"


# ── PATCH /agents updates it ─────────────────────────────────────────────


def test_update_agent_updates_ui_meta(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "be terse")
    hire.update_agent(
        str(tmp_path), "scout", display_name="Scout Prime", job="find facts", how="cite"
    )
    data = _load(tmp_path, "scout")
    block = data["ui_meta"][uimeta.NAMESPACE]
    assert block["display_name"] == "Scout Prime"
    assert block["job"] == "find facts"
    assert block["how"] == "cite"
    assert data["display_name"] == "Scout Prime"
    assert data["description"] == "find facts"


def test_update_agent_archive_reaches_ui_meta(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    hire.update_agent(str(tmp_path), "scout", archived=True)
    assert _load(tmp_path, "scout")["ui_meta"][uimeta.NAMESPACE]["archived"] is True


def test_model_switch_reaches_ui_meta(tmp_path):
    presets = tmp_path / hire.MODELS_DIRNAME
    presets.mkdir()
    (presets / "local.yaml").write_text(
        "model:\n  default: local-model\n  provider: openai_compatible\n", encoding="utf-8"
    )
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    hire.apply_model_preset(str(tmp_path), "scout", "local")
    assert _load(tmp_path, "scout")["ui_meta"][uimeta.NAMESPACE]["model_preset"] == "local"


# ── the mirror is a good citizen of a shared block ───────────────────────


def test_mirror_preserves_foreign_namespaces_and_keys(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    path = _profile_yaml(tmp_path, "scout")
    existing = yaml.safe_load(path.read_text(encoding="utf-8"))
    existing["ui_meta"]["hermes-bots"] = {"pinned": True}
    existing["ui_meta"]["accent"] = "#ff0000"
    existing["some_other_key"] = "keep me"
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    hire.update_agent(str(tmp_path), "scout", job="find facts")

    data = _load(tmp_path, "scout")
    assert data["ui_meta"]["hermes-bots"] == {"pinned": True}
    assert data["ui_meta"]["accent"] == "#ff0000"
    assert data["some_other_key"] == "keep me"
    assert data["ui_meta"][uimeta.NAMESPACE]["job"] == "find facts"


def test_mirror_never_claims_the_bot_mode_namespace(tmp_path):
    """Writing ui_meta['hermes-bots'] would flip the whole install into
    Bot-Mode-managed and start injecting the teammate protocol into prompts
    (tools/bot_mode_probe). Our namespace must never do that."""
    from tools.bot_mode_probe import _is_bot_managed

    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    pdir = tmp_path / "profiles" / "scout"
    assert "hermes-bots" not in _load(tmp_path, "scout")["ui_meta"]
    assert _is_bot_managed(pdir) is False


def test_mirror_respects_the_ui_meta_size_cap(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "x" * 200_000)
    block = _load(tmp_path, "scout")["ui_meta"][uimeta.NAMESPACE]
    assert len(json.dumps(_load(tmp_path, "scout")["ui_meta"])) <= uimeta.MAX_UI_META_BYTES
    assert len(block["how"]) < 200_000
    # The canonical store keeps the full text — only the mirror is clamped.
    meta = json.loads(
        (tmp_path / "profiles" / "scout" / hire.AGENT_META_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert len(meta["how"]) == 200_000


# ── lazy migration sweep ─────────────────────────────────────────────────


def _handmade_retainer(tmp_path, slug: str, meta: dict) -> None:
    pdir = tmp_path / "profiles" / slug
    pdir.mkdir(parents=True)
    (pdir / "SOUL.md").write_text("You are handmade.", encoding="utf-8")
    (pdir / hire.AGENT_META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")


def test_sync_all_migrates_pre_existing_retainers(tmp_path):
    _handmade_retainer(
        tmp_path,
        "legacy",
        {"display_name": "Legacy", "slug": "legacy", "job": "old work", "how": "slowly"},
    )
    assert uimeta.sync_all(str(tmp_path)) == ["legacy"]
    block = _load(tmp_path, "legacy")["ui_meta"][uimeta.NAMESPACE]
    assert block["display_name"] == "Legacy"
    assert block["job"] == "old work"


def test_sync_all_is_idempotent(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "")
    assert uimeta.sync_all(str(tmp_path)) == []  # hire already mirrored it
    path = _profile_yaml(tmp_path, "scout")
    before = path.read_bytes()
    mtime = os.stat(path).st_mtime_ns
    assert uimeta.sync_all(str(tmp_path)) == []
    assert path.read_bytes() == before
    assert os.stat(path).st_mtime_ns == mtime


def test_sync_all_ignores_handmade_profiles(tmp_path):
    pdir = tmp_path / "profiles" / "handmade"
    pdir.mkdir(parents=True)
    (pdir / "SOUL.md").write_text("You are handmade.", encoding="utf-8")
    assert uimeta.sync_all(str(tmp_path)) == []
    assert not (pdir / "profile.yaml").exists()


def test_sync_all_survives_a_corrupt_profile_yaml(tmp_path):
    _handmade_retainer(
        tmp_path, "legacy", {"display_name": "Legacy", "slug": "legacy", "job": "j", "how": ""}
    )
    _profile_yaml(tmp_path, "legacy").write_text(": not yaml :\n\t- [", encoding="utf-8")
    assert uimeta.sync_all(str(tmp_path)) == ["legacy"]
    assert _load(tmp_path, "legacy")["ui_meta"][uimeta.NAMESPACE]["job"] == "j"


# ── nothing else moved ───────────────────────────────────────────────────


def test_soul_generation_is_unchanged(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "be terse")
    soul_path = tmp_path / "profiles" / "scout" / "SOUL.md"
    assert soul_path.read_text(encoding="utf-8") == hire.soul_template(
        "Scout", "research", "be terse"
    )

    hire.update_agent(str(tmp_path), "scout", job="find facts")
    assert soul_path.read_text(encoding="utf-8") == hire.soul_template(
        "Scout", "find facts", "be terse"
    )


def test_agent_api_shape_is_unchanged(tmp_path):
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "be terse")
    agent = hire.list_agents(str(tmp_path))[0]
    # The mirror is a side channel: it must not leak into GET /agents.
    assert "ui_meta" not in agent
    assert "profile_yaml" not in agent
    for key in ("slug", "display_name", "job", "how", "identity", "voice_resolved"):
        assert key in agent


def test_upstream_read_path_sees_the_retainer(tmp_path):
    """What a stock client actually gets back from profiles.list."""
    from hermes_cli.profiles import read_profile_meta

    hire.scaffold_profile(str(tmp_path), "Data Scout", "research things", "")
    meta = read_profile_meta(tmp_path / "profiles" / "data-scout")
    assert meta["display_name"] == "Data Scout"
    assert meta["description"] == "research things"
    assert meta["description_auto"] is False
