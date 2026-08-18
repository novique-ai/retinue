"""Cross-room tools — ``rooms_list`` and ``rooms_post`` (novique-ai/retinue#128).

A dedicated pair of tools, deliberately narrow. The alternative was to turn
on Hermes' general ``send_message``, which can reach every connected
platform; a retainer that should be able to say one line to the room next
door does not need Telegram, SMS, and email as a side effect. Two tools
that do exactly the invited thing are the smaller blast radius.

Authorization: membership, and only membership. The caller identity comes
from ``HERMES_HOME`` — the gateway pointed it at the member's profile for
this turn — so it is a property of the runtime scope, not of the payload.
Anything unresolvable fails closed with prose the model can act on.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from . import crossroom
from .engine import KIND_AGENT, Room, RoomMessage
from .store import RoomStore

logger = logging.getLogger(__name__)

TOOLSET = "retinue_rooms"


def _home() -> str:
    return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _store() -> RoomStore:
    """Store rooted at the workspace home, not the member's profile home."""
    home = crossroom.workspace_home(_home())
    return RoomStore(base_dir=os.path.join(home, "retinue_rooms"))


def _identity() -> Optional[str]:
    return crossroom.caller_member(_home())


def _display_names(room: Room) -> Dict[str, str]:
    from . import hire

    names: Dict[str, str] = {m: m for m in room.members}
    try:
        for agent in hire.list_agents(crossroom.workspace_home(_home())):
            slug = str(agent.get("slug") or "")
            if slug in names:
                names[slug] = str(agent.get("display_name") or slug)
    except Exception:  # names are cosmetic; never fail a post over them
        logger.debug("cross-room: could not load display names", exc_info=True)
    return names


def _no_identity() -> str:
    return (
        "Error: this tool only works inside a room turn — no member "
        "identity is in scope."
    )


def rooms_list(args: dict | None = None, **_: Any) -> str:
    """Rooms this retainer belongs to, marking the current one."""
    member = _identity()
    if not member:
        return _no_identity()
    current = crossroom.current_room_id()
    rooms = crossroom.member_rooms(_store().list_rooms(), member)
    if not rooms:
        return "You are not a member of any room."
    lines = ["Your rooms:"]
    for room in rooms:
        here = "  (this room)" if room.id == current else ""
        lines.append(f"  - #{room.name}{here}")
    others = [r for r in rooms if r.id != current]
    if others:
        lines.append("")
        lines.append(
            "Post one line into any of the others with rooms_post(room=..., "
            "message=...). You cannot read their transcripts."
        )
    return "\n".join(lines)


def rooms_post(args: dict, **_: Any) -> str:
    """Post one text line, as this retainer, into another room they are in."""
    member = _identity()
    if not member:
        return _no_identity()

    args = args or {}
    token = str(args.get("room") or args.get("room_name") or args.get("to") or "").strip()
    message = str(args.get("message") or args.get("text") or "").strip()
    if not token:
        return "Error: 'room' is required (see rooms_list for your rooms)."
    if not message:
        return "Error: 'message' is required."
    if len(message) > crossroom.MAX_POST_CHARS:
        return (
            f"Error: message is {len(message)} characters; the cross-room "
            f"limit is {crossroom.MAX_POST_CHARS}. Post a summary and keep "
            f"the detail in this room."
        )

    store = _store()
    rooms = store.list_rooms()
    current_id = crossroom.current_room_id()
    source = store.get(current_id) if current_id else None
    if source is None:
        return (
            "Error: no source room in scope — rooms_post only works inside a "
            "room turn."
        )

    mine = crossroom.member_rooms(rooms, member)
    candidates = [r for r in mine if r.id != current_id]

    # Resolve against the caller's OWN rooms first. A hit against the whole
    # room list would let a non-member distinguish "no such room" from "a
    # room you were not invited to" — a membership oracle. Same-room and
    # not-a-member are separated only after that, and only for rooms the
    # caller can already see.
    destination, reason = crossroom.resolve_destination(token, candidates)
    if destination is None:
        if reason == crossroom.REASON_UNKNOWN:
            self_hit, self_reason = crossroom.resolve_destination(token, [source])
            if self_hit is not None:
                return crossroom.refusal_line(
                    crossroom.REASON_SAME_ROOM, token, candidates
                )
            if self_reason == crossroom.REASON_AMBIGUOUS:
                return crossroom.refusal_line(
                    crossroom.REASON_AMBIGUOUS, token, candidates
                )
        return crossroom.refusal_line(reason, token, candidates)

    # Belt and braces. resolve_destination only ever saw rooms the caller
    # belongs to, so this cannot fire today — but it is the invariant the
    # whole feature rests on, and an invariant worth stating is worth
    # checking. A future caller passing a wider candidate list fails closed
    # here instead of delivering.
    if member not in destination.members:
        return crossroom.refusal_line(
            crossroom.REASON_NOT_A_MEMBER, token, candidates
        )

    names = _display_names(destination)
    body = crossroom.defang_mentions(message, destination.members, names)
    line = crossroom.destination_line(source.name, body)

    try:
        # A plain transcript append. Turn cycles start at
        # post_user_message() only, so this schedules nobody in the
        # destination — the no-cycle guarantee is structural, not a flag we
        # remembered to pass.
        store.append(
            destination.id,
            RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=member, text=line),
        )
    except Exception as e:
        logger.exception("cross-room post to %s failed", destination.id)
        return f"Error: could not post to #{destination.name} — {e}."

    return crossroom.confirmation_line(destination.name)


_SCHEMAS: Dict[str, dict] = {
    "rooms_list": {
        "type": "function",
        "function": {
            "name": "rooms_list",
            "description": (
                "List the Retinue rooms you are a member of, marking the one "
                "you are speaking in now. Use it before rooms_post when you "
                "are unsure of a room's exact name."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    "rooms_post": {
        "type": "function",
        "function": {
            "name": "rooms_post",
            "description": (
                "Post one short text message, attributed to you, into another "
                "Retinue room you are a member of. Use it to hand something "
                "to a room you also belong to — a result, a heads-up, a "
                "pointer. It does NOT start a turn there and nobody is "
                "@mentioned, so treat it as leaving a note, not starting a "
                "conversation. You cannot post into a room you are not a "
                "member of, and you cannot read another room's transcript."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "description": (
                            "Destination room name (or id) — must be a room "
                            "you are a member of. See rooms_list."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The text to post. Plain text only; say which "
                            "room it came from in your own words if it needs "
                            "context."
                        ),
                    },
                },
                "required": ["room", "message"],
            },
        },
    },
}

_HANDLERS = {
    "rooms_list": rooms_list,
    "rooms_post": rooms_post,
}


def register_tools(ctx) -> None:
    """Register the cross-room tools in the ``retinue_rooms`` toolset."""
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji="\U0001f6aa",  # door
        )
