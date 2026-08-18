"""Cross-room post — a retainer in two rooms can speak into the other one.

Membership is the whole authorization model (novique-ai/retinue#128). A
retainer invited to rooms A and B may post from A into B; a retainer
invited only to A may not, and asking to fails closed rather than
silently no-oping.

Three properties this module exists to guarantee:

1. **Identity is not an argument.** The caller's profile is read from the
   turn's runtime scope, never from tool args. A model that types someone
   else's slug into the payload cannot borrow their membership.
2. **The destination does not start a cycle.** Delivery is a plain
   transcript append. Cycles begin at ``post_user_message`` only, so an
   agent line landing in B schedules nobody. Live ``@mentions`` are
   additionally defanged: a handoff that can never be honoured must not
   look like one on B's transcript.
3. **Ambiguity is a refusal.** Two rooms whose names both match a token
   resolve to nothing, exactly like ``resolve_mention`` refuses to let an
   ambiguous ``@S`` steal a turn.

Pure domain logic — no gateway imports, no store writes. The tool surface
in ``tools.py`` supplies the store and the resolved identity; everything
here is unit-testable without a running Hermes (see test_crossroom.py).
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

from .engine import _MENTION_RE, Room, blank_fences, mention_index, resolve_mention

# A cross-room line is a courtesy note, not a document transfer. Text-only
# v1: no attachments, no transcript reads, no /workspace path rewriting
# (B's container is a different workspace, so an A path would 404 there).
MAX_POST_CHARS = 4000

# Which room the in-flight turn is speaking in. A ContextVar, for the same
# reason the workspace overlay is one (tools/workspace_context.py): it is
# per-asyncio-task, and ``tools.thread_context`` copies the whole context
# into the worker threads that dispatch tools — so a value set at the top
# of a cycle is still correct inside a tool call, with nothing global to
# race and nothing to serialize. It is also why the source room cannot be
# a tool argument: a model could then claim to be speaking from anywhere.
_current_room: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "retinue_current_room", default=None
)


def current_room_id() -> Optional[str]:
    """Room id of the in-flight turn, or ``None`` outside a room cycle."""
    return _current_room.get()


@contextmanager
def in_room(room_id: Optional[str]) -> Iterator[Optional[str]]:
    """Bind *room_id* as the current room for the duration of the block."""
    token = _current_room.set(room_id or None)
    try:
        yield room_id
    finally:
        _current_room.reset(token)


# Refusal reasons. Callers turn these into prose; tests assert on them so a
# reworded error message never quietly becomes a different behaviour.
REASON_UNKNOWN = "unknown"
REASON_AMBIGUOUS = "ambiguous"
REASON_NOT_A_MEMBER = "not_a_member"
REASON_SAME_ROOM = "same_room"


def workspace_home(home_dir: str) -> str:
    """Collapse a member's profile home to the workspace home.

    Rooms are stored under the **default** profile's home, but a member's
    turn runs with ``HERMES_HOME`` pointed at
    ``<home>/profiles/<member>``. Reading the room store from inside a turn
    without this would open an empty directory and report — quite wrongly —
    that the retainer belongs to no rooms at all.
    """
    path = os.path.abspath(home_dir or "")
    parent, name = os.path.split(path)
    if os.path.basename(parent) == "profiles" and name:
        return os.path.dirname(parent)
    return path


def caller_member(home_dir: str) -> Optional[str]:
    """Profile name of the turn reading *home_dir*, or ``None``.

    ``<home>/profiles/<slug>`` → ``slug``; a bare workspace home is the
    ``default`` profile. This is the authorization subject: it comes from
    the runtime scope the gateway established for the turn, so it cannot be
    supplied — or altered — by the model.

    An empty home is no scope at all, and returns ``None`` rather than
    falling through ``abspath("")`` to the process working directory —
    which would hand out the ``default`` identity to a caller who has none.
    """
    if not (home_dir or "").strip():
        return None
    path = os.path.abspath(home_dir)
    parent, name = os.path.split(path)
    if os.path.basename(parent) == "profiles" and name:
        return name
    return "default" if name else None


def member_rooms(rooms: List[Room], member: str) -> List[Room]:
    """Live rooms *member* belongs to, in stable name order.

    Archived rooms are hidden from the sidebar and are not a destination:
    posting into one would land where nobody is looking.
    """
    who = (member or "").strip()
    if not who:
        return []
    hits = [r for r in rooms if not r.archived and who in r.members]
    return sorted(hits, key=lambda r: (r.name.lower(), r.id))


def other_rooms(rooms: List[Room], member: str, current_room_id: str) -> List[Room]:
    """``member_rooms`` minus the room the turn is already speaking in."""
    return [r for r in member_rooms(rooms, member) if r.id != current_room_id]


def resolve_destination(
    token: str, candidates: List[Room]
) -> Tuple[Optional[Room], str]:
    """Resolve *token* to one of *candidates*. Returns ``(room, reason)``.

    Match order, most specific first: exact room id, exact name
    (case-insensitive), then a unique case-insensitive name prefix. Every
    tier refuses on a tie — a token matching two rooms returns
    ``(None, "ambiguous")`` rather than picking one, because picking one
    means a message delivered to the wrong room with no way to tell.

    *candidates* is expected to be pre-filtered to rooms the caller belongs
    to, which is what makes an unknown-but-real room indistinguishable from
    a room that does not exist: a non-member learns nothing about rooms
    they were not invited to.
    """
    key = (token or "").strip()
    if not key:
        return None, REASON_UNKNOWN
    for room in candidates:
        if room.id == key:
            return room, ""
    lowered = key.lower()
    exact = [r for r in candidates if r.name.strip().lower() == lowered]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, REASON_AMBIGUOUS
    prefixed = [r for r in candidates if r.name.strip().lower().startswith(lowered)]
    if len(prefixed) == 1:
        return prefixed[0], ""
    if len(prefixed) > 1:
        return None, REASON_AMBIGUOUS
    return None, REASON_UNKNOWN


def defang_mentions(
    text: str,
    members: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> str:
    """Strip the ``@`` from tokens that resolve to a *members* slug.

    The destination append does not run the turn engine, so an ``@Dave`` in
    a cross-room line is already inert. Leaving it looking live is the
    problem: B's transcript would show a handoff that nobody will ever
    take, and Dave's next turn would read it as a request aimed at him.
    The name survives, the trigger does not.

    Mentions inside fenced code are copy, not handoffs — ``blank_fences``
    preserves offsets, so a match is live exactly when the blanked copy
    still has an ``@`` at that position.
    """
    body = text or ""
    if not body or not members:
        return body
    blanked = blank_fences(body)
    index = mention_index(list(members), display_names)
    out: List[str] = []
    cursor = 0
    for match in _MENTION_RE.finditer(body):
        start = match.start()
        if blanked[start : start + 1] != "@":
            continue  # inside a fence — literal copy
        if resolve_mention(match.group(1), index) is None:
            continue  # not a member of the destination; leave it alone
        out.append(body[cursor:start])
        out.append(match.group(1))
        cursor = match.end()
    out.append(body[cursor:])
    return "".join(out)


def destination_line(source_room_name: str, text: str) -> str:
    """The line as it appears in the destination room.

    Attribution is the speaker field (the retainer), so this only has to
    say where the line came from. Provenance is not decoration: without it
    B's members read an unprompted message with no idea which conversation
    produced it.
    """
    name = (source_room_name or "another room").strip() or "another room"
    return f"(from #{name}) {(text or '').strip()}"


def confirmation_line(destination_room_name: str) -> str:
    """Short receipt for the source room, so the turn is not silent."""
    name = (destination_room_name or "the other room").strip() or "the other room"
    return f"Posted to #{name}."


def refusal_line(reason: str, token: str, candidates: List[Room]) -> str:
    """Prose for a refused post. Never names a room the caller cannot see."""
    asked = (token or "").strip() or "(empty)"
    known = ", ".join(f"#{r.name}" for r in candidates)
    where = f" You are in: {known}." if known else " You are not in any other room."
    if reason == REASON_AMBIGUOUS:
        return f"'{asked}' matches more than one of your rooms — be exact.{where}"
    if reason == REASON_SAME_ROOM:
        return f"'{asked}' is this room. Just reply here."
    if reason == REASON_NOT_A_MEMBER:
        return (
            f"You are not a member of '{asked}', so you cannot post there."
            f"{where}"
        )
    return f"No room of yours matches '{asked}'.{where}"


def briefing_rooms_line(rooms: List[Room]) -> Optional[str]:
    """Briefing sentence listing the caller's other rooms, or ``None``.

    A capability nobody is told about is a capability nobody uses. The
    retainer is told which rooms it can reach and nothing about rooms it
    cannot — the briefing is the same membership view the gate enforces.
    """
    if not rooms:
        return None
    names = ", ".join(f"#{r.name}" for r in rooms)
    return (
        f"You are also a member of: {names}. You can post a short text "
        f"message into one of those rooms with the `rooms_post` tool "
        f"(`rooms_list` shows them). It posts one line as you and does not "
        f"start a turn there, so use it to hand something over, not to hold "
        f"a conversation. You cannot post into any other room, and you "
        f"cannot read another room's transcript."
    )
