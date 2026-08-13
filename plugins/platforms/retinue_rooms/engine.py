"""Room domain logic — pure and gateway-independent.

Everything in this module is deliberately free of gateway imports so the
turn-taking rules can be unit-tested without a running Hermes instance
(see test_engine.py).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SYSTEM = "system"

DEFAULT_MAX_AGENT_TURNS = 8

# @name — profile names may contain letters, digits, underscores, hyphens.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_-]*)")


@dataclass
class RoomMessage:
    seq: int
    ts: float
    kind: str  # KIND_USER | KIND_AGENT | KIND_SYSTEM
    speaker: str  # user display name, or the member profile name for agents
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoomMessage":
        return cls(
            seq=int(data["seq"]),
            ts=float(data.get("ts") or 0.0),
            kind=str(data.get("kind") or KIND_USER),
            speaker=str(data.get("speaker") or ""),
            text=str(data.get("text") or ""),
        )


@dataclass
class Room:
    id: str
    name: str
    members: List[str]  # Hermes profile names ("default" is allowed)
    lead: Optional[str] = None  # default responder when nobody is mentioned
    max_agent_turns: int = DEFAULT_MAX_AGENT_TURNS
    created_at: float = field(default_factory=time.time)
    # member -> highest transcript seq already delivered to that member
    last_seen: Dict[str, int] = field(default_factory=dict)

    def default_responder(self) -> Optional[str]:
        if self.lead and self.lead in self.members:
            return self.lead
        return self.members[0] if self.members else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Room":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            members=[str(m) for m in (data.get("members") or [])],
            lead=data.get("lead") or None,
            max_agent_turns=int(data.get("max_agent_turns") or DEFAULT_MAX_AGENT_TURNS),
            created_at=float(data.get("created_at") or 0.0),
            last_seen={str(k): int(v) for k, v in (data.get("last_seen") or {}).items()},
        )


def new_room_id(name: str) -> str:
    """Stable-ish, filesystem-safe room id: slug of the name + short suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "room").lower()).strip("-")[:32] or "room"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def parse_mentions(text: str, candidates: List[str]) -> List[str]:
    """@-mentions of ``candidates`` in ``text``, in order of first appearance.

    Case-insensitive, de-duplicated; tokens that match no candidate are
    ignored (so "@Mark" in an agent reply never schedules a turn unless
    "Mark" is a member).
    """
    by_lower = {c.lower(): c for c in candidates}
    seen: List[str] = []
    for match in _MENTION_RE.finditer(text or ""):
        member = by_lower.get(match.group(1).lower())
        if member is not None and member not in seen:
            seen.append(member)
    return seen


def plan_user_turns(room: Room, text: str) -> List[str]:
    """Turn queue for a fresh user message: mentioned members in mention
    order, else the room's default responder."""
    mentioned = parse_mentions(text, room.members)
    if mentioned:
        return mentioned
    responder = room.default_responder()
    return [responder] if responder else []


def plan_agent_followups(
    room: Room,
    speaker: str,
    text: str,
    already_queued: List[str],
    budget_left: int,
) -> List[str]:
    """Members an agent reply pulls into the conversation.

    Excludes the speaker (no self-triggering) and members already queued;
    truncated to the remaining turn budget.
    """
    if budget_left <= 0:
        return []
    picks = [
        m
        for m in parse_mentions(text, room.members)
        if m != speaker and m not in already_queued
    ]
    return picks[:budget_left]


def take_wave(queue: List[str], budget_left: int) -> tuple[List[str], List[str]]:
    """Split *queue* into (this independent wave, remainder).

    Members already in the queue were scheduled independently (a user
    @mentioned them together, or they were collected as follow-ups of the
    previous wave). They do not depend on each other's replies, so the
    adapter may run the wave concurrently. *budget_left* caps the wave.
    """
    if budget_left <= 0 or not queue:
        return [], list(queue)
    return list(queue[:budget_left]), list(queue[budget_left:])


def merge_followups(
    room: Room,
    replies: List[tuple[str, str]],
    already_queued: List[str],
    already_spoken: List[str],
    budget_left: int,
) -> List[str]:
    """Next wave: @mentions from a just-finished wave, in speaker order.

    Dedupes against *already_queued*, *already_spoken*, and earlier
    follow-ups in this merge so a member is scheduled at most once.
    """
    extra: List[str] = []
    blocked = set(already_queued) | set(already_spoken)
    remaining = budget_left
    for speaker, text in replies:
        if remaining <= 0:
            break
        picks = plan_agent_followups(
            room, speaker, text, list(blocked), remaining
        )
        extra.extend(picks)
        blocked.update(picks)
        remaining -= len(picks)
    return extra


def format_lines(messages: List[RoomMessage]) -> str:
    """Attributed transcript block for channel_context delivery."""
    lines = []
    for msg in messages:
        label = f"{msg.speaker} (agent)" if msg.kind == KIND_AGENT else msg.speaker
        if msg.kind == KIND_SYSTEM:
            label = "room"
        lines.append(f"[{label}] {msg.text}")
    return "\n".join(lines)


def room_briefing(room: Room, member: str, user_names: List[str]) -> str:
    """Per-turn channel prompt: who you are, who is here, how to behave."""
    others = [m for m in room.members if m != member]
    people = ", ".join(user_names) if user_names else "the user"
    parts = [
        f'You are "{member}", a member of the room "{room.name}".',
        f"Humans here: {people}.",
        (
            "Other agent members: " + ", ".join(others) + "."
            if others
            else "You are the only agent member."
        ),
        "Messages are prefixed [speaker] so you can tell who said what.",
        (
            "To bring another agent member into the conversation, mention them "
            "as @name in your reply; they will be given a turn and can see the "
            "transcript. Only mention someone when their input is actually needed."
        ),
        "Never write lines on behalf of other speakers; reply only as yourself.",
        "Do not prefix your reply with your own name or any [speaker] tag — "
        "the room adds attribution for you.",
        "Keep replies concise and conversational unless asked for detail.",
    ]
    return "\n".join(parts)
