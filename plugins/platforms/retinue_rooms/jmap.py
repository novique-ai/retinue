"""Fastmail JMAP inbox read — list envelopes, read one message. Read only.

Deliberately narrow: ``Email/query`` + ``Email/get`` against the inbox mailbox
and nothing else. There is no send, draft, move, flag, or mailbox-create path
here, and adding one is a scope change, not a patch.

Credentials come from the environment only:

  ``RETINUE_FASTMAIL_TOKEN``     bearer token (required — no token, no network)
  ``RETINUE_FASTMAIL_SESSION``   session endpoint (optional, defaults below)

A tool argument may never supply a token or an account override; the model is
untrusted input in a room, and a credential it can name is a credential it can
be talked into changing. See ``jmap_tools`` for the argument guard.

Transport is stdlib ``urllib`` — the rooms plugin is stdlib-only.
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOKEN_ENV = "RETINUE_FASTMAIL_TOKEN"
SESSION_ENV = "RETINUE_FASTMAIL_SESSION"
DEFAULT_SESSION_URL = "https://api.fastmail.com/jmap/session"

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"

DEFAULT_TIMEOUT = 30
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
MAX_BODY_BYTES = 256 * 1024

#: Envelope-only properties. ``bodyValues`` is deliberately absent — a list of
#: 20 messages should not drag 20 bodies across the wire.
ENVELOPE_PROPERTIES = [
    "id",
    "threadId",
    "subject",
    "from",
    "to",
    "receivedAt",
    "preview",
    "keywords",
    "hasAttachment",
    "size",
]

BODY_PROPERTIES = ENVELOPE_PROPERTIES + ["cc", "replyTo", "textBody", "htmlBody", "bodyValues"]


class JmapError(Exception):
    """Any JMAP-layer failure: no credentials, bad session, method error."""


# --------------------------------------------------------------------------
# Credentials (environment only)
# --------------------------------------------------------------------------

def _token() -> str:
    token = (os.getenv(TOKEN_ENV) or "").strip()
    if not token:
        raise JmapError(
            f"{TOKEN_ENV} is not set — mail is unavailable. Set the token in the "
            "gateway environment; it is never accepted as a tool argument."
        )
    return token


def _session_url() -> str:
    return (os.getenv(SESSION_ENV) or "").strip() or DEFAULT_SESSION_URL


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}


def has_token() -> bool:
    return bool((os.getenv(TOKEN_ENV) or "").strip())


# --------------------------------------------------------------------------
# HTTP (stdlib only)
# --------------------------------------------------------------------------

def _http_get_json(url: str, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https JMAP endpoint)
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https JMAP endpoint)
        return json.loads(resp.read().decode("utf-8"))


def _wrap_http(exc: Exception, what: str) -> JmapError:
    """Never let a transport error carry the Authorization header into a room."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return JmapError(f"{what} failed — HTTP {exc.code}: the token was rejected.")
        return JmapError(f"{what} failed — HTTP {exc.code}.")
    return JmapError(f"{what} failed — {type(exc).__name__}.")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

def fetch_session(timeout: int = DEFAULT_TIMEOUT) -> dict:
    """GET the JMAP session object. Raises before any network if unauthenticated."""
    headers = _auth_headers()  # fails closed on a missing token
    try:
        session = _http_get_json(_session_url(), headers, timeout)
    except Exception as exc:  # noqa: BLE001 — normalized below
        raise _wrap_http(exc, "JMAP session fetch") from None
    if not isinstance(session, dict):
        raise JmapError("JMAP session fetch failed — unexpected response shape.")
    return session


def _api_url(session: dict) -> str:
    api_url = str(session.get("apiUrl") or "").strip()
    if not api_url:
        raise JmapError("JMAP session has no apiUrl.")
    return api_url


def _account_id(session: dict) -> str:
    primary = (session.get("primaryAccounts") or {}).get(MAIL_CAPABILITY)
    if not primary:
        raise JmapError("JMAP session exposes no primary mail account.")
    return str(primary)


# --------------------------------------------------------------------------
# Method calls
# --------------------------------------------------------------------------

def _call(api_url: str, method_calls: list, timeout: int) -> list:
    body = {"using": [CORE_CAPABILITY, MAIL_CAPABILITY], "methodCalls": method_calls}
    try:
        payload = _http_post_json(api_url, body, _auth_headers(), timeout)
    except Exception as exc:  # noqa: BLE001 — normalized below
        raise _wrap_http(exc, "JMAP request") from None
    responses = (payload or {}).get("methodResponses")
    if not isinstance(responses, list):
        raise JmapError("JMAP request failed — no methodResponses in the reply.")
    return responses


def _response(responses: list, name: str, call_id: str) -> dict:
    """Pull one method response by call id, turning a JMAP ``error`` into a raise."""
    for entry in responses:
        if not (isinstance(entry, list) and len(entry) == 3):
            continue
        method, args, cid = entry
        if cid != call_id:
            continue
        if method == "error":
            kind = (args or {}).get("type", "unknown")
            raise JmapError(f"JMAP {name} returned an error: {kind}.")
        if method == name:
            return args or {}
    raise JmapError(f"JMAP reply is missing the {name} response.")


