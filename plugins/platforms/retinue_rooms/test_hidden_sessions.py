"""Hide room-owned sessions via the upstream hidden-session flag (issue #138).

Room turns create Hermes gateway sessions under the ``retinue_rooms``
session-key namespace. Those rows are an implementation detail and must
not appear in the shared session lists. Regular user sessions (cli,
telegram, desktop, …) are never touched.
"""

from __future__ import annotations

import asyncio

import pytest
from gateway.config import PlatformConfig
from hermes_state import SessionDB

from . import hidden_sessions
from .adapter import RetinueRoomsAdapter
from .engine import KIND_USER, Room, RoomMessage
from .store import RoomStore


ROOM_KEY = "agent:main:retinue_rooms:group:r-1:scout"
PROFILE_ROOM_KEY = "agent:scout:retinue_rooms:group:r-1:scout"
CLI_KEY = "agent:main:cli:direct:user-1"
TELEGRAM_KEY = "agent:main:telegram:group:chat-9:user-2"
DESKTOP_ID = "20260819_120000_abcd1234"


def _db(path):
    database = SessionDB(path)
    return database


def _seed(db: SessionDB, session_id: str, *, source: str, session_key: str) -> None:
    db.create_session(session_id, source=source, session_key=session_key)
    db._conn.execute(
        "UPDATE sessions SET message_count = 1 WHERE id = ?", (session_id,)
    )
    db._conn.commit()


def _visible_ids(db: SessionDB) -> set[str]:
    return {
        s["id"]
        for s in db.list_sessions_rich(min_message_count=1)
    }


def _all_ids(db: SessionDB) -> set[str]:
    return {
        s["id"]
        for s in db.list_sessions_rich(min_message_count=1, include_hidden=True)
    }


def _adapter(tmp_path, monkeypatch) -> RetinueRoomsAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    inst = RetinueRoomsAdapter(PlatformConfig())
    inst.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    return inst


def test_is_room_session_key_matches_only_the_rooms_namespace():
    """The platform slot is uniquely ``retinue_rooms`` — not a title/heuristic."""
    assert hidden_sessions.is_room_session_key(ROOM_KEY) is True
    assert hidden_sessions.is_room_session_key(PROFILE_ROOM_KEY) is True
    assert hidden_sessions.is_room_session_key("agent:main:retinue_rooms:group:r-1") is True
    assert hidden_sessions.is_room_session_key(CLI_KEY) is False
    assert hidden_sessions.is_room_session_key(TELEGRAM_KEY) is False
    assert hidden_sessions.is_room_session_key(DESKTOP_ID) is False
    assert hidden_sessions.is_room_session_key("") is False
    # Substring in a later slot must not count as the platform namespace.
    assert hidden_sessions.is_room_session_key(
        "agent:main:telegram:group:retinue_rooms:user"
    ) is False


def test_hide_created_room_session_is_dropped_from_default_list(tmp_path):
    """Creation path: a newly created rooms session is marked hidden."""
    db = _db(tmp_path / "state.db")
    try:
        _seed(db, "room-sid", source="retinue_rooms", session_key=ROOM_KEY)
        _seed(db, "cli-sid", source="cli", session_key=CLI_KEY)
        assert _visible_ids(db) == {"room-sid", "cli-sid"}

        hidden = hidden_sessions.hide_session_by_key(db, ROOM_KEY)
        assert hidden is True
        assert db.get_session("room-sid")["hidden"] == 1
        assert db.get_session("cli-sid")["hidden"] == 0
        assert _visible_ids(db) == {"cli-sid"}
        assert _all_ids(db) == {"room-sid", "cli-sid"}
    finally:
        db.close()


def test_hide_session_by_key_refuses_non_room_keys(tmp_path):
    """If the key is not in the rooms namespace, do not hide anything."""
    db = _db(tmp_path / "state.db")
    try:
        _seed(db, "cli-sid", source="cli", session_key=CLI_KEY)
        assert hidden_sessions.hide_session_by_key(db, CLI_KEY) is False
        assert db.get_session("cli-sid")["hidden"] == 0
        assert _visible_ids(db) == {"cli-sid"}
    finally:
        db.close()


def test_sweep_hides_preexisting_room_sessions_and_is_idempotent(tmp_path):
    db = _db(tmp_path / "state.db")
    try:
        _seed(db, "room-a", source="retinue_rooms", session_key=ROOM_KEY)
        _seed(
            db,
            "room-b",
            source="retinue_rooms",
            session_key="agent:editor:retinue_rooms:group:r-2:editor",
        )
        _seed(db, "cli-sid", source="cli", session_key=CLI_KEY)
        _seed(db, "desk-sid", source="desktop", session_key=DESKTOP_ID)

        first = hidden_sessions.sweep_db(db)
        assert first >= 2
        assert db.get_session("room-a")["hidden"] == 1
        assert db.get_session("room-b")["hidden"] == 1
        assert db.get_session("cli-sid")["hidden"] == 0
        assert db.get_session("desk-sid")["hidden"] == 0
        assert _visible_ids(db) == {"cli-sid", "desk-sid"}

        second = hidden_sessions.sweep_db(db)
        assert second >= 0  # idempotent: already-hidden rows are a no-op or re-set
        assert db.get_session("room-a")["hidden"] == 1
        assert db.get_session("cli-sid")["hidden"] == 0
        assert db.get_session("desk-sid")["hidden"] == 0
        assert _visible_ids(db) == {"cli-sid", "desk-sid"}
    finally:
        db.close()


