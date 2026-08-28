"""Room persistence — meta JSON (atomic tmp+rename) + transcript JSONL.

Plain files, no SQLite: the adapter serializes writes per room, volumes are
tiny, and append-only JSONL keeps the transcript trivially inspectable and
sync-safe (no WAL companions; see IDE constitution §15 for why that matters
in this ecosystem).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from .engine import Room, RoomMessage


def default_base_dir() -> str:
    """Rooms live under the (default profile's) Hermes home.

    The adapter is a default-profile-owned, port-binding platform, so the
    plain env/home resolution is correct here — per-profile contextvar
    overrides apply to member turns, not to room storage.
    """
    home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "retinue_rooms")


# The operator-meaningful half of a room, in stable order. Everything else in
# the meta file is live state the engine rewrites constantly (created_at,
# last_seen, needs_user) and must never reach an exported projection — a
# projection that churned on every transcript message would teach anyone
# tracking the directory to ignore its diffs.
COMPOSITION_FIELDS = (
    "id",
    "name",
    "members",
    "lead",
    "max_agent_turns",
    "max_followup_rounds",
    "archived",
    "workspace",
    "ide_path",
    "worktree_repos",
    "shared_mode",
    "project_id",
)

_HOME_TOKEN = "$HOME"


def default_templates_dir() -> Optional[str]:
    """Optional directory that mirrors each room's composition (issue #229).

    Unset → the feature is off and the store writes nothing new anywhere.
    """
    return os.getenv("RETINUE_ROOM_TEMPLATES_DIR") or None


def _portable_path(value: Optional[str]) -> Optional[str]:
    """Store host paths relative to $HOME so a projection is host-portable."""
    if not value:
        return value
    home = os.path.expanduser("~")
    return _HOME_TOKEN + value[len(home):] if value.startswith(home) else value


def composition_projection(room_dict: Dict[str, object]) -> Dict[str, object]:
    out = {k: room_dict.get(k) for k in COMPOSITION_FIELDS if k in room_dict}
    if "ide_path" in out:
        out["ide_path"] = _portable_path(out.get("ide_path"))  # type: ignore[arg-type]
    return out


class RoomStore:
    def __init__(
        self, base_dir: Optional[str] = None, templates_dir: Optional[str] = None
    ):
        self.base_dir = base_dir or default_base_dir()
        os.makedirs(self.base_dir, exist_ok=True)
        self.templates_dir = templates_dir or default_templates_dir()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._next_seq: Dict[str, int] = {}
        self._backfill_templates()

    # ── paths ────────────────────────────────────────────────────────────

    def _meta_path(self, room_id: str) -> str:
        return os.path.join(self.base_dir, f"{room_id}.json")

    def _transcript_path(self, room_id: str) -> str:
        return os.path.join(self.base_dir, f"{room_id}.transcript.jsonl")

    # ── rooms ────────────────────────────────────────────────────────────

    def create(self, room: Room) -> Room:
        with self._lock:
            self._write_meta(room)
        return room

    def get(self, room_id: str) -> Optional[Room]:
        try:
            with open(self._meta_path(room_id), encoding="utf-8") as f:
                return Room.from_dict(json.load(f))
        except (OSError, ValueError, KeyError):
            return None

    def list_rooms(self) -> List[Room]:
        rooms = []
        try:
            names = sorted(os.listdir(self.base_dir))
        except OSError:
            return []
        for name in names:
            if name.endswith(".itinerary.json"):
                continue
            if name.endswith(".json"):
                room = self.get(name[: -len(".json")])
                if room is not None:
                    rooms.append(room)
        return rooms

    def delete(self, room_id: str) -> bool:
        with self._lock:
            found = False
            for path in (self._meta_path(room_id), self._transcript_path(room_id)):
                try:
                    os.remove(path)
                    found = True
                except FileNotFoundError:
                    pass
            self._next_seq.pop(room_id, None)
            self._remove_template(room_id)
            return found

    def update(self, room: Room) -> None:
        with self._lock:
            self._write_meta(room)

    def mutate(self, room_id: str, fn) -> Room:
        """Load-modify-write room meta under the store lock.

        Incremental membership edits use this so two invites cannot
        last-write-wins each other the way a pair of full-array PATCHes can.
        """
        with self._lock:
            try:
                with open(self._meta_path(room_id), encoding="utf-8") as f:
                    room = Room.from_dict(json.load(f))
            except (OSError, ValueError, KeyError):
                raise KeyError(room_id)
            fn(room)
            self._write_meta(room)
            return room

    def touch_last_seen(self, room_id: str, member: str, seq: int) -> None:
        """Merge one member's last_seen without clobbering siblings.

        Parallel turns update last_seen concurrently; a full-room rewrite
        of a stale Room object would drop the other member's cursor.
        """
        with self._lock:
            room = None
            try:
                with open(self._meta_path(room_id), encoding="utf-8") as f:
                    room = Room.from_dict(json.load(f))
            except (OSError, ValueError, KeyError):
                return
            room.last_seen[member] = max(room.last_seen.get(member, 0), int(seq))
            self._write_meta(room)

    def restore_last_seen(
        self, room_id: str, member: str, previous: int, tentative: int
    ) -> None:
        """Rewind *member* to *previous* iff the stored cursor is still *tentative*.

        A failed turn uses this so the next cycle re-sees the same delta.
        If another completed turn already moved the cursor past
        *tentative*, leave that advance alone — never regress someone
        else's (or a later same-member) legitimate stick.
        """
        with self._lock:
            try:
                with open(self._meta_path(room_id), encoding="utf-8") as f:
                    room = Room.from_dict(json.load(f))
            except (OSError, ValueError, KeyError):
                return
            current = int(room.last_seen.get(member, 0))
            if current != int(tentative):
                return
            room.last_seen[member] = max(0, int(previous))
            self._write_meta(room)

    def _write_meta(self, room: Room) -> None:
        path = self._meta_path(room.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(room.to_dict(), f, indent=2)
        os.replace(tmp, path)
        self._export_template(room.to_dict())

    # ── composition templates (issue #229) ───────────────────────────────

    def _template_path(self, room_id: str) -> str:
        assert self.templates_dir is not None
        return os.path.join(self.templates_dir, f"{room_id}.json")

    def _export_template(self, room_dict: Dict) -> None:
        """Mirror the composition projection; never let it break room writes."""
        if not self.templates_dir:
            return
        try:
            payload = json.dumps(composition_projection(room_dict), indent=2) + "\n"
            path = self._template_path(str(room_dict["id"]))
            try:
                with open(path, encoding="utf-8") as f:
                    if f.read() == payload:
                        return  # volatile-state write; composition unchanged
            except OSError:
                pass
            os.makedirs(self.templates_dir, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass

    def _remove_template(self, room_id: str) -> None:
        if not self.templates_dir:
            return
        try:
            os.remove(self._template_path(room_id))
        except OSError:
            pass

    def _backfill_templates(self) -> None:
        """Converge the templates dir with the live rooms at startup.

        Enabling the feature on an existing install must not require any
        manual capture: export every room, prune projection-shaped files
        whose room is gone. Only files whose JSON carries an ``id`` matching
        their basename are pruned — anything else in the directory is not
        ours to delete.
        """
        if not self.templates_dir:
            return
        try:
            live = {room.id: room for room in self.list_rooms()}
            for room in live.values():
                self._export_template(room.to_dict())
            for name in os.listdir(self.templates_dir):
                if not name.endswith(".json"):
                    continue
                room_id = name[: -len(".json")]
                if room_id in live:
                    continue
                path = os.path.join(self.templates_dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        if json.load(f).get("id") == room_id:
                            os.remove(path)
                except (OSError, ValueError):
                    continue
        except OSError:
            pass

    # ── transcript ───────────────────────────────────────────────────────

    def append(self, room_id: str, message: RoomMessage) -> RoomMessage:
        """Assign the next seq and durably append. Caller provides seq=0."""
        import time as _time

        with self._cv:
            if message.seq <= 0:
                message.seq = self._peek_next_seq(room_id)
            self._next_seq[room_id] = message.seq + 1
            if message.ts <= 0:
                message.ts = _time.time()
            with open(self._transcript_path(room_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(message.to_dict()) + "\n")
            self._cv.notify_all()
        return message

    def _peek_next_seq(self, room_id: str) -> int:
        cached = self._next_seq.get(room_id)
        if cached is not None:
            return cached
        last = 0
        for msg in self._read_all(room_id):
            last = max(last, msg.seq)
        return last + 1

    def read_since(self, room_id: str, since_seq: int = 0) -> List[RoomMessage]:
        return [m for m in self._read_all(room_id) if m.seq > since_seq]

    def wait_since(
        self, room_id: str, since_seq: int = 0, timeout: float = 0.0
    ) -> List[RoomMessage]:
        """Block until a message newer than *since_seq* arrives, or *timeout*.

        Used by both the long-poll transcript route and the SSE stream so
        neither path busy-loops. *timeout* <= 0 is a non-blocking read.
        """
        if timeout <= 0:
            return self.read_since(room_id, since_seq)
        deadline = time.time() + timeout
        with self._cv:
            while True:
                messages = [m for m in self._read_all(room_id) if m.seq > since_seq]
                if messages:
                    return messages
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cv.wait(timeout=remaining)

    def _read_all(self, room_id: str) -> List[RoomMessage]:
        messages: List[RoomMessage] = []
        try:
            with open(self._transcript_path(room_id), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(RoomMessage.from_dict(json.loads(line)))
                    except (ValueError, KeyError):
                        continue  # skip a torn/corrupt line rather than fail the room
        except FileNotFoundError:
            pass
        return messages