# --------------------------------------------------------------------------
# Inbox
# --------------------------------------------------------------------------

def inbox_id(api_url: str, account_id: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Resolve the inbox by role, not by name — the display name is localized."""
    responses = _call(
        api_url,
        [["Mailbox/query", {"accountId": account_id, "filter": {"role": "inbox"}, "limit": 1}, "m0"]],
        timeout,
    )
    ids = _response(responses, "Mailbox/query", "m0").get("ids") or []
    if not ids:
        raise JmapError("This account has no mailbox with the 'inbox' role.")
    return str(ids[0])


def list_inbox(limit: int = DEFAULT_LIST_LIMIT, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Newest-first inbox envelopes. No bodies — use :func:`read_email` for one."""
    limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    session = fetch_session(timeout)
    api_url, account_id = _api_url(session), _account_id(session)
    mailbox_id = inbox_id(api_url, account_id, timeout)

    responses = _call(
        api_url,
        [
            [
                "Email/query",
                {
                    "accountId": account_id,
                    "filter": {"inMailbox": mailbox_id},
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": limit,
                    "collapseThreads": False,
                },
                "q0",
            ],
            [
                "Email/get",
                {
                    "accountId": account_id,
                    "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                    "properties": ENVELOPE_PROPERTIES,
                },
                "g0",
            ],
        ],
        timeout,
    )
    query = _response(responses, "Email/query", "q0")
    emails = _response(responses, "Email/get", "g0").get("list") or []

    # Email/get does not promise query order; restore newest-first.
    order = {eid: i for i, eid in enumerate(query.get("ids") or [])}
    emails = sorted(emails, key=lambda e: order.get(e.get("id"), len(order)))

    return {
        "account_id": account_id,
        "mailbox_id": mailbox_id,
        "total": query.get("total"),
        "emails": emails,
    }


def read_email(email_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """One message, with its body decoded to text."""
    email_id = str(email_id or "").strip()
    if not email_id:
        raise JmapError("An email id is required.")
    session = fetch_session(timeout)
    api_url, account_id = _api_url(session), _account_id(session)

    responses = _call(
        api_url,
        [
            [
                "Email/get",
                {
                    "accountId": account_id,
                    "ids": [email_id],
                    "properties": BODY_PROPERTIES,
                    "fetchTextBodyValues": True,
                    "fetchHTMLBodyValues": True,
                    "maxBodyValueBytes": MAX_BODY_BYTES,
                },
                "g0",
            ]
        ],
        timeout,
    )
    found = _response(responses, "Email/get", "g0").get("list") or []
    if not found:
        raise JmapError(f"No message with id {email_id} in this account.")
    email = found[0]

    body, truncated = _text_body(email)
    result = {k: email.get(k) for k in ENVELOPE_PROPERTIES}
    result.update({
        "cc": email.get("cc"),
        "replyTo": email.get("replyTo"),
        "body": body,
        "truncated": truncated,
    })
    return result


# --------------------------------------------------------------------------
# Body extraction — text, never a raw MIME dump
# --------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BREAK_RE = re.compile(r"<(br\s*/?|/p|/div|/tr|/li|/h[1-6])\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _strip_html(markup: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = text.replace(" ", " ").replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def _collect(email: dict, key: str) -> tuple[str, bool]:
    body_values = email.get("bodyValues") or {}
    chunks: list[str] = []
    truncated = False
    for part in email.get(key) or []:
        value = body_values.get(str((part or {}).get("partId") or ""))
        if not isinstance(value, dict):
            continue
        text = value.get("value")
        if not isinstance(text, str) or not text:
            continue
        chunks.append(text)
        truncated = truncated or bool(value.get("isTruncated"))
    return "\n\n".join(chunks), truncated


def _text_body(email: dict) -> tuple[str, bool]:
    """Prefer the text/plain parts; fall back to HTML stripped to text."""
    text, truncated = _collect(email, "textBody")
    if text.strip():
        return text.strip(), truncated
    markup, truncated = _collect(email, "htmlBody")
    if markup.strip():
        return _strip_html(markup), truncated
    return "", False


# --------------------------------------------------------------------------
# Rendering helpers (shared with jmap_tools)
# --------------------------------------------------------------------------

def format_address(entry: Optional[dict]) -> str:
    if not isinstance(entry, dict):
        return "?"
    name, addr = (entry.get("name") or "").strip(), (entry.get("email") or "").strip()
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name or "?"


def format_addresses(entries: Any, limit: int = 3) -> str:
    entries = entries if isinstance(entries, list) else []
    shown = [format_address(e) for e in entries[:limit]]
    if len(entries) > limit:
        shown.append(f"(+{len(entries) - limit} more)")
    return ", ".join(shown) or "?"


def is_unread(email: dict) -> bool:
    return not (email.get("keywords") or {}).get("$seen", False)
