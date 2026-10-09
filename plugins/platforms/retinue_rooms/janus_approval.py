"""Gateway-only Janus confirm-tier approvals (novique-ai/retinue#266).

The operator API lives outside this process. Rooms talk to it with a small
client so tests can inject a fake. Configuration is two gateway-process
environment variables:

  ``RETINUE_JANUS_APPROVAL_URL``    operator API origin, no path
  ``RETINUE_JANUS_APPROVAL_TOKEN``  bearer token for that API

The token is never written to a member environment, MCP config, the room
transcript, or a log line. Call arguments travel only in the authenticated
HTTP response the browser fetches for the approval card — not in the
transcript line that pauses the room.
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def project_detail(data: Mapping[str, Any], approval_id: str) -> Dict[str, Any]:
    """Authoritative fields for the authenticated UI. Unknown keys are dropped
    so an upstream token cannot ride along in the browser response."""
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
