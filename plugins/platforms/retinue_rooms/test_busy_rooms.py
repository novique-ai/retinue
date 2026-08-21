"""Issue #168 — the busy signal must say WHICH room, not just "somewhere".

`_pending` is keyed `(room, member)`, but `busy_slugs()` projects the room away,
so `agent["busy"]` was a gateway-global flag. A room view reading it painted a
thinking bubble on every other room the agent belonged to, which looks like a
hung turn and invites a needless Stop.
"""

from __future__ import annotations

from concurrent.futures import Future

from gateway.config import PlatformConfig

from . import hire
from .adapter import RetinueRoomsAdapter, _PendingTurn
from .store import RoomStore


def _adapter(tmp_path, monkeypatch) -> RetinueRoomsAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return adapter


def _pend(adapter: RetinueRoomsAdapter, room_id: str, member: str) -> None:
    """Register an in-flight turn the way _dispatch_turn does."""
    adapter._pending[(room_id, member)] = _PendingTurn(
        task_id=f"room-{room_id}-{member}-1",
        room_id=room_id,
        member=member,
        future=Future(),
    )


def test_busy_rooms_names_the_room_the_turn_is_in(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _pend(adapter, "r-a", "mangus")

    assert adapter.busy_rooms_by_slug() == {"mangus": ["r-a"]}


def test_idle_members_are_absent_not_empty(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _pend(adapter, "r-a", "mangus")

    assert "scout" not in adapter.busy_rooms_by_slug()


def test_one_agent_can_be_busy_in_several_rooms(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    _pend(adapter, "r-b", "mangus")
    _pend(adapter, "r-a", "mangus")

    assert adapter.busy_rooms_by_slug()["mangus"] == ["r-a", "r-b"]


def test_busy_slugs_stays_global(tmp_path, monkeypatch):
    """The model-switch guard wants "working anywhere" — do not narrow it."""
    adapter = _adapter(tmp_path, monkeypatch)
    _pend(adapter, "r-a", "mangus")

    assert adapter.busy_slugs() == {"mangus"}


def test_no_pending_turns_is_empty(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)

    assert adapter.busy_rooms_by_slug() == {}
    assert adapter.busy_slugs() == set()


def test_agent_payload_carries_busy_rooms_alongside_busy(tmp_path, monkeypatch):
    """The regression, at the payload the web UI actually reads.

    Before #168 this agent's payload said `busy: true` and nothing else, so a
    room-B view had no way to tell his turn was in room A.
    """
    adapter = _adapter(tmp_path, monkeypatch)
    hire.scaffold_profile(str(tmp_path), "Mangus", "build things", "be terse")
    _pend(adapter, "r-a", "mangus")

    [agent] = [a for a in adapter.list_agents() if a["slug"] == "mangus"]

    assert agent["busy"] is True
    assert agent["busy_rooms"] == ["r-a"]


def test_idle_agent_payload_is_busy_false_with_no_rooms(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, monkeypatch)
    hire.scaffold_profile(str(tmp_path), "Scout", "research", "be terse")

    [agent] = [a for a in adapter.list_agents() if a["slug"] == "scout"]

    assert agent["busy"] is False
    assert agent["busy_rooms"] == []
