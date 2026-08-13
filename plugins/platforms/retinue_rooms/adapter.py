"""Retinue rooms platform adapter.

Shared conversations between the user and N named agents (Hermes profiles),
with mention-driven turn-taking and a per-user-message turn budget. Design
notes: retinue/ROOMS.md. Follows the A2A plugin's proven mechanics:

  - stdlib ThreadingHTTPServer in a daemon thread; localhost-only bind when
    no RETINUE_ROOMS_API_KEY is configured.
  - Agent turns are injected into live gateway sessions via ``handle_message``
    with ``source.profile`` stamped per member; ``internal=True`` so a busy
    session queues instead of interrupting.
  - Reply capture: ``send()`` resolves the pending turn future only for sends
    carrying the gateway's ``metadata['notify']`` final-reply marker;
    ``on_processing_complete`` resolves failures/cancellations promptly.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)

from . import engine, hire
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8643
_MAX_BODY = 262_144  # 256 KB is plenty for a chat message
_DEFAULT_USER_NAME = "User"


def turn_timeout() -> float:
    try:
        return max(5.0, float(os.getenv("RETINUE_ROOMS_TURN_TIMEOUT", "300")))
    except (ValueError, TypeError):
        return 300.0


def rooms_enabled() -> bool:
    if (os.getenv("RETINUE_ROOMS_ENABLED") or "").strip().lower() in ("1", "true", "yes"):
        return True
    return bool((os.getenv("RETINUE_ROOMS_API_KEY") or "").strip())


@dataclass
class _PendingTurn:
    task_id: str
    room_id: str
    member: str
    future: Future  # resolves to (ok: bool, text: str)


class RetinueRoomsAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config=config, platform=Platform("retinue_rooms"))
        # Shared-group sessions => the gateway prefixes "[speaker]" on inbound
        # text, which is how members tell each other apart. Load-bearing.
        self.config.extra.setdefault("group_sessions_per_user", False)

        self.host = os.getenv("RETINUE_ROOMS_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            self.port = int(os.getenv("RETINUE_ROOMS_PORT", str(_DEFAULT_PORT)))
        except (ValueError, TypeError):
            self.port = _DEFAULT_PORT
        self.api_key = (os.getenv("RETINUE_ROOMS_API_KEY") or "").strip()
        if not self.api_key and self.host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "Retinue rooms: no API key set — forcing localhost bind (was %s)",
                self.host,
            )
            self.host = "127.0.0.1"

        self.store = RoomStore()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._room_locks: Dict[str, asyncio.Lock] = {}
        self._pending: Dict[str, _PendingTurn] = {}  # room_id -> pending turn
        self._pending_lock = threading.Lock()

    def _live_runner(self):
        """The in-process GatewayRunner, if this adapter is serving."""
        runner = getattr(self, "gateway_runner", None)
        if runner is not None:
            return runner
        try:
            from gateway.run import _gateway_runner_ref

            return _gateway_runner_ref()
        except Exception:
            return None

    def _activate_slug(self, slug: str) -> Dict[str, Any]:
        return hire.activate_hired_profile(slug, runner=self._live_runner())

    def _rescan_disk_profiles(self) -> None:
        """Pick up profiles created while we were down (or hired before
        hot-register existed). Idempotent with the multiplexer's own
        startup scan."""
        runner = self._live_runner()
        if runner is None:
            return
        for agent in hire.list_agents(self._home_dir()):
            slug = str(agent.get("slug") or "").strip()
            if slug and slug != "default":
                hire.activate_hired_profile(slug, runner=runner)

    # ── identity ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Retinue Rooms"

    @property
    def authorization_is_upstream(self) -> bool:
        """HTTP callers are authenticated by bearer token (or localhost-only
        bind) before dispatch; agent-to-agent turns are internal events."""
        return True

    # ── lifecycle ────────────────────────────────────────────────────────

    async def connect(self, **_kwargs) -> bool:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        try:
            self._httpd = _RoomsServer((self.host, self.port), _RoomsRequestHandler, self)
        except OSError as e:
            logger.error("Retinue rooms: could not bind %s:%s — %s", self.host, self.port, e)
            self._set_fatal_error("bind_failed", f"Rooms bind failed: {e}", retryable=True)
            return False
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever, name="retinue-rooms-http", daemon=True
        )
        self._server_thread.start()
        try:
            self._rescan_disk_profiles()
        except Exception:
            logger.debug("Retinue rooms: profile rescan at connect failed", exc_info=True)
        logger.info("Retinue rooms: serving on %s:%s", self.host, self.port)
        return True

    async def disconnect(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        with self._pending_lock:
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_result((False, "rooms adapter shutting down"))
            self._pending.clear()

    # ── reply capture (A2A pattern) ──────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Resolve the pending turn for this room. Only sends carrying the
        gateway's ``notify`` final-reply marker count; progress/preview sends
        must not satisfy a turn."""
        message_id = str(int(time.time() * 1000))
        if not (metadata or {}).get("notify"):
            return SendResult(success=True, message_id=message_id)
        self._resolve_pending(chat_id, ok=True, text=content or "")
        return SendResult(success=True, message_id=message_id)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Failure/cancel/empty-run path — the success path resolves in send()."""
        task_id = str(getattr(event, "message_id", "") or "")
        if not task_id:
            return
        with self._pending_lock:
            pending = None
            for candidate in self._pending.values():
                if candidate.task_id == task_id:
                    pending = candidate
                    break
        if pending is None or pending.future.done():
            return
        if outcome == ProcessingOutcome.FAILURE:
            self._resolve_pending(pending.room_id, ok=False, text="agent processing failed")
        elif outcome == ProcessingOutcome.CANCELLED:
            self._resolve_pending(pending.room_id, ok=False, text="turn cancelled")
        else:
            self._resolve_pending(pending.room_id, ok=False, text="agent returned no reply")

    def _resolve_pending(self, room_id: str, *, ok: bool, text: str) -> None:
        with self._pending_lock:
            pending = self._pending.pop(room_id, None)
        if pending is None:
            logger.debug("Retinue rooms: send for room %s had no pending turn", room_id)
            return
        if not pending.future.done():
            pending.future.set_result((ok, text))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        room = self.store.get(chat_id)
        return {"name": room.name if room else chat_id, "type": "group"}

    # ── room operations (called from HTTP worker threads) ────────────────

    def create_room(
        self,
        name: str,
        members: List[str],
        lead: Optional[str],
        max_agent_turns: Optional[int],
    ) -> Dict[str, Any]:
        members = [m.strip() for m in members if m and m.strip()]
        if not members:
            raise ValueError("a room needs at least one agent member")
        room = Room(
            id=engine.new_room_id(name),
            name=name.strip() or "room",
            members=members,
            lead=(lead or "").strip() or None,
            max_agent_turns=max(1, int(max_agent_turns or engine.DEFAULT_MAX_AGENT_TURNS)),
        )
        self.store.create(room)
        unknown = [m for m in members if not self._profile_exists(m)]
        payload = room.to_dict()
        if unknown:
            payload["warning"] = (
                "unknown profiles (create them before they can speak): " + ", ".join(unknown)
            )
        return payload

    @staticmethod
    def _home_dir() -> str:
        return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")

    @classmethod
    def _profile_exists(cls, member: str) -> bool:
        if member == "default":
            return True
        return os.path.isdir(os.path.join(cls._home_dir(), "profiles", member))

    # ── agents (the hire flow) ───────────────────────────────────────────

    def list_agents(self) -> List[Dict[str, Any]]:
        return hire.list_agents(self._home_dir())

    def list_model_presets(self) -> List[Dict[str, str]]:
        return hire.list_model_presets(self._home_dir())

    def hire_agent(
        self, name: str, job: str, how: str, model: Optional[str] = None
    ) -> Dict[str, Any]:
        meta = hire.scaffold_profile(
            self._home_dir(), name, job, how, model_preset=model
        )
        activation = self._activate_slug(meta["slug"])
        meta["online"] = bool(activation.get("online"))
        meta["activation"] = activation.get("activation") or (
            "online" if meta["online"] else
            "will come online the next time the gateway starts"
        )
        if meta["online"]:
            self._schedule_adapter_start(meta["slug"])
        return meta

    def _schedule_adapter_start(self, slug: str) -> None:
        """Best-effort: start any non-port-binding platforms on the new
        profile. Room turns do not need this (they route via source.profile
        on the already-running rooms adapter)."""
        runner = self._live_runner()
        loop = self._loop
        start = getattr(runner, "_start_one_profile_adapters", None) if runner else None
        if runner is None or loop is None or not callable(start):
            return
        try:
            from hermes_cli.profiles import get_profile_dir

            profile_home = get_profile_dir(slug)
        except Exception:
            return

        async def _go():
            try:
                await start(slug, profile_home, {})
            except Exception:
                logger.debug(
                    "Retinue rooms: secondary adapter start for %s failed",
                    slug,
                    exc_info=True,
                )

        try:
            asyncio.run_coroutine_threadsafe(_go(), loop)
        except Exception:
            logger.debug(
                "Retinue rooms: could not schedule adapter start for %s",
                slug,
                exc_info=True,
            )

    # ── static web UI ────────────────────────────────────────────────────

    @staticmethod
    def web_dist_dir() -> Optional[str]:
        """retinue-web/dist, when built. Resolved from this file:
        plugins/platforms/retinue_rooms/adapter.py -> repo root is 3 up."""
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        dist = os.path.join(repo_root, "retinue-web", "dist")
        return dist if os.path.isdir(dist) else None

    def post_user_message(self, room_id: str, text: str, from_name: str) -> Dict[str, Any]:
        room = self.store.get(room_id)
        if room is None:
            raise KeyError(room_id)
        if not text.strip():
            raise ValueError("empty message")
        if self._loop is None:
            raise RuntimeError("gateway loop not ready")
        message = self.store.append(
            room_id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker=from_name, text=text)
        )
        planned = engine.plan_user_turns(room, text)
        asyncio.run_coroutine_threadsafe(self._run_cycle(room_id, message), self._loop)
        return {"seq": message.seq, "planned": planned}

    # ── the turn cycle (runs on the gateway loop) ────────────────────────

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        lock = self._room_locks.get(room_id)
        if lock is None:
            lock = self._room_locks[room_id] = asyncio.Lock()
        return lock

    async def _run_cycle(self, room_id: str, user_message: RoomMessage) -> None:
        try:
            async with self._room_lock(room_id):
                await self._run_cycle_locked(room_id, user_message)
        except Exception:
            logger.exception("Retinue rooms: cycle for room %s crashed", room_id)
            self._post_system(room_id, "internal error running the turn cycle — see gateway log")

    async def _run_cycle_locked(self, room_id: str, user_message: RoomMessage) -> None:
        room = self.store.get(room_id)
        if room is None:
            return
        budget = room.max_agent_turns
        queue = engine.plan_user_turns(room, user_message.text)
        turns_taken = 0
        while queue:
            if turns_taken >= budget:
                self._post_system(
                    room_id,
                    f"turn budget ({budget}) reached — waiting for the next user message. "
                    f"Still queued: {', '.join(queue)}",
                )
                break
            member = queue.pop(0)
            turns_taken += 1
            ok, reply = await self._agent_turn(room, member)
            room = self.store.get(room_id) or room  # meta may have moved (last_seen)
            if not ok:
                self._post_system(room_id, f"{member} did not reply ({reply})")
                continue
            self.store.append(
                room_id, RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=member, text=reply)
            )
            followups = engine.plan_agent_followups(
                room, member, reply, queue, budget - turns_taken
            )
            queue.extend(followups)

    async def _agent_turn(self, room: Room, member: str) -> tuple[bool, str]:
        """Deliver the unseen transcript to ``member`` and await its reply."""
        delta = [
            m
            for m in self.store.read_since(room.id, room.last_seen.get(member, 0))
            # A member never re-reads its own lines; they are already in its
            # session history from the turn that produced them.
            if not (m.kind == KIND_AGENT and m.speaker == member)
        ]
        if not delta:
            return False, "nothing new to respond to"
        trigger = delta[-1]
        context_block = engine.format_lines(delta[:-1]) if len(delta) > 1 else None

        user_names = sorted(
            {m.speaker for m in self.store.read_since(room.id, 0) if m.kind == KIND_USER}
        ) or [_DEFAULT_USER_NAME]
        briefing = engine.room_briefing(room, member, user_names)

        speaker_display = (
            f"{trigger.speaker} (agent)" if trigger.kind == KIND_AGENT else trigger.speaker
        )
        source = self.build_source(
            chat_id=room.id,
            chat_name=f"room:{room.name}",
            chat_type="group",
            user_id=f"{trigger.kind}:{trigger.speaker}",
            user_name=speaker_display,
        )
        source.is_bot = trigger.kind == KIND_AGENT
        # Route this turn to the member's profile (in-process multiplexer).
        source.profile = None if member == "default" else member

        task_id = f"room-{room.id}-{int(time.time() * 1000)}"
        fut: Future = Future()
        with self._pending_lock:
            stale = self._pending.pop(room.id, None)
            if stale is not None and not stale.future.done():
                stale.future.set_result((False, "superseded by a newer turn"))
            self._pending[room.id] = _PendingTurn(
                task_id=task_id, room_id=room.id, member=member, future=fut
            )

        event = MessageEvent(
            text=trigger.text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=task_id,
            internal=True,  # queue behind a busy turn; never interrupt
            channel_prompt=briefing,
            channel_context=context_block,
            metadata={"retinue_room": room.id, "retinue_member": member},
        )

        # Mark the delta delivered before dispatch; on failure the trigger is
        # re-shown next turn only if new messages arrive — acceptable v1.
        room.last_seen[member] = max(room.last_seen.get(member, 0), delta[-1].seq)
        self.store.update(room)

        try:
            await self.handle_message(event)
        except Exception as e:
            self._resolve_pending(room.id, ok=False, text=f"dispatch failed: {e}")

        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=turn_timeout())
        except asyncio.TimeoutError:
            self._resolve_pending(room.id, ok=False, text="turn timed out")
            return False, f"no reply within {int(turn_timeout())}s"

    def _post_system(self, room_id: str, text: str) -> None:
        try:
            self.store.append(
                room_id, RoomMessage(seq=0, ts=0, kind=KIND_SYSTEM, speaker="room", text=text)
            )
        except Exception:
            logger.exception("Retinue rooms: failed to post system notice to %s", room_id)


# ── HTTP surface ─────────────────────────────────────────────────────────


class _RoomsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, adapter: RetinueRoomsAdapter):
        super().__init__(addr, handler)
        self.adapter = adapter


class _RoomsRequestHandler(BaseHTTPRequestHandler):
    server: _RoomsServer

    # ── plumbing ─────────────────────────────────────────────────────────

    def log_message(self, fmt, *args):  # route http.server noise to our logger
        logger.debug("Retinue rooms http: " + fmt, *args)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _authorized(self) -> bool:
        key = self.server.adapter.api_key
        if not key:
            return True  # no key => the server is bound localhost-only
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if token and hmac.compare_digest(token, key):
            return True
        # EventSource cannot set Authorization; accept the same secret as a
        # query param on the SSE route (and only via compare_digest).
        query = parse_qs(urlparse(self.path).query)
        qtoken = (query.get("access_token") or [""])[0] or ""
        return bool(qtoken) and hmac.compare_digest(qtoken, key)

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > _MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # ── routes ───────────────────────────────────────────────────────────

    _STATIC_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".woff2": "font/woff2",
        ".map": "application/json",
    }

    def _serve_static(self, path: str) -> bool:
        """Serve the built SPA (unauthenticated, like any web page — the API
        it calls stays bearer-gated). Returns False when no dist exists."""
        dist = self.server.adapter.web_dist_dir()
        if dist is None:
            return False
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(dist, rel))
        if not full.startswith(os.path.realpath(dist) + os.sep) and full != os.path.realpath(
            os.path.join(dist, "index.html")
        ):
            return False
        if not os.path.isfile(full):
            full = os.path.join(dist, "index.html")  # SPA fallback route
            if not os.path.isfile(full):
                return False
        ext = os.path.splitext(full)[1].lower()
        ctype = self._STATIC_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    _API_PREFIXES = ("rooms", "agents", "models", "health")

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["health"]:
            return self._json(200, {"ok": True, "rooms": len(self.server.adapter.store.list_rooms())})
        if not parts or parts[0] not in self._API_PREFIXES:
            if self._serve_static(parsed.path):
                return
            return self._json(404, {"error": "not found (web UI not built — see retinue-web)"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        adapter = self.server.adapter
        if parts == ["agents"]:
            return self._json(200, {"agents": adapter.list_agents()})
        if parts == ["models"]:
            return self._json(200, {"models": adapter.list_model_presets()})
        if parts == ["rooms"]:
            return self._json(200, {"rooms": [r.to_dict() for r in adapter.store.list_rooms()]})
        if len(parts) == 2 and parts[0] == "rooms":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            return self._json(200, room.to_dict())
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "transcript":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            query = parse_qs(parsed.query)
            since = int((query.get("since") or ["0"])[0] or 0)
            wait = min(float((query.get("wait") or ["0"])[0] or 0), 60.0)
            messages = adapter.store.wait_since(parts[1], since, timeout=wait)
            return self._json(200, {"messages": [m.to_dict() for m in messages]})
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "stream":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            query = parse_qs(parsed.query)
            since = int((query.get("since") or ["0"])[0] or 0)
            return self._sse_transcript(parts[1], since)
        return self._json(404, {"error": "not found"})

    def _sse_transcript(self, room_id: str, since: int) -> None:
        """Push transcript lines as ``event: messages`` until the client goes."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        adapter = self.server.adapter

        def emit(messages: List[Any], keepalive: bool = False) -> None:
            if messages:
                payload = json.dumps({"messages": [m.to_dict() for m in messages]})
                self.wfile.write(f"event: messages\ndata: {payload}\n\n".encode())
            elif keepalive:
                self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()

        try:
            # Catch-up first so EventSource sees history without a wait.
            caught = adapter.store.read_since(room_id, since)
            if caught:
                emit(caught)
                since = max(since, max(m.seq for m in caught))
            else:
                emit([], keepalive=True)
            while True:
                messages = adapter.store.wait_since(room_id, since, timeout=15.0)
                emit(messages, keepalive=not messages)
                if messages:
                    since = max(since, max(m.seq for m in messages))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except OSError:
            return

    def do_POST(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        adapter = self.server.adapter
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "invalid or oversized JSON body"})
        if parts == ["agents"]:
            try:
                payload = adapter.hire_agent(
                    name=str(body.get("name") or ""),
                    job=str(body.get("job") or ""),
                    how=str(body.get("how") or ""),
                    model=str(body.get("model") or "") or None,
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except FileExistsError as e:
                return self._json(409, {"error": f"an agent named '{e}' already exists"})
            return self._json(201, payload)
        if parts == ["rooms"]:
            try:
                payload = adapter.create_room(
                    name=str(body.get("name") or ""),
                    members=list(body.get("members") or []),
                    lead=body.get("lead"),
                    max_agent_turns=body.get("max_agent_turns"),
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(201, payload)
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
            try:
                result = adapter.post_user_message(
                    parts[1],
                    text=str(body.get("text") or ""),
                    from_name=str(body.get("from") or _DEFAULT_USER_NAME),
                )
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            return self._json(202, result)
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "rooms":
            if self.server.adapter.store.delete(parts[1]):
                return self._json(200, {"deleted": parts[1]})
            return self._json(404, {"error": "no such room"})
        return self._json(404, {"error": "not found"})
