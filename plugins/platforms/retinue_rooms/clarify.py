"""Surface Hermes ``clarify`` prompts on the room transcript.

The gateway already calls ``adapter.send_clarify`` and waits on
``clarify_gateway``. Rooms ``send()`` used to drop that prompt (no
``notify`` marker), so the yes/no never appeared and Speak Replies
never played it. This module formats the prompt, finds the pending
entry for a room, and maps a typed reply onto the existing gateway
resolver.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import engine


def session_keys(room_id: str, member: str) -> List[str]:
    """Session-key shapes rooms actually write (profile and multiplex)."""
    rid = room_id or ""
    slug = member or ""
    return [
        f"agent:{slug}:retinue_rooms:group:{rid}:{slug}",
        f"agent:{slug}:retinue_rooms:group:{rid}",
        f"agent:main:retinue_rooms:group:{rid}:{slug}",
    ]


def member_from_session_key(session_key: str) -> str:
    parts = [p for p in (session_key or "").split(":") if p]
    # agent:<profile>:retinue_rooms:group:<room>[:<member>]
    if len(parts) >= 2 and parts[0] == "agent":
        slug = parts[1]
        if slug and slug != "main":
            return slug
        if len(parts) >= 6 and parts[-1] not in {"group", "retinue_rooms"}:
            return parts[-1]
    return ""


def format_prompt(question: str, choices: Optional[Iterable[str]]) -> str:
    """Numbered list the human can answer with 1 / the option / their own words."""
    q = (question or "").strip() or "I need a decision."
    lines = [f"❓ {q}"]
    opts = [str(c).strip() for c in (choices or []) if str(c).strip()]
    if opts:
        lines.append("")
        for i, choice in enumerate(opts, start=1):
            lines.append(f"  {i}. {choice}")
        lines.append("")
        lines.append("Reply with the number, the option text, or your own answer.")
    return "\n".join(lines)


def pending_for_room(room: engine.Room) -> Optional[Tuple[str, Any]]:
    """Oldest pending clarify for any member of *room*, or None."""
    try:
        from tools.clarify_gateway import get_pending_for_session
    except Exception:
        return None
    for slug in list(room.members or []):
        for key in session_keys(room.id, slug):
            try:
                entry = get_pending_for_session(key, include_choice_prompts=True)
            except Exception:
                entry = None
            if entry is not None:
                return slug, entry
    return None


def try_resolve(room: engine.Room, text: str) -> bool:
    """True when *text* answered a pending clarify for this room."""
    try:
        from tools.clarify_gateway import (
            TEXT_RESOLVED,
            attempt_text_response_for_session,
        )
    except Exception:
        return False
    seen = set()
    for slug in list(room.members or []):
        for key in session_keys(room.id, slug):
            if key in seen:
                continue
            seen.add(key)
            try:
                outcome = attempt_text_response_for_session(key, text)
            except Exception:
                continue
            if outcome == TEXT_RESOLVED:
                return True
    return False


def release_room(room: engine.Room) -> None:
    """Unblock any clarify still waiting in this room (timeout / Stop)."""
    try:
        from tools.clarify_gateway import clear_session
    except Exception:
        return
    seen = set()
    for slug in list(room.members or []):
        for key in session_keys(room.id, slug):
            if key in seen:
                continue
            seen.add(key)
            try:
                clear_session(key)
            except Exception:
                continue


def spoken_from_clarify(entry: Any) -> str:
    question = str(getattr(entry, "question", "") or "").strip()
    if question:
        return f"I was waiting on a yes/no this room must show: {question}"
    return "I was waiting on a yes/no this room must show, and nobody answered."


def spoken_from_tool(last_tool: Optional[Dict[str, Any]]) -> Optional[str]:
    """Name a permission or missing-path stop from the last tool result."""
    if not last_tool:
        return None
    name = str(last_tool.get("name") or "").strip().lower()
    output = str(last_tool.get("output") or "")
    args = last_tool.get("arguments") or {}
    if name == "clarify":
        q = ""
        if isinstance(args, dict):
            q = str(args.get("question") or "").strip()
        if q:
            return f"I was waiting on a yes/no this room must show: {q}"
        return "I was waiting on a yes/no this room must show, and nobody answered."
    low = output.lower()
    snippet = _first_line(output)
    if any(tok in low for tok in ("permission denied", "eacces", "operation not permitted")):
        return f"I stopped — missing permission. {snippet}".strip()
    if any(
        tok in low
        for tok in (
            "no such file",
            "not found",
            "cannot find",
            "no issue found",
            "no issues found",
        )
    ):
        return f"I stopped — I could not find a file or path. {snippet}".strip()
    return None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        body = line.strip()
        if body:
            return body[:240]
    return ""
