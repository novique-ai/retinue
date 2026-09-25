"""Regression tests for the Anthropic model-picker dropping curated aliases.

Bug — newly-routed curated aliases vanished on a native Anthropic setup
    ``provider_model_ids("anthropic")`` returned the live ``/v1/models`` dump
    verbatim whenever Anthropic credentials were configured. Anthropic's API
    lags behind freshly-routed aliases (e.g. ``claude-fable-5``, which is
    reachable on Anthropic before the models endpoint enumerates it), so the
    curated entry disappeared from the picker. The picker now merges the
    curated ``_PROVIDER_MODELS["anthropic"]`` list with the live catalog —
    curated entries first, live-only models appended, deduped — mirroring the
    OpenAI curated-merge philosophy.
"""

from unittest.mock import patch

from hermes_cli import models as M


def test_anthropic_curated_alias_survives_when_live_omits_it():
    """A curated alias missing from /v1/models still surfaces (first)."""
    curated = M._PROVIDER_MODELS["anthropic"]
    assert "claude-fable-5" in curated  # sanity: the alias is curated
    assert "claude-sonnet-5" in curated  # newest Sonnet alias is curated

    # Live catalog the API would actually return — no fable-5.
    live = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert "claude-fable-5" in result
    assert "claude-sonnet-5" in result
    # Curated order is preserved at the front.
    assert result[:len(curated)] == list(curated)


def test_anthropic_merge_dedupes_overlap_and_appends_live_only():
    """Models in both lists appear once; live-only models are appended."""
    live = [
        "claude-opus-4-8",          # overlaps curated
        "claude-sonnet-4-6",        # overlaps curated
        "claude-future-9-99",       # live-only, not curated
    ]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    # No duplicates introduced by the merge.
    assert result.count("claude-opus-4-8") == 1
    # Live-only entry is preserved (discovery still works for unknown models).
    assert "claude-future-9-99" in result
    # Curated entries lead, live-only trails.
    assert result.index("claude-fable-5") < result.index("claude-future-9-99")


def test_anthropic_falls_back_to_curated_when_live_unavailable():
    """No creds / live failure -> curated list verbatim (alias still present)."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None):
        result = M.provider_model_ids("anthropic")

    assert result == list(M._PROVIDER_MODELS["anthropic"])
    assert "claude-fable-5" in result


def test_claude_opus_5_5_survives_when_live_omits_it():
    """The new Opus id stays in the picker when /v1/models has not listed it."""
    live = ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live), patch(
        "hermes_cli.config.load_config",
        return_value={"model": {"provider": "anthropic"}},
    ):
        result = M.provider_model_ids("anthropic")

    assert "claude-opus-5-5" in result
    assert result.count("claude-opus-5-5") == 1
    assert result.count("claude-sonnet-5") == 1
    assert result.count("claude-opus-4-8") == 1
    # Predecessors stay selectable. Opus 5 is live-only here, so the curated
    # id leads it.
    for slug in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"):
        assert slug in result
    assert result.index("claude-opus-5-5") < result.index("claude-opus-5")


def test_claude_opus_5_5_is_not_rewritten_to_its_predecessor():
    """Close-match auto-correct must not turn claude-opus-5-5 into claude-opus-5."""
    live = ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.validate_requested_model("claude-opus-5-5", "anthropic")

    assert result["accepted"] is True
    assert result["recognized"] is True
    assert result.get("corrected_model") in (None, "")
    assert "claude-opus-5-5" in (result.get("message") or "")


def test_claude_opus_5_5_recognized_when_live_catalog_unreachable():
    """No token / network failure still accepts the curated id."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None), patch.object(
        M, "fetch_api_models", return_value=None
    ):
        result = M.validate_requested_model("claude-opus-5-5", "anthropic")

    assert result["accepted"] is True
    assert result["recognized"] is True
    assert result.get("corrected_model") in (None, "")
