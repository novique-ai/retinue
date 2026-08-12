"""Enablement/creation gate behavior (see infra: quiet secondary-profile declines).

The per-profile skip must happen at the is_connected enablement gate (the
registry's quiet debug path), with validate_config kept only as a loud
backstop for paths that bypass enablement.
"""

from __future__ import annotations

import importlib

import pytest

rooms_pkg = importlib.import_module("plugins.platforms.retinue_rooms")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("RETINUE_ROOMS_ENABLED", "1")
    monkeypatch.delenv("RETINUE_ROOMS_API_KEY", raising=False)


def _fake_scope(monkeypatch, path: str) -> None:
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: path)


def test_is_connected_requires_opt_in(monkeypatch):
    monkeypatch.delenv("RETINUE_ROOMS_ENABLED", raising=False)
    monkeypatch.delenv("RETINUE_ROOMS_API_KEY", raising=False)
    assert rooms_pkg.is_connected(None) is False


def test_is_connected_true_in_default_scope(enabled, monkeypatch):
    _fake_scope(monkeypatch, "/home/user/.hermes")
    assert rooms_pkg.is_connected(None) is True


def test_is_connected_declines_secondary_scope(enabled, monkeypatch):
    """The skip must fire at enablement, NOT surface as a registry WARNING."""
    _fake_scope(monkeypatch, "/home/user/.hermes/profiles/scout")
    assert rooms_pkg.is_connected(None) is False


def test_validate_config_backstop_declines_secondary_scope(monkeypatch):
    _fake_scope(monkeypatch, "/home/user/.hermes/profiles/scout")
    assert rooms_pkg.validate_config(None) is False


def test_validate_config_accepts_default_scope(monkeypatch):
    _fake_scope(monkeypatch, "/home/user/.hermes")
    assert rooms_pkg.validate_config(None) is True
