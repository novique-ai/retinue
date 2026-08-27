"""Agent-runtime axis: registry, hire, dispatch, briefing, stop (#218)."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from gateway.config import PlatformConfig

from . import engine, grokbuild, hire, runtimes
from .adapter import RetinueRoomsAdapter, _PendingTurn
from .engine import KIND_AGENT, KIND_TOOL, KIND_USER, Room, RoomMessage
from .grokbuild import TurnResult
from .runtimes import (
    RUNTIME_GROK_BUILD,
    RUNTIME_HERMES,
    normalize_runtime,
    runtime_for_member,
    validate_runtime,
)
from .store import RoomStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _adapter(tmp_path, monkeypatch) -> RetinueRoomsAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _grok_member(home: str, name: str = "Gizmo") -> str:
    meta = hire.scaffold_profile(home, name, "build things", "carefully", runtime="grok-build")
    return meta["slug"]


# ── registry ─────────────────────────────────────────────────────────────


def test_registry_names_both_runtimes():
    assert set(runtimes.known_runtimes()) == {RUNTIME_HERMES, RUNTIME_GROK_BUILD}
    info = runtimes.runtime_info(RUNTIME_GROK_BUILD)
    assert info is not None
    assert info.capabilities["tool_activity"] is True
    assert info.capabilities["model_choice"] is False
    hermes = runtimes.runtime_info(RUNTIME_HERMES)
    assert hermes is not None and hermes.capabilities["model_choice"] is True


def test_normalize_and_validate():
    assert normalize_runtime(None) == RUNTIME_HERMES
    assert normalize_runtime("") == RUNTIME_HERMES
    assert normalize_runtime("Grok_Build") == RUNTIME_GROK_BUILD
    assert normalize_runtime("grok") == RUNTIME_GROK_BUILD
    assert validate_runtime("hermes") == RUNTIME_HERMES
    with pytest.raises(ValueError):
        validate_runtime("copilot")


def test_runtime_for_member_defaults_to_hermes(tmp_path):
    home = str(tmp_path)
    assert runtime_for_member(home, "nobody") == RUNTIME_HERMES
    assert runtime_for_member(home, "default") == RUNTIME_HERMES
    slug = _grok_member(home)
    assert runtime_for_member(home, slug) == RUNTIME_GROK_BUILD
    # An unknown stored value must not break the member — it degrades to
    # Hermes rather than refusing every turn.
    meta_path = os.path.join(home, "profiles", slug, hire.AGENT_META_FILENAME)
    meta = json.load(open(meta_path, encoding="utf-8"))
    meta["runtime"] = "somebody-elses-runtime"
    json.dump(meta, open(meta_path, "w", encoding="utf-8"))
    assert runtime_for_member(home, slug) == RUNTIME_HERMES


def test_list_runtimes_reports_health(tmp_path, monkeypatch):
    monkeypatch.setattr(
        grokbuild, "health", lambda home_dir, force=False: {"status": "auth_required", "detail": "x"}
    )
    entries = {e["id"]: e for e in runtimes.list_runtimes(str(tmp_path))}
    assert entries[RUNTIME_HERMES]["health"]["status"] == "available"
    assert entries[RUNTIME_GROK_BUILD]["health"]["status"] == "auth_required"


# ── hire integration ─────────────────────────────────────────────────────


def test_scaffold_stores_runtime_and_rejects_model_preset(tmp_path):
    home = str(tmp_path)
    slug = _grok_member(home)
    meta = json.load(open(os.path.join(home, "profiles", slug, hire.AGENT_META_FILENAME), encoding="utf-8"))
    assert meta["runtime"] == RUNTIME_GROK_BUILD
    with pytest.raises(ValueError, match="model presets do not apply"):
        hire.scaffold_profile(home, "Other", "job", "", runtime="grok-build", model_preset="grok-4.6")
    # Hermes members do not get a runtime key at all (additive axis).
    hermes_meta = hire.scaffold_profile(home, "Plain", "job", "")
    stored = json.load(
        open(os.path.join(home, "profiles", hermes_meta["slug"], hire.AGENT_META_FILENAME), encoding="utf-8")
    )
    assert "runtime" not in stored


def test_grok_turn_timeout_selected(tmp_path, monkeypatch):
    home = str(tmp_path)
    slug = _grok_member(home)
    monkeypatch.delenv("RETINUE_ROOMS_GROK_TURN_TIMEOUT", raising=False)
    assert hire.turn_timeout_for(home, slug, "sandbox") == hire.grok_turn_timeout()
    monkeypatch.setenv("RETINUE_ROOMS_GROK_TURN_TIMEOUT", "123")
    assert hire.turn_timeout_for(home, slug, "ide") == 123.0


def test_list_agents_annotates_grok_member(tmp_path):
    home = str(tmp_path)
    slug = _grok_member(home)
    agent = {a["slug"]: a for a in hire.list_agents(home)}[slug]
    assert agent["runtime"] == RUNTIME_GROK_BUILD
    assert agent["model_summary"].startswith("Grok Build")
    assert agent["model_preset"] is None
    assert agent["local_llm"] is False


def test_apply_model_preset_refused_for_grok_member(tmp_path):
    home = str(tmp_path)
    hire.ensure_bundled_cloud_presets(home)
    slug = _grok_member(home)
    with pytest.raises(ValueError, match="model presets do not apply"):
        hire.apply_model_preset(home, slug, "grok-4.6")


def test_annotate_agents_maps_grok_health(tmp_path, monkeypatch):
    from . import auth

    home = str(tmp_path)
    monkeypatch.setattr(
        grokbuild,
        "health",
        lambda home_dir, force=False: {"status": "auth_required", "detail": "run grok login"},
    )
    agents = [{"slug": "g", "runtime": "grok-build"}]
    auth.annotate_agents(home, agents)
    assert agents[0]["auth_status"] == "relogin_required"
    assert agents[0]["auth_provider"] == "grok-build"
    monkeypatch.setattr(
        grokbuild, "health", lambda home_dir, force=False: {"status": "available"}
    )
    agents = [{"slug": "g", "runtime": "grok-build"}]
    auth.annotate_agents(home, agents)
    assert agents[0]["auth_status"] == "ok"


def test_hire_agent_refuses_when_runtime_unavailable(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grokbuild,
        "health",
        lambda home_dir, force=False: {"status": "not_installed", "detail": "no grok"},
    )
    with pytest.raises(ValueError, match="not usable"):
        adapter.hire_agent("Gizmo", "build", "well", runtime="grok-build")


# ── engine: briefing + transcript kinds ──────────────────────────────────


def _room(**kwargs) -> Room:
    defaults = dict(id="r-1", name="Test", members=["gizmo"], lead="gizmo")
    defaults.update(kwargs)
    return Room(**defaults)


def test_briefing_host_native_names_host_paths():
    room = _room(workspace="ide", ide_path="/srv/project")
    text = engine.room_briefing(
        room,
        "gizmo",
        ["Mark"],
        host_workspace="/srv/project",
        host_uploads="/srv/uploads",
    )
    assert "/srv/project" in text
    assert "/srv/uploads" in text
    # No container-mount narrative for a host-native member.
    assert "bind-mount" not in text
    assert "/workspace/uploads" not in text


def test_briefing_container_narrative_unchanged_without_host_workspace():
    room = _room(workspace="ide", ide_path=None)
    text = engine.room_briefing(room, "gizmo", ["Mark"])
    assert "/workspace" in text


def test_tool_kind_excluded_from_member_delta(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    room = _room(members=["gizmo", "scout"])
    adapter.store.create(room)
    adapter.store.append(
        room.id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="hi @gizmo")
    )
    adapter.store.append(
        room.id, RoomMessage(seq=0, ts=0, kind=KIND_TOOL, speaker="gizmo", text="Read a file")
    )
    readable, delta = adapter._unseen(room, "scout")
    kinds = {m.kind for m in readable}
    assert KIND_TOOL not in kinds
    assert [m.text for m in delta] == ["hi @gizmo"]
    # A tool line alone is not a reason to speak.
    room2 = _room(id="r-2", members=["gizmo", "scout"])
    adapter.store.create(room2)
    adapter.store.append(
        room2.id, RoomMessage(seq=0, ts=0, kind=KIND_TOOL, speaker="gizmo", text="Ran tests")
    )
    _, delta2 = adapter._unseen(room2, "scout")
    assert delta2 == []


# ── adapter dispatch ─────────────────────────────────────────────────────


class _FakeManager:
    def __init__(self, result: TurnResult | Exception):
        self.result = result
        self.calls: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []

    async def run_turn(self, room_id, member, cwd, *, build_prompt, approval, timeout, on_activity=None, cancel_event=None, extra_roots=(), denied_roots=None):
        self.calls.append(
            {
                "room": room_id,
                "member": member,
                "cwd": cwd,
                "prompt": build_prompt(True),
                "approval": approval,
                "timeout": timeout,
                "extra_roots": extra_roots,
                "denied_roots": denied_roots,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        if on_activity is not None:
            on_activity("tool_start", {"title": "Read `README.md`", "kind": "read", "tool_call_id": "t1"})
        return self.result

    async def cancel(self, room_id, member):
        self.cancelled.append((room_id, member))

    async def reset(self, room_id, member):
        pass

    async def shutdown(self):
        pass


def _wire(tmp_path, monkeypatch, result) -> tuple[RetinueRoomsAdapter, _FakeManager, Room, str]:
    adapter = _adapter(tmp_path, monkeypatch)
    slug = _grok_member(str(tmp_path))
    fake = _FakeManager(result)
    monkeypatch.setattr(adapter, "_grok_manager", lambda: fake)
    room = _room(members=[slug], lead=slug)
    adapter.store.create(room)
    adapter.store.append(
        room.id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="Mark", text="please do the thing")
    )
    return adapter, fake, adapter.store.get(room.id), slug


def test_grok_member_turn_dispatches_to_runtime(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="end_turn", text="done it")
    )
    ok, text = _run(adapter._agent_turn(room, slug))
    assert (ok, text) == (True, "done it")
    call = fake.calls[0]
    assert call["member"] == slug
    # Sandbox room -> the dedicated per-room host workspace dir.
    assert call["cwd"].endswith(os.path.join("grok_workspaces", room.id))
    # Fresh-session prompt leads with the briefing (room identity) and
    # carries the delta.
    assert f'a member of the room "{room.name}"' in call["prompt"]
    assert "please do the thing" in call["prompt"]
    # Tool activity landed on the transcript as a tool-kind line.
    tool_lines = [m for m in adapter.store.read_since(room.id, 0) if m.kind == KIND_TOOL]
    assert [m.text for m in tool_lines] == ["Read `README.md`"]
    assert tool_lines[0].speaker == slug
    # Watermark advanced over the delta (turn completed).
    assert adapter.store.get(room.id).last_seen[slug] >= 1


def test_grok_turn_failure_restores_watermark(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, grokbuild.GrokBuildError("agent exploded")
    )
    ok, text = _run(adapter._agent_turn(room, slug))
    assert not ok and "agent exploded" in text
    assert adapter.store.get(room.id).last_seen.get(slug, 0) == 0


def test_grok_auth_failure_names_the_fix(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, grokbuild.GrokBuildAuthRequired("token expired")
    )
    ok, text = _run(adapter._agent_turn(room, slug))
    assert not ok
    assert "grok login" in text


def test_cancelled_stop_reason_is_a_failed_turn(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="cancelled", text="partial")
    )
    ok, text = _run(adapter._agent_turn(room, slug))
    assert not ok and "cancel" in text.lower()


def test_empty_reply_is_no_reply(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="end_turn", text="   ")
    )
    ok, text = _run(adapter._agent_turn(room, slug))
    assert not ok and text == "agent returned no reply"


def test_worktree_rooms_map_isolation_for_grok_members(tmp_path, monkeypatch):
    """#223: worktree rooms no longer refuse — the shadowed real tree
    becomes a denied root with a redirect to the room's own checkout,
    the checkout an allowed extra root, and the briefing explains it."""
    from . import worktrees

    project = tmp_path / "proj"
    (project / "infra").mkdir(parents=True)
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(tmp_path))
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="end_turn", text="hi")
    )
    adapter.store.mutate(
        room.id,
        lambda r: (
            setattr(r, "workspace", "ide"),
            setattr(r, "ide_path", str(project)),
            setattr(r, "worktree_repos", ["infra"]),
        ),
    )
    room = adapter.store.get(room.id)
    ok, text = _run(adapter._agent_turn(room, slug))
    assert ok, text
    call = fake.calls[0]
    wt = worktrees.worktree_path(
        room.id, "infra", worktrees.resolve_worktree_root()
    )
    real = os.path.join(str(project), "infra")
    assert call["extra_roots"] == (wt,)
    assert call["denied_roots"] == {real: wt}
    # Briefing names the isolation, the checkout, and the branch.
    assert "EXCEPTION — infra is isolated" in call["prompt"]
    assert wt in call["prompt"]
    assert worktrees.branch_for(room.id) in call["prompt"]


def test_rooms_without_worktrees_pass_no_roots(tmp_path, monkeypatch):
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="end_turn", text="hi")
    )
    ok, _ = _run(adapter._agent_turn(room, slug))
    assert ok
    assert fake.calls[0]["extra_roots"] == ()
    assert fake.calls[0]["denied_roots"] is None


def test_ide_room_uses_the_room_tree_as_cwd(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("RETINUE_IDE_ROOT", str(tmp_path))
    adapter, fake, room, slug = _wire(
        tmp_path, monkeypatch, TurnResult(stop_reason="end_turn", text="hi")
    )
    adapter.store.mutate(room.id, lambda r: (setattr(r, "workspace", "ide"), setattr(r, "ide_path", str(project))))
    room = adapter.store.get(room.id)
    ok, _ = _run(adapter._agent_turn(room, slug))
    assert ok
    assert fake.calls[0]["cwd"] == str(project)


def test_stop_uses_grok_cancel_hook(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    called = []

    async def fake_cancel():
        called.append(True)

    pending = _PendingTurn(
        task_id="t", room_id="r-1", member="gizmo", future=__import__("concurrent.futures", fromlist=["Future"]).Future(),
        grok_cancel=fake_cancel,
    )
    _run(adapter._interrupt_turn(pending))
    assert called == [True]
