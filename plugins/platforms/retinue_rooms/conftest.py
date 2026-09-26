"""Shared fixtures for the rooms plugin tests (re-exported by tests/retinue_rooms)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_host_uv_python(tmp_path, monkeypatch):
    """Keep the developer's real uv interpreter store out of ide-room volume lists."""
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path / "no-uv-python"))


@pytest.fixture(autouse=True)
def no_process_workspace_env(monkeypatch):
    """Start every rooms test with no process-wide workspace mount or key (#250).

    The first import of ``gateway.run`` in a process calls
    ``load_hermes_dotenv()`` at module level, which runs
    ``apply_terminal_config_to_env()`` and backfills config defaults straight
    into ``os.environ`` — e.g. ``TERMINAL_DOCKER_VOLUMES='[]'``. That write
    bypasses monkeypatch, so it outlives the test that triggered the import.
    ``workspace_context.getenv`` falls back to ``os.environ`` when no room
    overlay is bound, so the leaked value made the "no mount" security tests
    fail whenever an earlier file had imported ``gateway.run``. Upstream's
    hermetic fixture in ``tests/conftest.py`` does not clear these, and it
    does not reach tests under ``plugins/`` at all, so clear them here.
    """
    monkeypatch.delenv("TERMINAL_DOCKER_VOLUMES", raising=False)
    monkeypatch.delenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", raising=False)
