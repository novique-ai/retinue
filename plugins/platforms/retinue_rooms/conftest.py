"""Shared fixtures for the rooms plugin tests (re-exported by tests/retinue_rooms)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_host_uv_python(tmp_path, monkeypatch):
    """Keep the developer's real uv interpreter store out of ide-room volume lists."""
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path / "no-uv-python"))