def test_sweep_home_walks_default_and_profile_dbs(tmp_path):
    """Multiplex stores member sessions in that profile's state.db."""
    default = _db(tmp_path / "state.db")
    profile_dir = tmp_path / "profiles" / "scout"
    profile_dir.mkdir(parents=True)
    profile = _db(profile_dir / "state.db")
    try:
        _seed(default, "root-room", source="retinue_rooms", session_key=ROOM_KEY)
        _seed(default, "root-cli", source="cli", session_key=CLI_KEY)
        _seed(profile, "scout-room", source="retinue_rooms", session_key=PROFILE_ROOM_KEY)
        _seed(profile, "scout-cli", source="cli", session_key="agent:scout:cli:direct:u")

        hidden_sessions.sweep_home(str(tmp_path))

        assert default.get_session("root-room")["hidden"] == 1
        assert default.get_session("root-cli")["hidden"] == 0
        assert profile.get_session("scout-room")["hidden"] == 1
        assert profile.get_session("scout-cli")["hidden"] == 0
    finally:
        default.close()
        profile.close()


def test_on_session_start_hides_only_retinue_rooms_platform(tmp_path, monkeypatch):
    """Creation-path hook: hide when the agent reports platform=retinue_rooms."""
    import hermes_state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Under the tests/ conftest, an autouse fixture re-pins DEFAULT_DB_PATH and
    # _default_db_path() prefers that pin over a fresh HERMES_HOME resolution —
    # so the hook's argless SessionDB() would open the conftest's DB, not ours.
    # Pin it to the DB this test asserts against (same pattern as
    # tests/gateway/test_session.py). Harmless via the plugin path.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = _db(tmp_path / "state.db")
    try:
        _seed(db, "room-sid", source="retinue_rooms", session_key=ROOM_KEY)
        _seed(db, "cli-sid", source="cli", session_key=CLI_KEY)

        hidden_sessions.on_session_start("room-sid", platform="retinue_rooms")
        hidden_sessions.on_session_start("cli-sid", platform="cli")

        assert db.get_session("room-sid")["hidden"] == 1
        assert db.get_session("cli-sid")["hidden"] == 0
    finally:
        db.close()


def test_agent_turn_hides_the_session_it_just_created(tmp_path, monkeypatch):
    """Adapter creation path: after handle_message, the member session is hidden."""
    adapter = _adapter(tmp_path, monkeypatch)
    adapter.store.create(Room(id="r-1", name="Test", members=["scout"], lead="scout"))
    adapter.store.append(
        "r-1",
        RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker="You", text="hello scout"),
    )
    room = adapter.store.get("r-1")

    db = _db(tmp_path / "state.db")
    created = {}

    async def fake_handle(event):
        key = adapter._session_key_for(event.source)
        created["key"] = key
        _seed(db, "turn-sid", source="retinue_rooms", session_key=key)
        adapter._resolve_pending(room.id, ok=True, text="hi", member="scout")

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    ok, text = asyncio.run(adapter._agent_turn(room, "scout"))
    assert ok is True
    assert text == "hi"
    assert hidden_sessions.is_room_session_key(created["key"]) is True
    assert db.get_session("turn-sid")["hidden"] == 1
    assert _visible_ids(db) == set()
    db.close()


def test_adapter_connect_sweeps_preexisting_room_sessions(tmp_path, monkeypatch):
    from . import ide

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RETINUE_ROOMS_HOST", "127.0.0.1")
    monkeypatch.setenv("RETINUE_ROOMS_PORT", "0")
    monkeypatch.setattr(ide, "docker_backend_error", lambda: None)

    db = _db(tmp_path / "state.db")
    _seed(db, "old-room", source="retinue_rooms", session_key=ROOM_KEY)
    _seed(db, "old-cli", source="cli", session_key=CLI_KEY)

    adapter = RetinueRoomsAdapter(PlatformConfig())
    adapter.store = RoomStore(base_dir=str(tmp_path / "rooms"))
    try:
        assert asyncio.run(adapter.connect()) is True
        assert db.get_session("old-room")["hidden"] == 1
        assert db.get_session("old-cli")["hidden"] == 0
    finally:
        asyncio.run(adapter.disconnect())
        db.close()
