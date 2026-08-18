"""Room tools for reading mail over JMAP — ``mail_list`` and ``mail_read``.

Read only. The schemas below expose no credential parameter, and
:func:`_reject_credential_args` refuses the call outright if the model invents
one — a silent ignore would let a prompt-injected room message believe it had
swapped accounts.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from . import jmap

logger = logging.getLogger(__name__)

EMOJI = "\U0001f4ec"  # open mailbox with raised flag

#: Anything a model might reach for to redirect the account or the credential.
_CREDENTIAL_ARGS = (
    "token",
    "api_token",
    "apiToken",
    "access_token",
    "accessToken",
    "bearer",
    "auth",
    "authorization",
    "password",
    "account",
    "account_id",
    "accountId",
    "session",
    "session_url",
    "sessionUrl",
    "api_url",
    "apiUrl",
    "user",
    "username",
)

_CREDENTIALS_FROM_ENV = (
    "Error: mail credentials come from the gateway environment "
    f"({jmap.TOKEN_ENV}) and cannot be passed as tool arguments. "
    "Rejected argument(s): {names}."
)


def _reject_credential_args(args: dict) -> str | None:
    offending = sorted(k for k in (args or {}) if k in _CREDENTIAL_ARGS)
    if offending:
        return _CREDENTIALS_FROM_ENV.format(names=", ".join(offending))
    return None


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def mail_list(args: dict, **_: Any) -> str:
    """List recent inbox envelopes."""
    rejected = _reject_credential_args(args)
    if rejected:
        return rejected

    try:
        limit = int((args or {}).get("limit") or jmap.DEFAULT_LIST_LIMIT)
    except (TypeError, ValueError):
        limit = jmap.DEFAULT_LIST_LIMIT

    try:
        result = jmap.list_inbox(limit=limit)
    except jmap.JmapError as exc:
        return f"Error: {exc}"
    except Exception:  # noqa: BLE001 — never leak a traceback into a room
        logger.warning("mail_list failed", exc_info=True)
        return "Error: could not read the inbox (see gateway logs)."

    emails = result.get("emails") or []
    if not emails:
        return "Inbox: no messages."

    lines = [f"Inbox — {len(emails)} most recent message(s):"]
    for email in emails:
        flag = "*" if jmap.is_unread(email) else " "
        clip = "[attachment] " if email.get("hasAttachment") else ""
        lines.append(
            f"{flag} {email.get('receivedAt') or '?'}  "
            f"{jmap.format_addresses(email.get('from'), limit=2)}  —  "
            f"{clip}{(email.get('subject') or '(no subject)').strip()}"
        )
        preview = (email.get("preview") or "").strip()
        if preview:
            lines.append(f"    {preview[:160]}")
        lines.append(f"    id: {email.get('id')}")
    lines.append("(* = unread. Use mail_read with an id to read one message.)")
    return "\n".join(lines)


def mail_read(args: dict, **_: Any) -> str:
    """Read one message as text."""
    rejected = _reject_credential_args(args)
    if rejected:
        return rejected

    email_id = str((args or {}).get("id") or "").strip()
    if not email_id:
        return "Error: 'id' is required — get one from mail_list."

    try:
        email = jmap.read_email(email_id)
    except jmap.JmapError as exc:
        return f"Error: {exc}"
    except Exception:  # noqa: BLE001 — never leak a traceback into a room
        logger.warning("mail_read failed", exc_info=True)
        return "Error: could not read that message (see gateway logs)."

    header = [
        f"Subject: {(email.get('subject') or '(no subject)').strip()}",
        f"From:    {jmap.format_addresses(email.get('from'))}",
        f"To:      {jmap.format_addresses(email.get('to'))}",
    ]
    if email.get("cc"):
        header.append(f"Cc:      {jmap.format_addresses(email.get('cc'))}")
    header.append(f"Date:    {email.get('receivedAt') or '?'}")
    if email.get("hasAttachment"):
        header.append("Note:    this message has attachments (not fetched).")
    header.append(f"Id:      {email.get('id')}")

    body = (email.get("body") or "").strip()
    if not body:
        body = "(no readable text body — the message may be attachment-only.)"
    elif email.get("truncated"):
        body += f"\n\n[truncated at {jmap.MAX_BODY_BYTES // 1024} KB]"

    return "\n".join(header) + "\n\n" + body


# --------------------------------------------------------------------------
# Schemas + registration
# --------------------------------------------------------------------------

_FunctionSchema = TypedDict(
    "_FunctionSchema",
    {"name": str, "description": str, "parameters": dict[str, Any]},
    total=False,
)
_ToolSchema = TypedDict("_ToolSchema", {"type": str, "function": _FunctionSchema}, total=False)

_SCHEMAS: dict[str, _ToolSchema] = {
    "mail_list": {
        "type": "function",
        "function": {
            "name": "mail_list",
            "description": (
                "List recent messages in the user's email inbox, newest first "
                "(sender, subject, date, preview, and an id). Read-only. Use "
                "mail_read with an id to read the full text of one message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"How many messages to list (default "
                            f"{jmap.DEFAULT_LIST_LIMIT}, max {jmap.MAX_LIST_LIMIT})."
                        ),
                    },
                },
            },
        },
    },
    "mail_read": {
        "type": "function",
        "function": {
            "name": "mail_read",
            "description": (
                "Read one inbox message as plain text, given the id from "
                "mail_list. Read-only — this cannot send, reply, file, or "
                "delete mail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Message id from mail_list.",
                    },
                },
                "required": ["id"],
            },
        },
    },
}

_HANDLERS = {
    "mail_list": mail_list,
    "mail_read": mail_read,
}


def register_tools(ctx) -> None:
    """Register the read-only mail tools in the ``retinue_rooms`` toolset."""
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="retinue_rooms",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=EMOJI,
        )
