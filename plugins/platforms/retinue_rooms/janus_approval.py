"""Gateway-only Janus confirm-tier approvals (novique-ai/retinue#266).

The operator API lives outside this process. Rooms talk to it with a small
client so tests can inject a fake. Configuration is gateway-process
environment variables:

  ``RETINUE_JANUS_APPROVAL_URL``    operator API origin, no path
  ``RETINUE_JANUS_APPROVAL_TOKEN``  bearer token for that API
  ``RETINUE_JANUS_MCP_SERVER``      MCP server name whose tool results may
                                    bind an approval (default ``janus``)

The token is never written to a member environment, MCP config, the room
transcript, or a log line. Call arguments travel only in the authenticated
HTTP response the browser fetches for the approval card — not in the
transcript line that pauses the room.

A spoken line does not bind a room. Janus identity is shared, and Janus
``session_id`` is an MCP session key, not a Retinue room id. An in-process
Hermes turn binds the room captured when the gateway observed the Janus
tool result. A Grok Build turn has no trustworthy tool-completion payload.
The gateway mints a fresh opaque claim per (room, member, MCP session),
sends it only as ``X-Retinue-Room-Binding`` on that session's Janus HTTP
entry, and compares the operator detail's ``room_binding`` in constant
time before binding. The claim is not written to a child environment,
MCP config, prompt, transcript, tool title, log, or model-facing result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

URL_ENV = "RETINUE_JANUS_APPROVAL_URL"
TOKEN_ENV = "RETINUE_JANUS_APPROVAL_TOKEN"
JANUS_MCP_SERVER_ENV = "RETINUE_JANUS_MCP_SERVER"
DEFAULT_JANUS_MCP_SERVER = "janus"
# A confirm-tier preview is small. Anything larger is not the structured
# result this parser will trust (Codex also slices mcpToolCall results).
_MAX_TOOL_RESULT_CHARS = 64 * 1024
# Both stay on the gateway process. Member isolation is
# grokbuild.member_subprocess_env: an allowlist plus a name scrub that
# already includes these two. This module does not copy a child environment.

# Only the rooms HTTP handler may pass this. Agent lines, tool calls, and
# a spoofed ``from`` on an ordinary message post must use anything else
# and will not reach the decision API.
HTTP_ORIGIN = "http"

JANUS_SPEAKER = "janus"
LINE_PREFIX = "Janus approval request "

# Spoken contract the briefing teaches. The label is required so ordinary
# prose that merely says "approval" does not become a card.
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{3,127}"
_ID_RE = re.compile(rf"^{_ID}$")
_LABELED_ID = re.compile(
    rf"""(?ix)
    approval_request_id
    (?:\\?["']?\s*[:=]\s*\\?["']?)
    ({_ID})
    """
)

PENDING_STATUSES = frozenset({"pending", "needs_confirmation"})
APPROVE_STATUSES = frozenset({"approve", "approved"})
DENY_STATUSES = frozenset({"deny", "denied"})

_DETAIL_STRINGS = ("status", "identity", "capability_id", "reason", "expires_at")
_MAX_BODY = 1024 * 1024
_DEFAULT_TIMEOUT = 10.0

# (method, url, body or None, bearer token) -> (http status, parsed object)
Transport = Callable[[str, str, Optional[Dict[str, Any]], str], Tuple[int, Dict[str, Any]]]

_PUBLIC = {
    "not_configured": "janus approvals are not configured",
    "expired": "approval request has expired",
    "not_pending": "approval request is not pending",
    "already_decided": "approval request was already decided",
    "not_found": "no such approval",
    "mismatch": "approval service returned a different request",
    "rejected": "approval service did not accept the decision",
    "transport": "approval service is unavailable",
    "bad_response": "approval service returned an unreadable response",
    "too_large": "approval service response was too large",
    "bad_id": "invalid approval id",
    "upstream_auth": "approval service rejected the gateway credential",
}


class JanusApprovalError(Exception):
    """Fail-closed approval error. The message is public and never includes
    a token, call arguments, or an upstream body."""

    def __init__(self, code: str):
        self.code = code if code in _PUBLIC else "transport"
        self.public_message = _PUBLIC[self.code]
        super().__init__(self.public_message)

    def http_status(self) -> int:
        if self.code == "not_configured":
            return 503
        if self.code in {"not_found", "bad_id"}:
            return 404
        if self.code in {"expired", "not_pending", "already_decided", "rejected"}:
            return 409
        return 502


def extract_approval_request_ids(text: str) -> List[str]:
    """Ids an agent spoke with the ``approval_request_id`` label, in order."""
    found: List[str] = []
    seen = set()
    for match in _LABELED_ID.finditer(text or ""):
        approval_id = match.group(1)
        if approval_id in seen or not _ID_RE.fullmatch(approval_id):
            continue
        seen.add(approval_id)
        found.append(approval_id)
    return found


def gateway_approval_line(approval_id: str) -> str:
    """Transcript line that pauses the room. No call arguments."""
    return (
        f"{LINE_PREFIX}{approval_id}. "
        "A confirm-tier call is waiting. "
        "It runs only if the agent retries after you approve. "
        "@user"
    )


def principal_decision_line(decision: str, approval_id: str) -> str:
    if decision == "approve":
        return (
            f"Approved Janus request {approval_id}. "
            "The call does not run until the agent retries it."
        )
    return f"Denied Janus request {approval_id}. Do not retry that call."


def unverified_notice(approval_id: str) -> str:
    return (
        f"Janus confirmation {approval_id} could not be verified. "
        "Nothing was approved."
    )


def unavailable_notice(approval_id: str) -> str:
    return (
        f"Janus confirmation {approval_id} is not available in this room. "
        "Nothing was approved."
    )


def expired_notice(approval_id: str) -> str:
    return f"Janus confirmation {approval_id} is expired. Nothing was approved."


def valid_id(approval_id: str) -> bool:
    return bool(_ID_RE.fullmatch(approval_id or ""))


class _Reject(Exception):
    """Parser failure. Carries no payload so a log of the type is safe."""


# Janus stores the header lowercased. 43 is secrets.token_urlsafe(32)
# with the base64 padding removed: alphabet A-Za-z0-9_-.
ROOM_BINDING_HEADER = "X-Retinue-Room-Binding"
ROOM_BINDING_LENGTH = 43
_ROOM_BINDING_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ROOM_BINDING_HEADER_FOLDED = ROOM_BINDING_HEADER.lower()


def mint_room_binding() -> str:
    """Fresh opaque claim. One per Grok (room, member, MCP session).

    ``token_urlsafe(32)`` is 43 characters. A shape miss raises so a bad
    value is never sent. The caller keeps the result in process memory.
    """
    value = secrets.token_urlsafe(32)
    if _ROOM_BINDING_RE.fullmatch(value) is None:
        raise RuntimeError("room binding mint failed")
    return value


def _room_binding_bytes(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or _ROOM_BINDING_RE.fullmatch(value) is None:
        return None
    return value.encode("utf-8")


def claim_matches(expected: Any, presented: Any) -> bool:
    """Constant-time compare of two room claims.

    A missing, non-string, or malformed value does not match. Unequal
    lengths still run ``compare_digest`` on the expected bytes so the
    failure does not return early. Neither value is logged.
    """
    left = _room_binding_bytes(expected)
    if left is None:
        filler = b"x" * ROOM_BINDING_LENGTH
        secrets.compare_digest(filler, filler)
        return False
    right = _room_binding_bytes(presented)
    if right is None or len(right) != len(left):
        secrets.compare_digest(left, left)
        return False
    return secrets.compare_digest(left, right)


def attach_janus_room_binding(
    servers: Any,
    claim: str,
    server_name: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Copy ``servers`` and put ``claim`` on the named Janus HTTP entry.

    Callers pass the list already scrubbed of gateway credentials. The
    header is appended only to the first ``http`` or ``sse`` server of
    ``server_name``. An existing header of the same name is replaced so
    Janus does not see a duplicate. Stdio entries are copied unchanged
    and the claim is not written into their env. ``attached`` is False
    when no such HTTP entry exists; the caller must not retain the claim.
    """
    if _room_binding_bytes(claim) is None:
        raise RuntimeError("room binding rejected")
    name = str(server_name or "").strip()
    copied: List[Dict[str, Any]] = []
    attached = False
    for entry in servers or []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        if isinstance(item.get("headers"), list):
            item["headers"] = [
                dict(header) for header in item["headers"] if isinstance(header, dict)
            ]
        if isinstance(item.get("env"), list):
            item["env"] = [dict(pair) for pair in item["env"] if isinstance(pair, dict)]
        if isinstance(item.get("args"), list):
            item["args"] = list(item["args"])
        if (
            not attached
            and name
            and item.get("name") == name
            and item.get("type") in ("http", "sse")
        ):
            headers = [
                header
                for header in (item.get("headers") or [])
                if str(header.get("name") or "").lower() != _ROOM_BINDING_HEADER_FOLDED
            ]
            headers.append({"name": ROOM_BINDING_HEADER, "value": claim})
            item["headers"] = headers
            attached = True
        copied.append(item)
    return copied, attached


def configured_janus_mcp_server(environ: Optional[Mapping[str, str]] = None) -> str:
    """MCP server whose tool results may bind an approval.

    Unset means the documented Janus server name. Set-but-empty disables
    tool-result binding (fail closed). This is not a secret.
    """
    env = os.environ if environ is None else environ
    if JANUS_MCP_SERVER_ENV not in env:
        return DEFAULT_JANUS_MCP_SERVER
    return str(env.get(JANUS_MCP_SERVER_ENV) or "").strip()


def is_janus_mcp_tool(tool_name: str, server: str) -> bool:
    """True for Hermes ``mcp__<server>__<tool>`` or Codex ``mcp.<server>.<tool>``.

    The server name is the configured Janus MCP server, not a name taken
    from the tool title or the result body. A shell tool never matches.
    """
    name = str(tool_name or "").strip()
    server_name = str(server or "").strip()
    if not name or not server_name:
        return False
    if any(mark in server_name for mark in ("__", ".", "/", " ", "\n", "\r")):
        return False
    hermes = f"mcp__{server_name}__"
    codex = f"mcp.{server_name}."
    if name.startswith(hermes) and len(name) > len(hermes):
        return True
    if name.startswith(codex) and len(name) > len(codex):
        return True
    return False


def approval_id_from_tool_result(
    tool_name: str,
    result: Any,
    *,
    server: str,
) -> Optional[str]:
    """The single approval id in a Janus MCP tool result, or None.

    Accepts the broker object itself or a Hermes envelope whose ``result``
    is that object (dict or JSON text) and/or whose ``structuredContent``
    is that object. ``status`` must be ``needs_confirmation`` and the
    object must carry exactly one ``approval_request_id``. Malformed,
    truncated, disagreeing, or ambiguous input returns None. This does
    not read a room id out of the payload.
    """
    if not is_janus_mcp_tool(tool_name, server):
        return None
    try:
        obj = _decode_tool_object(result)
        if obj is None:
            return None
        return _extract_confirmation_id(obj, depth=0)
    except _Reject:
        return None


def trusted_retinue_room(source: Any, metadata: Any) -> Optional[str]:
    """Room id stamped on this turn, when it matches the routed chat.

    ``metadata['retinue_room']`` is the adapter's own stamp. A room id in
    tool output, a title, or a metadata value that disagrees with
    ``source.chat_id`` is not a claim. Returns None unless the platform
    is Retinue rooms.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("retinue_room")
    if not isinstance(raw, str):
        return None
    room_id = raw.strip()
    if not room_id or room_id != raw:
        return None
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    if str(platform_value or "").strip().lower() != "retinue_rooms":
        return None
    chat_id = getattr(source, "chat_id", None)
    if not isinstance(chat_id, str) or chat_id != room_id:
        return None
    return room_id


def room_tool_complete_callback(adapter: Any, room_id: str) -> Optional[Callable]:
    """Tool-complete closure bound to one room turn.

    ``room_id`` is the string captured when the turn was set up. The
    callback does not read a process-global current room and does not log
    arguments or the result body.
    """
    observe = getattr(adapter, "observe_janus_tool_result", None)
    if not callable(observe) or not isinstance(room_id, str) or not room_id:
        return None

    def _callback(_call_id: Any, tool_name: Any, _args: Any, result: Any) -> None:
        try:
            observe(room_id, tool_name, result)
        except Exception as exc:
            logger.info(
                "janus tool bind failed room=%s error=%s",
                room_id,
                type(exc).__name__,
            )

    return _callback


def compose_tool_complete_callbacks(
    primary: Optional[Callable],
    extra: Optional[Callable],
) -> Optional[Callable]:
    """Run the existing callback, then the room callback.

    A missing extra returns ``primary`` unchanged so a Slack-only turn
    keeps today's exception behavior. When both are set, one failure
    does not skip the other, and neither callback's arguments are logged.
    """
    if extra is None:
        return primary
    if primary is None:
        return extra

    def _both(call_id: Any, tool_name: Any, args: Any, result: Any) -> None:
        for callback in (primary, extra):
            try:
                callback(call_id, tool_name, args, result)
            except Exception as exc:
                logger.info(
                    "tool complete callback failed error=%s",
                    type(exc).__name__,
                )

    return _both


def _decode_tool_object(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, dict):
        return result
    if isinstance(result, (bytes, bytearray)):
        try:
            result = result.decode("utf-8")
        except UnicodeDecodeError:
            raise _Reject from None
    if not isinstance(result, str):
        raise _Reject
    if len(result) > _MAX_TOOL_RESULT_CHARS:
        raise _Reject
    text = result.strip()
    if not text:
        return None
    if text[0] not in "{[":
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        raise _Reject from None
    if not isinstance(parsed, dict):
        raise _Reject
    return parsed


def _extract_confirmation_id(obj: Dict[str, Any], depth: int) -> Optional[str]:
    if depth > 2:
        raise _Reject
    has_structured = "structuredContent" in obj
    has_result = "result" in obj
    direct = _direct_confirmation_id(obj)
    if not has_structured and not has_result:
        return direct
    if direct is not None:
        raise _Reject
    found: List[str] = []
    if has_structured:
        found.append(_required_confirmation(obj.get("structuredContent"), depth))
    if has_result:
        extra = _optional_result_id(obj.get("result"), depth)
        if extra is not None:
            found.append(extra)
    if not found:
        return None
    if any(item != found[0] for item in found):
        raise _Reject
    return found[0]


def _required_confirmation(value: Any, depth: int) -> str:
    embedded = _coerce_embedded_object(value)
    if _is_envelope(embedded):
        got = _extract_confirmation_id(embedded, depth + 1)
    else:
        got = _direct_confirmation_id(embedded)
    if not got:
        raise _Reject
    return got


def _optional_result_id(value: Any, depth: int) -> Optional[str]:
    """Id from a ``result`` slot.

    Prose is ignored (it is not a claim). JSON text that does not parse,
    or a parsed object that is not this confirmation, fails the whole
    envelope so a truncated copy cannot sit beside a structured one.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > _MAX_TOOL_RESULT_CHARS:
            raise _Reject
        text = value.strip()
        if not text:
            return None
        if text[0] not in "{[":
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            raise _Reject from None
        if not isinstance(parsed, dict):
            raise _Reject
        value = parsed
    if not isinstance(value, dict):
        raise _Reject
    if _is_envelope(value):
        got = _extract_confirmation_id(value, depth + 1)
        if not got:
            raise _Reject
        return got
    got = _direct_confirmation_id(value)
    if not got:
        raise _Reject
    return got


def _is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and (
        "structuredContent" in obj or "result" in obj
    ) and "approval_request_id" not in obj and "status" not in obj


def _coerce_embedded_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TOOL_RESULT_CHARS:
            raise _Reject
        text = value.strip()
        if not text or text[0] not in "{[":
            raise _Reject
        try:
            parsed = json.loads(text)
        except ValueError:
            raise _Reject from None
        if isinstance(parsed, dict):
            return parsed
    raise _Reject


def _direct_confirmation_id(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    if "status" not in obj and "approval_request_id" not in obj:
        return None
    status = obj.get("status")
    if not isinstance(status, str) or status.strip().lower() != "needs_confirmation":
        raise _Reject
    raw = obj.get("approval_request_id")
    if not isinstance(raw, str):
        raise _Reject
    approval_id = raw.strip()
    if approval_id != raw or not valid_id(approval_id):
        raise _Reject
    ids: List[Any] = []
    statuses: List[Any] = []
    seen: set = set()
    _walk_key(obj, "approval_request_id", ids, seen, 0)
    _walk_key(obj, "status", statuses, set(), 0)
    if len(ids) != 1 or not isinstance(ids[0], str) or ids[0].strip() != approval_id:
        raise _Reject
    for item in statuses:
        if not isinstance(item, str) or item.strip().lower() != "needs_confirmation":
            raise _Reject
    return approval_id


def _walk_key(value: Any, key: str, found: List[Any], seen: set, depth: int) -> None:
    if depth > 6:
        raise _Reject
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            raise _Reject
        seen.add(marker)
        if len(value) > 64:
            raise _Reject
        for item_key, item in value.items():
            if item_key == key:
                found.append(item)
            _walk_key(item, key, found, seen, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise _Reject
        for item in value:
            _walk_key(item, key, found, seen, depth + 1)


def project_detail(data: Mapping[str, Any], approval_id: str) -> Dict[str, Any]:
    """Authoritative fields for the authenticated UI. Unknown keys are dropped
    so an upstream token cannot ride along in the browser response.

    ``room_binding`` is operator-only. It is not a public field and is not
    copied here.
    """
    got = str(data.get("approval_request_id") or "").strip()
    if got and got != approval_id:
        raise JanusApprovalError("mismatch")
    out: Dict[str, Any] = {"approval_request_id": approval_id}
    for key in _DETAIL_STRINGS:
        if key not in data or data[key] is None:
            continue
        out[key] = data[key] if key == "status" else str(data[key])
    out["status"] = str(out.get("status") or "").strip().lower()
    if "arguments" in data:
        out["arguments"] = data["arguments"]
    if "env" in data:
        out["env"] = data["env"]
    return out


def status_matches(status: str, decision: str) -> bool:
    folded = (status or "").strip().lower()
    if decision == "approve":
        return folded in APPROVE_STATUSES
    if decision == "deny":
        return folded in DENY_STATUSES
    return False


def is_pending(status: str) -> bool:
    return (status or "").strip().lower() in PENDING_STATUSES


def expires_in_past(value: Any, now: Optional[float] = None) -> bool:
    """True when ``expires_at`` is already past, or present but unreadable.

    Absent means the status field is authoritative. Unreadable fails closed.
    """
    if value is None or value == "":
        return False
    current = time.time() if now is None else now
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts <= current
    text = str(value).strip()
    if not text:
        return False
    try:
        return expires_in_past(float(text), current)
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() <= current


class ApprovalBindings:
    """Gateway-owned id → room map. Survives restart. Stores no arguments."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._load().get(approval_id)
            return dict(row) if isinstance(row, dict) else None

    def bind(self, approval_id: str, room_id: str) -> Dict[str, Any]:
        """Bind ``approval_id`` to ``room_id``. A different room does not steal it.

        The caller chooses ``room_id``. Janus ``session_id`` is not a room
        claim and is not consulted here.
        """
        with self._lock:
            data = self._load()
            row = data.get(approval_id)
            if isinstance(row, dict) and row.get("room_id") not in (None, "", room_id):
                return dict(row)
            if not isinstance(row, dict):
                row = {
                    "approval_request_id": approval_id,
                    "room_id": room_id,
                    "bound_at": time.time(),
                    "surfaced": False,
                    "decision": None,
                }
            else:
                row["room_id"] = room_id
                row.setdefault("approval_request_id", approval_id)
                row.setdefault("surfaced", False)
                row.setdefault("decision", None)
            data[approval_id] = {
                "approval_request_id": approval_id,
                "room_id": str(row.get("room_id") or room_id),
                "bound_at": float(row.get("bound_at") or time.time()),
                "surfaced": bool(row.get("surfaced")),
                "decision": row.get("decision") or None,
            }
            self._save(data)
            return dict(data[approval_id])

    def mark_surfaced(self, approval_id: str) -> None:
        self._update(approval_id, surfaced=True)

    def mark_decision(self, approval_id: str, decision: str) -> None:
        self._update(approval_id, decision=decision)

    def _update(self, approval_id: str, **fields: Any) -> None:
        with self._lock:
            data = self._load()
            row = data.get(approval_id)
            if not isinstance(row, dict):
                return
            row.update(fields)
            data[approval_id] = row
            self._save(data)

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp-{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)


def client_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional["JanusApprovalsClient"]:
    env = os.environ if environ is None else environ
    base_url = (env.get(URL_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    if not base_url or not token:
        return None
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.info("janus approval URL is not an http(s) origin; approvals are off")
        return None
    if parsed.username or parsed.password:
        logger.info("janus approval URL must not carry credentials; approvals are off")
        return None
    # Origin only. A path on the env value would double up with /v1/approvals.
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return JanusApprovalsClient(origin, token)


class JanusApprovalsClient:
    """Bearer client for ``GET/POST /v1/approvals/{id}``.

    ``transport`` replaces urllib in tests. It receives the bearer token so a
    test can assert the header, and must not log it.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: Optional[Transport] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"JanusApprovalsClient(configured=True, origin_set={bool(self._base_url)})"

    def get(self, approval_id: str) -> Dict[str, Any]:
        if not valid_id(approval_id):
            raise JanusApprovalError("bad_id")
        status, payload = self._call("GET", self._url(approval_id), None)
        self._raise_for_status(status)
        return project_detail(payload, approval_id)

    def inspect(self, approval_id: str) -> Dict[str, Any]:
        """Operator payload for the Grok claim compare.

        This keeps ``room_binding``. ``get`` does not. Callers must not log
        the dict, put it on a transcript, or return it from the HTTP card.
        """
        if not valid_id(approval_id):
            raise JanusApprovalError("bad_id")
        status, payload = self._call("GET", self._url(approval_id), None)
        self._raise_for_status(status)
        if not isinstance(payload, dict):
            raise JanusApprovalError("bad_response")
        return payload

    def decide(self, approval_id: str, decision: str) -> Dict[str, Any]:
        if decision not in {"approve", "deny"}:
            raise JanusApprovalError("rejected")
        if not valid_id(approval_id):
            raise JanusApprovalError("bad_id")
        status, payload = self._call(
            "POST",
            self._url(approval_id, "/decision"),
            {"decision": decision},
        )
        if status == 409:
            projected = _decision_view(payload, decision)
            if status_matches(projected["status"], decision) or projected["decision"] == decision:
                return {
                    "approval_request_id": approval_id,
                    "status": projected["status"] or ("approved" if decision == "approve" else "denied"),
                    "decision": decision,
                    "duplicate": True,
                }
            raise JanusApprovalError("already_decided")
        self._raise_for_status(status)
        projected = _decision_view(payload, decision)
        if not (
            status_matches(projected["status"], decision) or projected["decision"] == decision
        ):
            raise JanusApprovalError("rejected")
        return {
            "approval_request_id": approval_id,
            "status": projected["status"] or ("approved" if decision == "approve" else "denied"),
            "decision": decision,
            "duplicate": False,
        }

    def _url(self, approval_id: str, suffix: str = "") -> str:
        quoted = urllib.parse.quote(approval_id, safe="")
        return f"{self._base_url}/v1/approvals/{quoted}{suffix}"

    def _call(
        self,
        method: str,
        url: str,
        body: Optional[Dict[str, Any]],
    ) -> Tuple[int, Dict[str, Any]]:
        if self._transport is not None:
            try:
                status, payload = self._transport(method, url, body, self._token)
            except JanusApprovalError:
                raise
            except Exception:
                raise JanusApprovalError("transport") from None
            if not isinstance(payload, dict):
                raise JanusApprovalError("bad_response")
            return int(status), payload
        return self._urllib(method, url, body)

    def _urllib(
        self,
        method: str,
        url: str,
        body: Optional[Dict[str, Any]],
    ) -> Tuple[int, Dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return int(response.status), _read_object(response)
        except urllib.error.HTTPError as exc:
            # The body can carry call arguments. Read it for the status field
            # and do not attach it to the exception we raise later.
            try:
                payload = _read_object(exc)
            except JanusApprovalError:
                payload = {}
            return int(exc.code), payload
        except Exception:
            raise JanusApprovalError("transport") from None

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status == 404:
            raise JanusApprovalError("not_found")
        if status in {401, 403}:
            raise JanusApprovalError("upstream_auth")
        if status >= 400:
            raise JanusApprovalError("transport")


def _read_object(response: Any) -> Dict[str, Any]:
    raw = response.read(_MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        raise JanusApprovalError("too_large")
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise JanusApprovalError("bad_response") from None
    if not isinstance(data, dict):
        raise JanusApprovalError("bad_response")
    return data


def _decision_view(payload: Mapping[str, Any], decision: str) -> Dict[str, str]:
    """Status and decision only. Arguments in the upstream body are discarded."""
    status = str(payload.get("status") or "").strip().lower()
    got = str(payload.get("decision") or "").strip().lower()
    if got not in {"approve", "deny"}:
        got = decision if status_matches(status, decision) else ""
    return {"status": status, "decision": got}
