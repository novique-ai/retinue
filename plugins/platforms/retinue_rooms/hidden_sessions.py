"""Hide room-owned Hermes sessions via the upstream ``hidden`` flag (#138).

Room turns create real gateway sessions so the member can run. Those rows
are an implementation detail: they clutter every session list (web
dashboard, CLI, desktop) and invite accidental interaction. Upstream's
durable ``hidden`` flag drops them from the default list while leaving
them fully resumable for later room turns.

Identification is the rooms session-key namespace, not a title heuristic.
``build_session_key`` stamps the platform as a fixed slot:

    agent:<profile-or-main>:retinue_rooms:group:<room>[:<member>]

Regular user sessions (cli, telegram, desktop, …) never occupy that
platform slot. ``create_session`` also records ``source="retinue_rooms"``.
If a row matches neither, it is left alone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

ROOM_PLATFORM = "retinue_rooms"


def is_room_session_key(session_key: str) -> bool:
    """True iff *session_key* is in the rooms adapter's key namespace.

    Keys are colon-delimited ``agent:<ns>:<platform>:<chat_type>:...``.
    The platform slot is uniquely ``retinue_rooms``; a later slot that
    merely contains that string (a chat id, a title) does not match.
    """
    parts = (session_key or "").split(":")
    return len(parts) >= 3 and parts[0] == "agent" and parts[2] == ROOM_PLATFORM


def is_room_session_row(row: Dict[str, Any]) -> bool:
    """True iff *row* is stamped as a rooms-adapter session."""
    source = (row.get("source") or "").strip().lower()
    if source == ROOM_PLATFORM:
        return True
    return is_room_session_key(str(row.get("session_key") or ""))


def hide_session_by_key(db: Any, session_key: str) -> bool:
    """Hide every row in *db* that uses this rooms session key.

    Refuses keys outside the rooms namespace so a caller cannot hide a
    regular user session by accident. Returns True if at least one row
    was updated.
    """
    if not is_room_session_key(session_key):
        return False
    setter = getattr(db, "set_session_hidden", None)
    if not callable(setter):
        return False
    changed = False
    for row in _rows_for_key(db, session_key):
        sid = row.get("id")
        if not sid:
            continue
        try:
            if setter(sid, True):
                changed = True
        except Exception:
            logger.debug(
                "Retinue rooms: failed to hide session %s", sid, exc_info=True
            )
    return changed


def hide_session_in_home(
    home: str, session_key: str, member: Optional[str] = None
) -> bool:
    """Hide *session_key* in the default DB and, if present, the member's."""
    if not is_room_session_key(session_key):
        return False
    changed = False
    for path in _candidate_db_paths(home, member):
        db = _open_session_db(path)
        if db is None:
            continue
        try:
            if hide_session_by_key(db, session_key):
                changed = True
        except Exception:
            logger.debug(
                "Retinue rooms: hide-by-key failed for %s", path, exc_info=True
            )
        finally:
            _close_db(db)
    return changed


def sweep_db(db: Any) -> int:
    """Hide every room-owned session in *db*. Idempotent. Returns rows hidden."""
    setter = getattr(db, "set_session_hidden", None)
    if not callable(setter):
        return 0
    hidden = 0
    for row in _room_session_rows(db):
        sid = row.get("id")
        if not sid:
            continue
        try:
            if setter(sid, True):
                hidden += 1
        except Exception:
            logger.debug(
                "Retinue rooms: sweep failed to hide %s", sid, exc_info=True
            )
    return hidden


def sweep_home(home: str) -> int:
    """Sweep the workspace DB and every ``profiles/<slug>/state.db``."""
    hidden = 0
    for path in _iter_state_db_paths(home):
        db = _open_session_db(path)
        if db is None:
            continue
        try:
            hidden += sweep_db(db)
        except Exception:
            logger.debug(
                "Retinue rooms: sweep of %s failed", path, exc_info=True
            )
        finally:
            _close_db(db)
    return hidden


def on_session_start(session_id: str = "", platform: str = "", **_kwargs) -> None:
    """Plugin hook: hide a session born on the rooms platform.

    Fires on the first turn of a new session, after the row exists, inside
    the member's profile scope (so ``SessionDB()`` opens the right file).
    Other platforms are ignored.
    """
    if (platform or "").strip().lower() != ROOM_PLATFORM:
        return
    sid = (session_id or "").strip()
    if not sid:
        return
    db = _open_session_db(None)
    if db is None:
        return
    try:
        setter = getattr(db, "set_session_hidden", None)
        if callable(setter):
            setter(sid, True)
    except Exception:
        logger.debug(
            "Retinue rooms: on_session_start hide failed for %s", sid, exc_info=True
        )
    finally:
        _close_db(db)


def _rows_for_key(db: Any, session_key: str) -> List[Dict[str, Any]]:
    lister = getattr(db, "list_sessions_rich", None)
    if not callable(lister):
        return []
    try:
        return list(
            lister(
                session_key=session_key,
                include_hidden=True,
                min_message_count=0,
                include_children=True,
                limit=50,
                project_compression_tips=False,
            )
            or []
        )
    except Exception:
        logger.debug(
            "Retinue rooms: list by session_key failed", exc_info=True
        )
        return []


def _room_session_rows(db: Any) -> List[Dict[str, Any]]:
    seen: set = set()
    rows: List[Dict[str, Any]] = []
    gateway_list = getattr(db, "list_gateway_sessions", None)
    if callable(gateway_list):
        try:
            for row in gateway_list(platform=ROOM_PLATFORM, active_only=False) or []:
                _accumulate_room_row(row, seen, rows)
        except Exception:
            logger.debug(
                "Retinue rooms: list_gateway_sessions failed", exc_info=True
            )
    lister = getattr(db, "list_sessions_rich", None)
    if callable(lister):
        try:
            for row in (
                lister(
                    source=ROOM_PLATFORM,
                    include_hidden=True,
                    min_message_count=0,
                    include_children=True,
                    limit=10000,
                    project_compression_tips=False,
                )
                or []
            ):
                _accumulate_room_row(row, seen, rows)
        except Exception:
            logger.debug(
                "Retinue rooms: list_sessions_rich(source=retinue_rooms) failed",
                exc_info=True,
            )
    return rows


def _accumulate_room_row(
    row: Dict[str, Any], seen: set, rows: List[Dict[str, Any]]
) -> None:
    if not is_room_session_row(row):
        return
    sid = row.get("id")
    if not sid or sid in seen:
        return
    seen.add(sid)
    rows.append(row)


def _candidate_db_paths(home: str, member: Optional[str]) -> List[Path]:
    root = Path(home)
    paths = [root / "state.db"]
    slug = (member or "").strip()
    if slug and slug != "default":
        paths.append(root / "profiles" / slug / "state.db")
    return paths


def _iter_state_db_paths(home: str) -> Iterable[Path]:
    root = Path(home)
    yield root / "state.db"
    profiles = root / "profiles"
    if not profiles.is_dir():
        return
    try:
        children = list(profiles.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir():
            yield child / "state.db"


def _open_session_db(path: Optional[Path]):
    try:
        from hermes_state import SessionDB
    except Exception:
        return None
    try:
        if path is None:
            return SessionDB()
        if not path.is_file():
            return None
        return SessionDB(path)
    except Exception:
        logger.debug(
            "Retinue rooms: could not open SessionDB %s", path, exc_info=True
        )
        return None


def _close_db(db: Any) -> None:
    closer = getattr(db, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        pass
