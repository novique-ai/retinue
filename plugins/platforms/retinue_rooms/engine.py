"""Room domain logic — pure and gateway-independent.

Everything in this module is deliberately free of gateway imports so the
turn-taking rules can be unit-tested without a running Hermes instance
(see test_engine.py).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Optional

from . import worktrees

KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SYSTEM = "system"
# Tool-activity lines from a runtime that streams its tool lifecycle
# (Grok Build, #218). Presentation-only observability: excluded from
# member deltas, TTS, and turn accounting; the web UI renders them as
# muted activity rows.
KIND_TOOL = "tool"

# System-notice prefixes the web UI uses to drop a thinking bubble.
# Keep in lockstep with retinue-web/src/thinking.ts.
CYCLE_INTERNAL_ERROR_PREFIX = "internal error running the turn cycle"
CYCLE_BUDGET_PREFIX = "turn budget"
CYCLE_STOPPED_PREFIX = "Stopped."
CYCLE_ROUND_BUDGET_PREFIX = "⚠️ round budget reached"
DID_NOT_REPLY_INFIX = " did not reply ("

# Why a planned/mentioned speaker was dropped. Adapter posts one notice
# per exhaustion event; the engine only reports.
REASON_AGENT_TURNS = "agent_turns"
REASON_FOLLOWUP_ROUNDS = "followup_rounds"

DEFAULT_MAX_AGENT_TURNS = 8
# Room-wide speak-or-pass laps are a deliberate mode, not the resting
# state of a room: a lap costs every member a turn out of the same
# budget the addressed member needs. Opt in per room (#160).
DEFAULT_MAX_FOLLOWUP_ROUNDS = 0

# Structured pass contract at the engine boundary. The whole reply must
# JSON-decode to this object — not a substring, not ``(pass)`` in prose.
# The adapter/briefing owns instructing members; this module owns matching.
TURN_SPEAK = "speak"
TURN_PASS = "pass"
TURN_FAIL = "fail"
PASS_PAYLOAD: Dict[str, bool] = {"pass": True}

# On invite, seed last_seen so the newcomer receives only the last N
# messages rather than the whole transcript. This is not a compromise
# and is not laziness: room_briefing already includes the itinerary
# when the lead keeps one, and the itinerary is already a lead-authored
# outline of the room's purpose. "The itinerary plus the last N turns"
# — the briefed join — falls out of seeding last_seen. No summarisation
# call, no new prompt path.
INVITE_TRANSCRIPT_WINDOW = 20

# Injected per-turn delta is capped at this many messages. The member's
# own session already has long-run context; dumping an unbounded unread
# backlog after a long idle is cost without new information. Same size
# as invite seeding so a first turn and a long-idle turn see one shape.
DELTA_TRANSCRIPT_WINDOW = INVITE_TRANSCRIPT_WINDOW

# @name — profile names may contain letters, digits, underscores, hyphens.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_MENTION_RE = re.compile(r"@(" + _TOKEN_RE.pattern + r")")

# User-only broadcast. Reserved — do not hire a retainer whose slug is ``room``.
ROOM_BROADCAST_TOKEN = "room"

# Principal escalation. Generic forms always count; the display name is
# added when it is a mention token. Reserved — do not hire a retainer
# whose slug is ``user`` or ``you``.
PRINCIPAL_GENERIC = ("user", "you")


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
    # Bounded speak-or-pass laps after the first planned wave. 0 disables.
    max_followup_rounds: int = DEFAULT_MAX_FOLLOWUP_ROUNDS
    created_at: float = field(default_factory=time.time)
    # member -> highest transcript seq already delivered to that member
    last_seen: Dict[str, int] = field(default_factory=dict)
    # Hidden from the sidebar without wiping the transcript.
    archived: bool = False
    # sandbox (default): isolated container, no host mount.
    # ide: same container runtime, bind-mount of ide_path at /workspace.
    workspace: str = "sandbox"
    ide_path: Optional[str] = None
    # Repo paths (relative to ide_path) this room isolates in its own git
    # worktree on retinue/room/<id>, bind-mounted over their place in
    # /workspace. Empty = share the tree with every other room, as before
    # (novique-ai/retinue#169).
    worktree_repos: List[str] = field(default_factory=list)
    # /shared mount: "rw" (default) or "ro". Absent treated as "rw";
    # unknown values on disk stay read-only.
    shared_mode: Optional[str] = None
    # Projects group rooms (see projects.py). None = Unfiled. This is the
    # only membership record — do not also track room ids on the project.
    project_id: Optional[str] = None
    # A member @mentioned the principal; cleared when the principal posts.
    needs_user: bool = False

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
            max_followup_rounds=_coerce_followup_rounds(data.get("max_followup_rounds")),
            created_at=float(data.get("created_at") or 0.0),
            last_seen={str(k): int(v) for k, v in (data.get("last_seen") or {}).items()},
            archived=bool(data.get("archived")),
            workspace=str(data.get("workspace") or "sandbox"),
            ide_path=(str(data["ide_path"]) if data.get("ide_path") else None),
            worktree_repos=[str(r) for r in (data.get("worktree_repos") or [])],
            shared_mode=(str(data["shared_mode"]) if data.get("shared_mode") else None),
            project_id=(str(data["project_id"]) if data.get("project_id") else None),
            needs_user=bool(data.get("needs_user")),
        )


def _coerce_followup_rounds(value: Any) -> int:
    """0 is a valid disable; missing/garbage fall back to the default."""
    if value is None or value == "":
        return DEFAULT_MAX_FOLLOWUP_ROUNDS
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FOLLOWUP_ROUNDS


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


def is_directed(
    text: str,
    members: List[str],
    display_names: Optional[Dict[str, str]] = None,
) -> bool:
    """True when the user named who should answer.

    A directed message is answered by the members it names -- convening
    the rest of the room behind it is a poll the user did not ask for
    (#160). ``@room`` counts as directed: it already addresses every
    member in the first wave, so a lap after it is a second poll.
    Mentions that match no member, or that sit inside a fence, do not
    direct anything.
    """
    if has_room_broadcast(text):
        return True
    return bool(parse_mentions(text, members, display_names))


def has_room_broadcast(text: str) -> bool:
    """True when the user addressed ``@room`` (not inside a fence)."""
    for match in _MENTION_RE.finditer(blank_fences(text or "")):
        if match.group(1).lower() == ROOM_BROADCAST_TOKEN:
            return True
    return False


def principal_aliases(display_name: Optional[str] = None) -> List[str]:
    """Generic ``@user`` / ``@you`` plus mentionable tokens of the display name."""
    aliases = list(PRINCIPAL_GENERIC)
    seen = {a.lower() for a in aliases}
    name = (display_name or "").strip()
    if not name:
        return aliases
    candidates: List[str] = []
    if _TOKEN_RE.fullmatch(name):
        candidates.append(name)
    first = name.split()[0]
    if first and _TOKEN_RE.fullmatch(first):
        candidates.append(first)
    for token in candidates:
        key = token.lower()
        if key not in seen:
            aliases.append(token)
            seen.add(key)
    return aliases


def principal_mention_handle(display_name: Optional[str] = None) -> Optional[str]:
    """Display-name token to advertise next to ``@user``, or None."""
    named = [
        alias
        for alias in principal_aliases(display_name)
        if alias.lower() not in PRINCIPAL_GENERIC
    ]
    return named[0] if named else None


def mentions_principal(
    text: str,
    display_name: Optional[str] = None,
    members: Optional[List[str]] = None,
    display_names: Optional[Dict[str, str]] = None,
) -> bool:
    """True when a live @mention addresses the principal, not a retainer.

    Generic ``@user`` / ``@you`` always count. The principal's display name
    and unique first name count unless that alias already belongs to a
    member — the retainer wins so ``@Clayton`` still hands off.
    Mentions inside fenced code are not live.
    """
    generics = {token.lower() for token in PRINCIPAL_GENERIC}
    named = {alias.lower() for alias in principal_aliases(display_name)} - generics
    if members:
        index = mention_index(list(members), display_names)
        named = {alias for alias in named if alias not in index}
    for match in _MENTION_RE.finditer(blank_fences(text or "")):
        token = match.group(1).lower()
        if token in generics or token in named:
            return True
    return False


def apply_needs_user(
    room: Room,
    message: RoomMessage,
    principal_name: str = "",
    member_names: Optional[Dict[str, str]] = None,
) -> bool:
    """Set or clear ``room.needs_user`` for a newly posted message.

    An agent line that @mentions the principal sets the flag. The
    principal's next post clears it. System notices do neither. Returns
    whether the flag changed.
    """
    before = bool(room.needs_user)
    if message.kind == KIND_USER:
        room.needs_user = False
    elif message.kind == KIND_AGENT and mentions_principal(
        message.text,
        principal_name,
        members=room.members,
        display_names=member_names,
    ):
        room.needs_user = True
    return bool(room.needs_user) != before


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


def pass_payload_text() -> str:
    """Canonical JSON a member emits to pass. Adapter briefing quotes this."""
    return json.dumps(PASS_PAYLOAD)


_PASS_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\n(.*?)\n?```$", re.DOTALL)


def is_pass_reply(text: str) -> bool:
    """True iff *text* is exactly the structured pass payload.

    The whole reply must JSON-decode to ``{"pass": true}``. Extra keys,
    surrounding prose, and ``(pass)`` in a sentence are spoken replies.
    One surrounding markdown code fence is tolerated — models wrap JSON
    in fences despite instructions, and a fenced payload is still exactly
    the payload.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    fenced = _PASS_FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
        if not raw:
            return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return data == PASS_PAYLOAD


def classify_turn(ok: bool, text: str) -> str:
    """Map a model result onto speak / pass / fail.

    Fail is *ok* being false (timeout, dispatch error, empty-delta no-op
    returned as an error). Pass is recognized only by ``is_pass_reply``.
    Everything else — including an empty successful reply — is speak;
    the adapter may still substitute the fallback line.
    """
    if not ok:
        return TURN_FAIL
    if is_pass_reply(text):
        return TURN_PASS
    return TURN_SPEAK


def plan_followup_round(
    members: List[str],
    skip: List[str],
    budget_left: int,
) -> List[str]:
    """Members offered a speak-or-pass turn this follow-up round.

    *skip* is who already spoke in the immediately previous round, or —
    for the first follow-up — who already attempted the planned wave.
    Roster order, truncated to *budget_left*. Empty means nobody is left
    to ask and the caller treats the room as settled.
    """
    if budget_left <= 0:
        return []
    blocked = set(skip)
    return [m for m in members if m not in blocked][:budget_left]


def followup_round_settled(spoke: List[str]) -> bool:
    """True when a follow-up round added no member speech."""
    return not spoke


@dataclass(frozen=True)
class DroppedPending:
    """Speakers the engine declined because a budget ran out (#189)."""

    speakers: tuple[str, ...]
    reason: str
    used: int


def pending_mentioned(
    room: Room,
    replies: List[tuple[str, str]],
    attempted: List[str],
    display_names: Optional[Dict[str, str]] = None,
    exclude: Optional[List[str]] = None,
) -> List[str]:
    """Members @mentioned in *replies* who did not take a later turn.

    Each reply is aligned with the next unused occurrence of its speaker
    in *attempted* so a member who speaks twice is matched to the right
    mention. *exclude* is leftover queue already named by
    ``cycle_budget_notice`` — those speakers are not double-reported.
    Self-mentions do not count.
    """
    blocked = set(exclude or [])
    pending: List[str] = []
    seen: set[str] = set()
    used = 0
    for speaker, text in replies:
        try:
            rel = attempted.index(speaker, used)
        except ValueError:
            rel = max(used - 1, 0) if attempted else -1
        used = rel + 1
        later = set(attempted[rel + 1 :]) if rel >= 0 else set()
        for member in parse_mentions(text or "", room.members, display_names):
            if (
                member == speaker
                or member in later
                or member in blocked
                or member in seen
            ):
                continue
            pending.append(member)
            seen.add(member)
    return pending


def dropped_pending(
    speakers: List[str],
    *,
    reason: str,
    used: int,
) -> Optional[DroppedPending]:
    """None when nothing was dropped — the adapter must not post a notice."""
    ordered = tuple(dict.fromkeys(s for s in speakers if s))
    if not ordered:
        return None
    return DroppedPending(speakers=ordered, reason=reason, used=int(used))


def dropped_pending_notice(
    drop: Optional[DroppedPending],
    display_names: Optional[Dict[str, str]] = None,
    members: Optional[List[str]] = None,
) -> Optional[str]:
    """Rate-limited system line for a budget drop. None if *drop* is empty."""
    if drop is None or not drop.speakers:
        return None
    names = display_names or {}
    candidates = list(members or drop.speakers)
    handles = [
        f"@{mention_handle(slug, names.get(slug), candidates, names)}"
        for slug in drop.speakers
    ]
    who = ", ".join(handles)
    if drop.reason == REASON_FOLLOWUP_ROUNDS:
        unit = "followup round" if drop.used == 1 else "followup rounds"
        used_clause = f"{drop.used} {unit} used"
    else:
        unit = "agent turn" if drop.used == 1 else "agent turns"
        used_clause = f"{drop.used} {unit} used"
    return (
        f"{CYCLE_ROUND_BUDGET_PREFIX} — {who} did not get a turn "
        f"({used_clause}). A new user message grants fresh turns."
    )


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


PROVIDER_EVENT_PREFIX = "⚠️"
_PROVIDER_DETAIL_CAP = 200


def provider_event_notice(member_display: str, detail: str) -> str:
    """System line for a mid-turn provider stall/retry (#166).

    Compact by contract: *detail* is a one-line summary from the retry loop
    (error class, attempt counter, model), never a payload — and it is
    capped here so a provider's error prose cannot flood the transcript.
    """
    body = " ".join((detail or "").split())
    if len(body) > _PROVIDER_DETAIL_CAP:
        body = body[: _PROVIDER_DETAIL_CAP - 1] + "…"
    who = (member_display or "").strip() or "the retainer"
    return f"{PROVIDER_EVENT_PREFIX} {who}'s model provider hiccuped — {body} Still working."


def is_cycle_abort_notice(text: str) -> bool:
    body = text or ""
    return (
        body.startswith(CYCLE_INTERNAL_ERROR_PREFIX)
        or body.startswith(CYCLE_BUDGET_PREFIX)
        or body.startswith(CYCLE_STOPPED_PREFIX)
        or body.startswith(CYCLE_ROUND_BUDGET_PREFIX)
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


def omitted_delta_notice(count: int) -> str:
    """Compact one-liner when older unread messages were not injected."""
    return f"{int(count)} earlier messages omitted"


def cap_delta(
    messages: List[RoomMessage],
    cap: int = DELTA_TRANSCRIPT_WINDOW,
) -> tuple[List[RoomMessage], int]:
    """Keep the newest *cap* messages; return ``(kept, omitted_count)``."""
    if cap <= 0:
        return list(messages), 0
    blob = list(messages)
    extra = len(blob) - cap
    if extra <= 0:
        return blob, 0
    return blob[-cap:], extra


def format_delta_context(
    prior: List[RoomMessage],
    omitted: int = 0,
) -> Optional[str]:
    """``channel_context`` body: optional elision notice + attributed priors.

    The notice uses the same ``[room] …`` shape as ``format_lines`` so it
    sits in the injected block as one more system line.
    """
    body = format_lines(prior) if prior else ""
    if omitted > 0:
        notice = f"[room] {omitted_delta_notice(omitted)}"
        return f"{notice}\n{body}" if body else notice
    return body or None


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

# Spoken when a planned turn fails. Distinct from FALLBACK_GENERIC so a
# timeout is not mistaken for an empty successful answer (#133). Distinct
# from a system-only notice so Speak Replies can play it and the human
# hears the retainer instead of silence.
TIMEOUT_REPLY = (
    "I ran out of time and never posted. Usually that means I was waiting "
    "on a yes/no, I lacked permission, or I could not find a file or path."
)
DISPATCH_REPLY = "I couldn't start that turn. I lacked a way to begin it."


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


def failed_turn_reply(
    reason: str,
    *,
    last_tool: Optional[Dict[str, Any]] = None,
    clarify: Any = None,
) -> str:
    """Spoken line when a planned turn times out or fails to dispatch.

    Prefer the actual blocker (a hidden yes/no, missing permission, missing
    path) over a generic "ask me to continue". Distinct from
    ``FALLBACK_GENERIC`` so a timeout is not an empty successful answer.
    """
    from . import clarify as room_clarify

    if clarify is not None:
        return room_clarify.spoken_from_clarify(clarify)
    named = room_clarify.spoken_from_tool(last_tool)
    if named:
        return named
    blob = (reason or "").strip().lower()
    if "no reply within" in blob or "timed out" in blob:
        return TIMEOUT_REPLY
    return DISPATCH_REPLY


# A planned turn can end without an agent message for reasons that are not
# the retainer failing at anything. Rendering all of them as the apology
# above tells the human their retainer broke when it did not: the queue
# simply had nothing for it to read, or a newer turn replaced it.
NO_OP_TURN_REASONS = frozenset(
    {
        "nothing new to respond to",
        "superseded by a newer turn",
    }
)


def turn_is_no_op(reason: str) -> bool:
    """True when a turn ended for a scheduling reason, not a real attempt."""
    return (reason or "") in NO_OP_TURN_REASONS


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
    governed_contract: Optional[str] = None,
    jobs: Optional[Dict[str, str]] = None,
    host_workspace: Optional[str] = None,
    host_uploads: Optional[str] = None,
    host_worktrees: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Per-turn channel prompt: who you are, who is here, how to behave.

    ``governed_contract`` is the operator's binding operating contract for
    governed retainers (governed.py) — appended last so it reads as the
    final word of the briefing. The adapter only passes it for governed
    members in ide rooms, and fails the turn closed rather than passing
    None for a governed member whose contract is unreadable.

    ``jobs`` (slug → one-line job title) annotates each roster entry so a
    member knows what a teammate is *for*, not just their handle (#203).
    The when/how of delegation stays in each hire's SOUL; this is only the
    directory line.

    ``host_worktrees`` (#223) lists the room's isolated repos for a
    host-native member as ``{"rel", "real", "path", "branch"}`` dicts:
    the member's own checkout of ``rel`` is at ``path`` on ``branch``;
    the tree at ``real`` is shadowed and must not be touched (the
    permission gate also enforces this — the briefing is so the member
    works in the right place instead of bouncing off rejections).

    ``host_workspace`` marks a host-native runtime (Grok Build, #218):
    the member's tools run directly on the host in that directory, not in
    the room container, so every /workspace-shaped instruction would name
    a path that does not exist for it. The workspace, skills, and shared
    sections switch to host paths; ``host_uploads`` is the host directory
    of the room's attachments.
    """
    names = display_names or {}
    member_jobs = jobs or {}
    me_name = names.get(member) or member
    me_handle = mention_handle(member, me_name, room.members, names)
    others = [m for m in room.members if m != member]
    roster: List[str] = []
    for slug in others:
        handle = mention_handle(slug, names.get(slug), room.members, names)
        extra = f" (`{slug}`)" if handle.lower() != slug.lower() else ""
        job = str(member_jobs.get(slug) or "").strip()
        title = f" — {job}" if job else ""
        roster.append(f"@{handle}{extra}{title}")
    people = ", ".join(user_names) if user_names else "the user"
    you_handle = principal_mention_handle(principal_name)
    escalate = "Escalate a real judgment call with @user"
    if you_handle:
        escalate += f" (or @{you_handle})"
    escalate += "; that flags the room as needing them."
    parts = [
        f'You are {me_name}, a member of the room "{room.name}".',
        f"In this room you speak as @{me_handle}.",
        f"Humans here: {people}.",
        (
            "The human does not take agent turns. Do not @ them as if they "
            f"were a retainer. {escalate}"
        ),
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
        (
            "If this turn adds nothing, pass instead of filling the transcript. "
            "Pass by replying with only this JSON object and no other text: "
            f"{pass_payload_text()}. A pass is silent — the room will not post "
            "a message for you. A sentence that merely says you pass is a "
            "spoken reply, not a pass."
        ),
        (
            f"Work you make belongs in this room. Your tools run directly in "
            f"{host_workspace} — write files there and include the full path "
            f"in your reply so it appears on the transcript. Files the human "
            f"attached with + live at {host_uploads or '(no uploads yet)'} — "
            f"you can open them. If you cannot find a piece the user asks "
            f"for, say so in one sentence — never stay silent and never "
            f"crash out."
            if host_workspace
            else "Work you make belongs in this room. Write files under /workspace "
            "and include the /workspace/... path in your reply so it appears "
            "on the transcript. Files the human attached with + live at "
            "/workspace/uploads/ — you can open them. If you cannot find a "
            "piece the user asks for, say so in one sentence — never stay "
            "silent and never crash out."
        ),
        "If you cannot finish the work — missing permission, a file or "
        "path you cannot find, a command that failed — say so in this "
        "room and stop. Never keep calling tools until the turn times "
        "out. A stuck turn with no message is worse than an incomplete "
        "one. If you need a yes/no, ask it in this room (the clarify "
        "tool posts here); do not wait on a prompt the human cannot see.",
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
    if host_workspace:
        # Host-native runtime: the container narrative below would name
        # mounts this member does not have. One section says what its
        # world actually is.
        if (room.workspace or "sandbox") == "ide":
            parts.append(
                f"Your working directory is {host_workspace} — the real "
                f"project tree on this machine, not a sandbox or a mount. "
                f"Documented paths in AGENTS.md / runbooks are written "
                f"relative to that tree. Treat every edit as an edit to the "
                f"real project."
            )
            for wt in host_worktrees or []:
                parts.append(
                    f"EXCEPTION — {wt['rel']} is isolated for this room. "
                    f"Do NOT read or write {wt['real']}: that checkout "
                    f"belongs to the host and other rooms, and tool calls "
                    f"touching it will be declined. Your OWN checkout of "
                    f"{wt['rel']} is {wt['path']}, already on branch "
                    f"{wt['branch']} — do all {wt['rel']} work there "
                    f"(git works normally in it; commit to that branch, "
                    f"never switch it). The human merges the branch on the "
                    f"host when the work is verified."
                )
        else:
            parts.append(
                f"This room is sandboxed. Your working directory is "
                f"{host_workspace}, a scratch area reserved for this room. "
                f"Keep your work inside it."
            )
    elif (room.workspace or "sandbox") == "ide":
        parts.append(
            "This room is attached to this machine's IDE. Your terminal "
            "/workspace is a bind-mount of that host tree — treat it as the "
            "real project, not a throwaway sandbox."
        )
        # What /workspace IS depends on how the room was scoped, and saying
        # the wrong one costs the retainer the whole turn: told "/workspace is
        # the ENTIRE IDE" while mounted on a single repo, it reached for
        # /workspace/infra/..., found nothing, and reported the repo's own
        # documented paths as wrong (infra-90xc).
        from .ide import configured_ide_root, mounts_ide_root, resolve_ide_path

        try:
            scoped = not mounts_ide_root(room)
            host = resolve_ide_path(room.ide_path) if scoped else ""
        except ValueError:
            # A room record whose path no longer resolves already fails at
            # container start with a real message. The briefing is not the
            # place to invent a second failure, so it keeps the old wording.
            scoped, host = False, ""
        if not scoped:
            parts.append(
                "/workspace is the ENTIRE IDE — every repo and data tree on "
                "this machine — not one project. Work inside the specific repo "
                "your task names (e.g. /workspace/infra/) and search there. "
                "Recursive searches rooted at /workspace itself are refused: "
                "they take minutes and flood your context with output."
            )
        else:
            root = configured_ide_root()
            rel = os.path.relpath(host, root) if root else host
            parts.append(
                f"/workspace is ONE subtree of this machine's IDE: the host "
                f"path {host}. The rest of the IDE is NOT mounted — no "
                f"/workspace/infra, no sibling projects. If the work needs "
                f"something outside this subtree, say so in the room; do not "
                f"invent a path."
            )
            parts.append(
                f"Paths in AGENTS.md, runbooks and issue trackers are written "
                f"relative to the IDE root, so they read as {rel}/... — the "
                f"same file is /workspace/... for you. A documented path that "
                f"starts with {rel}/ is not wrong; it is the host's view of "
                f"your mount."
            )
        isolated = [str(r) for r in (getattr(room, "worktree_repos", None) or [])]
        if isolated:
            paths = ", ".join(f"/workspace/{r}" for r in isolated)
            parts.append(
                f"{paths} is your OWN git worktree, checked out on branch "
                f"{worktrees.branch_for(room.id)} — no other room can see or disturb "
                f"your edits there. Commit to that branch as normal; you "
                f"cannot and should not switch it to main. The human merges "
                f"it on the host when the work is verified. Everything else "
                f"under /workspace is still the shared tree."
            )
    else:
        parts.append(
            "This room is sandboxed. Your terminal /workspace is an isolated "
            "container with no host IDE mount."
        )
    # Your skills, at YOUR path (#188, #192). The room's container no
    # longer mounts /root/.hermes/skills at all — that was the creating
    # profile's tree, readable by every member. Only said when THIS
    # member's mount actually exists: naming a path the container does
    # not have is worse than saying nothing.
    from .ide import MEMBER_SKILLS_ENV, member_skills_host_dir, member_skills_mount_for

    if host_workspace:
        my_skills_host = member_skills_host_dir(member)
        if my_skills_host:
            parts.append(
                f"Your own skills live at {my_skills_host} on this machine "
                f"(read them; edit only your workspace). Run skill scripts "
                f"from there, e.g. "
                f"`python3 {my_skills_host}/<skill>/scripts/<script>.py`."
            )
    else:
        my_skills = member_skills_mount_for(member)
        if my_skills:
            parts.append(
                f"Your own skills are mounted read-only in your terminal at "
                f"{my_skills} (also ${MEMBER_SKILLS_ENV}). Run your skill scripts "
                f"and read their skill-local .env from there, e.g. "
                f"`python3 {my_skills}/<skill>/scripts/<script>.py`. "
                f"/root/.hermes/skills is not mounted in this room — if a "
                f"SKILL.md names that path, use yours above instead."
            )
    # A mount nobody is told about is a mount nobody uses. Only mentioned
    # when it is actually configured, and the read-only case says so — an
    # agent that tries to write to a ro mount fails in the terminal instead
    # of in the reply.
    from .ide import SHARED_MOUNT, SHARED_MODE_RW, configured_shared_dir, shared_mode_for

    if configured_shared_dir() and host_workspace:
        shared_host = configured_shared_dir()
        if shared_mode_for(room) == SHARED_MODE_RW:
            parts.append(
                f"{shared_host} is a folder shared with every room and with "
                f"the human. You can read and write it; this room's files go "
                f"under {shared_host}/rooms/{room.id}/. Read "
                f"{shared_host}/README.md before writing."
            )
        else:
            parts.append(
                f"{shared_host} is a read-only folder shared with every "
                f"room. Read from it; you cannot write there."
            )
    elif configured_shared_dir():
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
    if governed_contract:
        parts.append(
            "\n## OPERATING CONTRACT (binding)\n"
            "You are a governed agent of this ecosystem. The rules below are "
            "not suggestions; when they conflict with anything above except a "
            "direct human instruction in this room, the contract wins.\n\n"
            + governed_contract.strip()
        )
    return "\n".join(parts)
