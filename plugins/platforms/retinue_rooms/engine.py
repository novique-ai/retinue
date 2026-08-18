"""Room domain logic — pure and gateway-independent.

Everything in this module is deliberately free of gateway imports so the
turn-taking rules can be unit-tested without a running Hermes instance
(see test_engine.py).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional

KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SYSTEM = "system"

# System-notice prefixes the web UI uses to drop a thinking bubble.
# Keep in lockstep with retinue-web/src/thinking.ts.
CYCLE_INTERNAL_ERROR_PREFIX = "internal error running the turn cycle"
CYCLE_BUDGET_PREFIX = "turn budget"
CYCLE_STOPPED_PREFIX = "Stopped."
DID_NOT_REPLY_INFIX = " did not reply ("

DEFAULT_MAX_AGENT_TURNS = 8

# On invite, seed last_seen so the newcomer receives only the last N
# messages rather than the whole transcript. This is not a compromise
# and is not laziness: room_briefing already includes the itinerary
# when the lead keeps one, and the itinerary is already a lead-authored
# outline of the room's purpose. "The itinerary plus the last N turns"
# — the briefed join — falls out of seeding last_seen. No summarisation
# call, no new prompt path.
INVITE_TRANSCRIPT_WINDOW = 20

# @name — profile names may contain letters, digits, underscores, hyphens.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_MENTION_RE = re.compile(r"@(" + _TOKEN_RE.pattern + r")")

# User-only broadcast. Reserved — do not hire a retainer whose slug is ``room``.
ROOM_BROADCAST_TOKEN = "room"


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
    # /shared mount: "rw" (default) or "ro". Absent treated as "rw";
    # unknown values on disk stay read-only.
    shared_mode: Optional[str] = None
    # Projects group rooms (see projects.py). None = Unfiled. This is the
    # only membership record — do not also track room ids on the project.
    project_id: Optional[str] = None

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
            shared_mode=(str(data["shared_mode"]) if data.get("shared_mode") else None),
            project_id=(str(data["project_id"]) if data.get("project_id") else None),
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


def blank_fences(text: str) -> str:
    """Replace fenced code (``` / ~~~) with spaces, keeping offsets.

    Mentions inside a blog-draft fence are literal copy, not a handoff.
    Headings and list items stay live.
    """
    if not text:
        return ""
    out: List[str] = []
    i = 0
    n = len(text)
    fence: Optional[str] = None
    while i < n:
        if fence is None:
            if text.startswith("```", i) or text.startswith("~~~", i):
                fence = text[i : i + 3]
                out.append("   ")
                i += 3
            else:
                out.append(text[i])
                i += 1
        elif text.startswith(fence, i):
            out.append("   ")
            i += len(fence)
            fence = None
        else:
            out.append(" ")
            i += 1
    return "".join(out)


def parse_mentions(
    text: str,
    candidates: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """@-mentions of ``candidates`` in ``text``, in order of first appearance.

    Case-insensitive, de-duplicated. Tokens match a slug, a unique
    display / first name, or a unique alias prefix. Tokens that match no
    candidate are ignored (so "@Mark" in an agent reply never schedules
    a turn unless "Mark" is a member). Mentions inside fenced code
    blocks are not live.
    """
    index = mention_index(candidates, display_names)
    seen: List[str] = []
    for match in _MENTION_RE.finditer(blank_fences(text or "")):
        member = resolve_mention(match.group(1), index)
        if member is not None and member not in seen:
            seen.append(member)
    return seen


def has_room_broadcast(text: str) -> bool:
    """True when the user addressed ``@room`` (not inside a fence)."""
    for match in _MENTION_RE.finditer(blank_fences(text or "")):
        if match.group(1).lower() == ROOM_BROADCAST_TOKEN:
            return True
    return False


# Composer prefix on a voice take. Mentions live at the start of the
# draft (``@Patty``), so a long prefix is clipped from the end.
_MAX_AUDIO_DRAFT = 4000


def join_draft_and_speech(draft: str, speech: str) -> str:
    """Prefix the composer onto an STT line so @mentions stay live.

    Hold-to-talk is a separate send path from typed Send. Without this,
    tapping ``@Patty`` then speaking posts only the transcript and the
    lead takes the turn. Empty draft is a no-op.
    """
    left = (draft or "").strip()
    if len(left) > _MAX_AUDIO_DRAFT:
        left = left[:_MAX_AUDIO_DRAFT].rstrip()
    right = (speech or "").strip()
    if not left:
        return right
    if not right:
        return left
    return f"{left} {right}"


# Spoken vocative → a real @token. Only the start of the line, and only
# after the composer prefix is already on it. A live @mention (including
# a tap-to-talk draft) wins and is left alone. Unique prefixes shorter
# than this many characters are ignored so "at Ed" does not steal Editor.
_SPOKEN_MIN_PREFIX = 4
_SPOKEN_AT_ROOM = re.compile(r"(?is)^(at|hey|yo)\s*,?\s+room\b[,:]?\s*")
_SPOKEN_AT_NAME = re.compile(
    r"(?is)^(at|hey|yo)\s*,?\s+(" + _TOKEN_RE.pattern + r")[,:]?\s*"
)
_SPOKEN_HI_NAME = re.compile(
    r"(?is)^(hi|hello)\s*,?\s+(" + _TOKEN_RE.pattern + r")\b"
)
_SPOKEN_NAME_COMMA = re.compile(
    r"(?is)^(" + _TOKEN_RE.pattern + r")[,:]\s+"
)


def _one_edit_apart(left: str, right: str) -> bool:
    """True when *left* and *right* differ by a single insert/delete/replace."""
    if left == right:
        return False
    a, b = left, right
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    i = 0
    skipped = False
    for ch in b:
        if i < len(a) and a[i] == ch:
            i += 1
            continue
        if skipped:
            return False
        skipped = True
    return True


def resolve_spoken_name(
    token: str, index: Dict[str, str]
) -> Optional[str]:
    """Exact roster alias, unique prefix (>=4), or unique one-edit near-miss.

    STT routinely writes ``Mingus`` for Mangus. A single-character
    misspelling is accepted only when it maps to exactly one alias.
    """
    key = (token or "").lower()
    if not key or key == ROOM_BROADCAST_TOKEN:
        return None
    if key in index:
        return index[key]
    if len(key) < _SPOKEN_MIN_PREFIX:
        return None
    prefixed = resolve_mention(token, index)
    if prefixed:
        return prefixed
    hits = {slug for alias, slug in index.items() if _one_edit_apart(key, alias)}
    if len(hits) == 1:
        return hits.pop()
    return None


def rewrite_spoken_address(
    text: str,
    candidates: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> str:
    """Turn a leading spoken vocative into a live ``@Handle``.

    STT writes "at Patty" / "hey Claude" / "Hi, Ellie", never ``@``.
    The turn engine only looks at ``@`` tokens, so without this rewrite
    every hands-free take goes to the lead. The transcript is rewritten
    — there is no hidden voice recipient.

    Patterns (start of line only):

    * ``at|hey|yo NAME`` — replaced with ``@Handle``
      (optional comma after the cue: STT often writes ``Hey, Dave``)
    * ``at|hey|yo room`` — replaced with ``@room``
    * ``hi|hello NAME`` — ``@Handle`` is prepended; the greeting stays
    * ``NAME,`` / ``NAME:`` — replaced with ``@Handle``

    A line that already has a live @mention is unchanged (the v1
    composer prefix wins). Mid-sentence "look at Patty" is unchanged.
    """
    body = (text or "").strip()
    if not body:
        return text or ""
    if parse_mentions(body, candidates, display_names) or has_room_broadcast(body):
        return text
    names = display_names or {}
    index = mention_index(candidates, names)

    match = _SPOKEN_AT_ROOM.match(body)
    if match:
        rest = body[match.end() :]
        return f"@room {rest}".strip() if rest else "@room"

    match = _SPOKEN_AT_NAME.match(body)
    if match:
        slug = resolve_spoken_name(match.group(2), index)
        if slug:
            handle = mention_handle(slug, names.get(slug), candidates, names)
            rest = body[match.end() :]
            return f"@{handle} {rest}".strip() if rest else f"@{handle}"

    match = _SPOKEN_HI_NAME.match(body)
    if match:
        slug = resolve_spoken_name(match.group(2), index)
        if slug:
            handle = mention_handle(slug, names.get(slug), candidates, names)
            return f"@{handle} {body}".strip()

    match = _SPOKEN_NAME_COMMA.match(body)
    if match:
        slug = resolve_spoken_name(match.group(1), index)
        if slug:
            handle = mention_handle(slug, names.get(slug), candidates, names)
            rest = body[match.end() :]
            return f"@{handle} {rest}".strip() if rest else f"@{handle}"

    return text


def plan_user_turns(
    room: Room,
    text: str,
    display_names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Turn queue for a fresh user message: mentioned members in mention
    order, else the room's default responder.

    ``@room`` (user messages only) addresses every current member.
    Explicit ``@Name``s keep their mention order at the front; the rest
    of the roster follows. Agent replies that say ``@room`` do not
    expand — that path still uses ``parse_mentions`` only.
    """
    mentioned = parse_mentions(text, room.members, display_names)
    if has_room_broadcast(text):
        ordered = list(mentioned)
        for member in room.members:
            if member not in ordered:
                ordered.append(member)
        return ordered
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


def did_not_reply_notice(member: str, reason: str) -> str:
    """System line posted when a planned speaker produces no agent message."""
    return f"{member}{DID_NOT_REPLY_INFIX}{reason})"


def turn_started_notice(member: str) -> str:
    """System line posted when a member's turn begins."""
    return f"{member} is on it."


def member_joined_notice(member: str) -> str:
    """System line posted when someone is invited into a live room."""
    return f"{member} joined the room"


def member_left_notice(member: str) -> str:
    """System line posted when someone is removed from a live room."""
    return f"{member} left the room"


def seed_invite_last_seen(room: Room, member: str, head_seq: int) -> None:
    """Set last_seen for a first-time invitee so they see at most WINDOW messages.

    A member who already has a last_seen entry is a re-invite: keep their
    real position. last_seen survives removal so this works. ``head_seq``
    is the highest transcript seq at seed time (typically the join
    notice). The cursor is never negative.
    """
    if member in room.last_seen:
        return
    room.last_seen[member] = max(0, int(head_seq) - INVITE_TRANSCRIPT_WINDOW)


def with_members(room: Room, members: List[str]) -> Room:
    """A copy of *room* whose roster is *members* (one cycle's snapshot).

    Invite/remove updates the stored roster immediately, but turn
    planning for an already-running user-message cycle must keep using
    the members that cycle started with.
    """
    return replace(room, members=list(members))


def cycle_internal_error_notice() -> str:
    return f"{CYCLE_INTERNAL_ERROR_PREFIX} — see gateway log"


def cycle_budget_notice(budget: int, queued: List[str]) -> str:
    still = ", ".join(queued)
    return (
        f"{CYCLE_BUDGET_PREFIX} ({budget}) reached — waiting for the next "
        f"user message. Still queued: {still}"
    )


def cycle_stopped_notice(who: Optional[str] = None) -> str:
    """System line posted when the user stops this room's in-flight cycle."""
    name = (who or "").strip()
    if name:
        return f"{CYCLE_STOPPED_PREFIX} {name} stopped this turn."
    return CYCLE_STOPPED_PREFIX


def is_cycle_abort_notice(text: str) -> bool:
    body = text or ""
    return (
        body.startswith(CYCLE_INTERNAL_ERROR_PREFIX)
        or body.startswith(CYCLE_BUDGET_PREFIX)
        or body.startswith(CYCLE_STOPPED_PREFIX)
    )


def turn_concludes_waiter(msg: RoomMessage, waiter: str) -> bool:
    """True when *msg* should drop *waiter* from the in-room thinking list."""
    if msg.kind == KIND_AGENT and msg.speaker == waiter:
        return True
    if msg.kind != KIND_SYSTEM:
        return False
    text = msg.text or ""
    if is_cycle_abort_notice(text):
        return True
    return text.startswith(f"{waiter}{DID_NOT_REPLY_INFIX}")


def remaining_thinkers(waiting: List[str], fresh: List[RoomMessage]) -> List[str]:
    if not waiting or not fresh:
        return list(waiting)
    if any(m.kind == KIND_SYSTEM and is_cycle_abort_notice(m.text) for m in fresh):
        return []
    return [w for w in waiting if not any(turn_concludes_waiter(m, w) for m in fresh)]


def remaining_thinkers_after(waiting: List[str], messages: List[RoomMessage]) -> List[str]:
    """Only messages after the latest user line can end the current turn."""
    if not waiting:
        return list(waiting)
    last_user = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].kind == KIND_USER:
            last_user = i
            break
    if last_user < 0:
        return list(waiting)
    return remaining_thinkers(waiting, messages[last_user + 1 :])


def format_lines(messages: List[RoomMessage]) -> str:
    """Attributed transcript block for channel_context delivery."""
    lines = []
    for msg in messages:
        label = f"{msg.speaker} (agent)" if msg.kind == KIND_AGENT else msg.speaker
        if msg.kind == KIND_SYSTEM:
            label = "room"
        lines.append(f"[{label}] {msg.text}")
    return "\n".join(lines)


_MEDIA_WORDS = (
    "image",
    "picture",
    "photo",
    "png",
    "jpg",
    "jpeg",
    "graphic",
    "illustration",
    "still",
    "artwork",
)

FALLBACK_MEDIA = "I'm sorry, I cannot find that image at the moment."
FALLBACK_GENERIC = "I'm sorry — I couldn't complete that just now."


_MAKE_WORDS = ("make", "create", "draw", "generate", "paint", "render", "design")
_FIND_WORDS = ("show", "again", "find", "previous", "last", "where is")


def looks_like_media_request(text: str) -> bool:
    """True for a recall/show ask, not a request to make a new picture."""
    blob = (text or "").lower()
    if not any(word in blob for word in _MEDIA_WORDS):
        return False
    if any(word in blob for word in _MAKE_WORDS):
        return False
    return any(word in blob for word in _FIND_WORDS)


def fallback_reply(trigger_text: str) -> str:
    """Spoken line when a turn produces no reply. Never leave the room silent."""
    if looks_like_media_request(trigger_text):
        return FALLBACK_MEDIA
    return FALLBACK_GENERIC


def room_briefing(
    room: Room,
    member: str,
    user_names: List[str],
    display_names: Optional[Dict[str, str]] = None,
    itinerary: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[str]] = None,
    principal_about: Optional[str] = None,
    principal_name: Optional[str] = None,
    other_rooms: Optional[List["Room"]] = None,
) -> str:
    """Per-turn channel prompt: who you are, who is here, how to behave."""
    names = display_names or {}
    me_name = names.get(member) or member
    me_handle = mention_handle(member, me_name, room.members, names)
    others = [m for m in room.members if m != member]
    roster: List[str] = []
    for slug in others:
        handle = mention_handle(slug, names.get(slug), room.members, names)
        extra = f" (`{slug}`)" if handle.lower() != slug.lower() else ""
        roster.append(f"@{handle}{extra}")
    people = ", ".join(user_names) if user_names else "the user"
    parts = [
        f'You are {me_name}, a member of the room "{room.name}".',
        f"In this room you speak as @{me_handle}.",
        f"Humans here: {people}.",
        "The human does not take agent turns. Do not @ them as if they were a retainer.",
        (
            "Other agent members: " + ", ".join(roster) + "."
            if roster
            else "You are the only agent member."
        ),
        "A room is one transcript. Speak only as yourself. Never write "
        "another member's lines.",
        "Messages are prefixed [speaker] so you can tell who said what.",
        (
            "To hand work off, @ that member by the name the user would type "
            "(display / first name; the slug in parentheses still works). "
            "Put the @ in your own prose, not inside a fenced draft or heading. "
            "Example: `@Sheila please make a 16:9 header of the room UI.` "
            "Then stop — they take the next turn."
        ),
        "Only @ someone whose input is needed. Do not @-spam the roster.",
        "Do not prefix your reply with your own name or any [speaker] tag — "
        "the room adds attribution for you.",
        "Keep replies conversational. A handoff is @Name plus one sentence, "
        "then you stop; that is not too short.",
        "Work you make belongs in this room. Write files under /workspace "
        "and include the /workspace/... path in your reply so it appears "
        "on the transcript. Files the human attached with + live at "
        "/workspace/uploads/ — you can open them. If you cannot find a "
        "piece the user asks for, say so in one sentence — never stay "
        "silent and never crash out.",
    ]
    if principal_about:
        who = people.split(",")[0].strip() if people else "the human"
        parts.append(f"About {who}: {principal_about}")
    # The principal is workspace state, not agent state — guidance lives
    # here, not in SOUL.md. Skip the literal default "You": addressing
    # someone as "You" is worse than not addressing them at all.
    # "Occasionally" is the whole difficulty: a rule the model can satisfy
    # by maximising becomes every reply. Name the occasions, and say not
    # every message.
    name = (principal_name or "").strip()
    if name and name != "You":
        parts.append(
            f"The human's name is {name}. Use it the way a colleague would: "
            f"when you greet them, when you hand something back, or when you "
            f"re-engage after time away. Most messages should not use their "
            f"name — do not add it to every reply."
        )
    if artifacts:
        parts.append("Work already in this room: " + ", ".join(artifacts) + ".")
        parts.append("Reuse those paths when the user asks to see them again.")
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
    # A mount nobody is told about is a mount nobody uses. Only mentioned
    # when it is actually configured, and the read-only case says so — an
    # agent that tries to write to a ro mount fails in the terminal instead
    # of in the reply.
    from .ide import SHARED_MOUNT, SHARED_MODE_RW, configured_shared_dir, shared_mode_for

    if configured_shared_dir():
        if shared_mode_for(room) == SHARED_MODE_RW:
            parts.append(
                f"{SHARED_MOUNT} is a folder shared with every room and with "
                "the human on the host. You can read and write it. Keep it "
                "organized — it is a filing cabinet, not a dump. This room's "
                f"files go under {SHARED_MOUNT}/rooms/{room.id}/. The human "
                f"drops things for you in {SHARED_MOUNT}/inbox/. Read "
                f"{SHARED_MOUNT}/README.md before writing. Do not leave loose "
                f"files at {SHARED_MOUNT}/ itself, and do not edit another "
                "room's folder. Name files so a stranger knows what they are. "
                "Throwaway work stays under /workspace."
            )
        else:
            parts.append(
                f"{SHARED_MOUNT} is a read-only folder shared with every room. "
                "Read from it; you cannot write there. Your own work still "
                "goes under /workspace."
            )
    # Membership in another room is a capability, and a capability nobody
    # is told about is one nobody uses. Only the rooms this member actually
    # belongs to are named — the briefing shows exactly what the gate
    # allows, never a room they cannot reach.
    if other_rooms:
        from .crossroom import briefing_rooms_line

        line = briefing_rooms_line(other_rooms)
        if line:
            parts.append(line)
    if itinerary:
        from .itinerary import briefing_lines

        parts.extend(
            briefing_lines(itinerary, is_lead=bool(room.lead and room.lead == member))
        )
    return "\n".join(parts)
