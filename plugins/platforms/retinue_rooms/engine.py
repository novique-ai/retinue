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
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_MENTION_RE = re.compile(r"@(" + _TOKEN_RE.pattern + r")")


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
    # Hidden from the sidebar without wiping the transcript.
    archived: bool = False
    # sandbox (default): isolated container, no host mount.
    # ide: same container runtime, bind-mount of ide_path at /workspace.
    workspace: str = "sandbox"
    ide_path: Optional[str] = None

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
            archived=bool(data.get("archived")),
            workspace=str(data.get("workspace") or "sandbox"),
            ide_path=(str(data["ide_path"]) if data.get("ide_path") else None),
        )


def new_room_id(name: str) -> str:
    """Stable-ish, filesystem-safe room id: slug of the name + short suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "room").lower()).strip("-")[:32] or "room"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def mention_index(
    candidates: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Lowercase alias → member slug.

    Slugs always win. A single-token display name and a unique first
    name are added only when they do not collide with a slug or with
    another member's alias. Prefix resolution lives in ``resolve_mention``.
    """
    names = display_names or {}
    index: Dict[str, str] = {c.lower(): c for c in candidates}
    first_claims: Dict[str, List[str]] = {}
    for slug in candidates:
        raw = (names.get(slug) or "").strip()
        if not raw:
            continue
        full = raw.lower()
        if _TOKEN_RE.fullmatch(raw) and full not in index:
            index[full] = slug
        first = raw.split()[0]
        if first:
            first_claims.setdefault(first.lower(), []).append(slug)
    for first, slugs in first_claims.items():
        if first not in index and len(set(slugs)) == 1:
            index[first] = slugs[0]
    return index


def resolve_mention(token: str, index: Dict[str, str]) -> Optional[str]:
    """Exact alias, else a unique prefix of the alias table.

    Ambiguous prefixes (``@S`` with Sally and Scout) resolve to nothing
    so they do not steal a turn.
    """
    key = (token or "").lower()
    if not key:
        return None
    if key in index:
        return index[key]
    hits = {slug for alias, slug in index.items() if alias.startswith(key)}
    if len(hits) == 1:
        return hits.pop()
    return None


def mention_handle(
    slug: str,
    display_name: Optional[str],
    candidates: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> str:
    """Human token to insert for *slug* (``Sheila``, not the hire slug).

    Prefers the unique full display name, then a unique first name.
    Falls back to the slug when those collide or are not mention tokens.
    """
    names = dict(display_names or {})
    names.setdefault(slug, display_name or slug)
    raw = (names.get(slug) or slug).strip() or slug
    if _TOKEN_RE.fullmatch(raw):
        owners = [
            m
            for m in candidates
            if (names.get(m) or m).strip().lower() == raw.lower()
        ]
        if owners == [slug]:
            return raw
    first = raw.split()[0]
    if not _TOKEN_RE.fullmatch(first):
        return slug
    owners = [
        m
        for m in candidates
        if ((names.get(m) or m).strip().split() or [m])[0].lower() == first.lower()
    ]
    return first if owners == [slug] else slug


def parse_mentions(
    text: str,
    candidates: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """@-mentions of ``candidates`` in ``text``, in order of first appearance.

    Case-insensitive, de-duplicated. Tokens match a slug, a unique
    display / first name, or a unique alias prefix. Tokens that match no
    candidate are ignored (so "@Mark" in an agent reply never schedules
    a turn unless "Mark" is a member).
    """
    index = mention_index(candidates, display_names)
    seen: List[str] = []
    for match in _MENTION_RE.finditer(text or ""):
        member = resolve_mention(match.group(1), index)
        if member is not None and member not in seen:
            seen.append(member)
    return seen


def plan_user_turns(
    room: Room,
    text: str,
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Turn queue for a fresh user message: mentioned members in mention
    order, else the room's default responder."""
    mentioned = parse_mentions(text, room.members, display_names)
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
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Members an agent reply pulls into the conversation.

    Excludes the speaker (no self-triggering) and members already queued;
    truncated to the remaining turn budget.
    """
    if budget_left <= 0:
        return []
    picks = [
        m
        for m in parse_mentions(text, room.members, display_names)
        if m != speaker and m not in already_queued
    ]
    return picks[:budget_left]


def take_wave(queue: List[str], budget_left: int) -> tuple[List[str], List[str]]:
    """Split *queue* into (this speaker, remainder).

    Rooms take turns. Mention order (then follow-up ``@mention``s from a
    reply) is a queue, not a fan-out: the next speaker must see the
    previous reply on the transcript. *budget_left* still gates whether
    anyone runs. An explicit parallel control is later; it is not the
    default.
    """
    if budget_left <= 0 or not queue:
        return [], list(queue)
    return [queue[0]], list(queue[1:])


def merge_followups(
    room: Room,
    replies: List[tuple[str, str]],
    already_queued: List[str],
    already_spoken: List[str],
    budget_left: int,
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Follow-up ``@mention``s from a just-finished speaker (or speakers).

    Dedupes against *already_queued*, *already_spoken*, and earlier
    follow-ups in this merge so a member is scheduled at most once.
    Sequential cycles pass a single reply; the list form stays so a
    later explicit parallel control can reuse the same merge.
    """
    extra: List[str] = []
    blocked = set(already_queued) | set(already_spoken)
    remaining = budget_left
    for speaker, text in replies:
        if remaining <= 0:
            break
        picks = plan_agent_followups(
            room, speaker, text, list(blocked), remaining, display_names
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
    if (room.workspace or "sandbox") == "ide":
        parts.append(
            "This room is attached to this machine's IDE. Your terminal "
            "/workspace is a bind-mount of that host tree — treat it as the "
            "real project, not a throwaway sandbox."
        )
    else:
        parts.append(
            "This room is sandboxed. Your terminal /workspace is an isolated "
            "container with no host IDE mount."
        )
    return "\n".join(parts)
