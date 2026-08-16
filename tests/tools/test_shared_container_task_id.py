"""
Regression tests for the shared-container task_id mapping.

The top-level agent and all delegate_task subagents share a single
terminal sandbox keyed by ``"default"``.  ``_resolve_container_task_id``
is the sole gatekeeper for which tool-call task_ids go to the shared
container vs. get their own isolated sandbox.  RL / benchmark
environments opt in to isolation by calling
``register_task_env_overrides(task_id, {...})`` before the agent loop;
every other task_id collapses back to ``"default"``.

If you change the collapse logic, update both the helper and these
tests -- see `hermes-agent-dev` skill, "Why do subagents get their own
containers?" section, and the Container lifecycle paragraph under
Docker Backend in ``website/docs/user-guide/configuration.md``.
"""

import pytest

from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides(monkeypatch):
    """Ensure no stray overrides from other tests leak in.

    The workspace-computer key is cleared too: it is a cache key of its own
    (see the shared-workspace tests below), so a leaked value would quietly
    turn every "collapses to default" assertion into a different claim.
    """
    monkeypatch.delenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", raising=False)
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def test_none_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_cwd_only_override_collapses_to_default():
    """CWD-only overrides (ACP adapter workspace tracking) must NOT trigger
    container isolation — they should collapse to the shared 'default'
    container so all surfaces (TUI, gateway, dashboard) share one sandbox.
    Regression for #37361."""
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("acp-session-abc")
            == "default"
        )
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_env_type_override_keeps_own_id():
    """env_type is an isolation key — must trigger per-task container."""
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("bench-env")
            == "bench-env"
        )
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")


# ── shared workspace computer (TERMINAL_DOCKER_SHARED_CONTAINER_KEY) ──────
#
# The key names a container shared by every profile in one workspace, and it
# already decides container identity at creation time (the hermes-profile
# label). The cache must agree: collapsing these callers to "default" handed
# the first workspace's environment to every later one.


def test_shared_container_key_becomes_the_cache_key(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "retinue-ide-r-1")
    assert terminal_tool._resolve_container_task_id(None) == "retinue-ide-r-1"
    assert terminal_tool._resolve_container_task_id("") == "retinue-ide-r-1"
    # Subagents still share their parent's workspace container.
    assert terminal_tool._resolve_container_task_id("sub-7") == "retinue-ide-r-1"


def test_distinct_workspaces_get_distinct_cache_keys(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "retinue-sandbox-r-1")
    sandbox = terminal_tool._resolve_container_task_id(None)
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "retinue-ide-r-2")
    ide = terminal_tool._resolve_container_task_id(None)
    assert sandbox != ide


def test_blank_shared_container_key_still_collapses_to_default(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "   ")
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_isolation_override_outranks_the_shared_container_key(monkeypatch):
    """An RL/benchmark rollout asked for its own sandbox — it still gets one."""
    monkeypatch.setenv("TERMINAL_DOCKER_SHARED_CONTAINER_KEY", "retinue-ide-r-1")
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert terminal_tool._resolve_container_task_id("bench-env") == "bench-env"
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")
