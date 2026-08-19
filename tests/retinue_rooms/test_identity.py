"""Collector shim for issue #145 — real tests live at plugins/platforms/retinue_rooms/test_identity.py.

`pyproject.toml`'s `testpaths = ["tests"]` is upstream and must not be edited
(fork policy), so the rooms plugin test suite — which lives next to the
plugin, not under tests/ — is invisible to a bare `pytest` run. This file
re-exports that module's tests/fixtures so they get collected here too,
without duplicating any test logic. Do not add new test logic to this file;
add it to the real module instead.
"""

from __future__ import annotations

from plugins.platforms.retinue_rooms.test_identity import *  # noqa: F401,F403
