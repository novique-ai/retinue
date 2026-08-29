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
import contextvars
import hmac
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
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

from . import attachments, auth, brokertoken, clarify as room_clarify, cron_workspace, cronjobs, crossroom, engine, governed, grokbuild, hidden_sessions, hire, ide, identity, itinerary, keepalive, principal, projects, routines, runtimes, sidebar, skilldraft, uimeta, voice, workspace, worktrees
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_TOOL, KIND_USER, Room, RoomMessage
from .store import RoomStore

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8643
_MAX_BODY = 262_144  # 256 KB is plenty for a chat message
_MAX_AUDIO = 8 * 1024 * 1024  # 8 MiB ≈ 4 min of 16 kHz mono WAV
_DEFAULT_USER_NAME = "User"


class AgentBusy(ValueError):
    """Raised when a model switch would evict a mid-turn agent."""


def _evict_room_environment(cache_key: str) -> None:
    from tools import terminal_tool

    terminal_tool._evict_environment_for_task(cache_key)


def _room_container_key(room: Optional[Room]) -> Optional[str]:
    """This room's container key, or ``None`` if it cannot be computed.

    Used to notice that an edit changed the room's container identity. A room
    whose ide_path no longer resolves already fails at container start with a
    real message; it must not also make an unrelated edit (a rename, an
    invite) raise from the eviction bookkeeping.
    """
    if room is None:
        return None
    try:
        return ide.container_key_for_room(room)
    except Exception:
        logger.debug("container key unavailable for room", exc_info=True)
        return None

# Parallel turns share one adapter.send(chat_id=room). The gateway's notify
# metadata does not echo event.metadata, and send() runs AFTER the runner
# leaves _profile_runtime_scope — so HERMES_HOME is the default home for
# every speaker. This ContextVar is set around handle_message in the same
# task that later calls send().
_turn_member: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "retinue_turn_member", default=None
)


def turn_timeout() -> float:
    """Cloud/default per-turn wait. Local-LLM members use the longer
    ``hire.turn_timeout_for`` budget instead."""
    return hire.cloud_turn_timeout()


def _member_from_scope() -> Optional[str]:
    """Profile name of the in-flight multiplex turn, if any.

    Final-reply ``send()`` metadata is the gateway's thread meta + notify;
    it does not echo our event metadata. The turn still runs inside
    ``_profile_runtime_scope``, so HERMES_HOME is ``.../profiles/<member>``.
    """
    try:
        from pathlib import Path

        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        if home.parent.name == "profiles":
            return home.name
        return "default"
    except Exception:
        return None


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
    session_key: str = ""
    source: Any = None
    # Grok Build turns (#218): Stop calls this instead of the Hermes
    # gateway interrupt — it sends session/cancel to the member's agent.
    grok_cancel: Any = None  # Optional[Callable[[], Awaitable[None]]]


class RetinueRoomsAdapter(BasePlatformAdapter):
    # A room transcript has no platform message-length cap, so cron
    # deliver=origin payloads must arrive whole. Without this the gateway
    # truncates them at MAX_PLATFORM_OUTPUT (4000, sized for Telegram) with a
    # host-path footer the web viewer cannot open (issue #201); the
    # gateway/delivery.py gate hands full payloads to adapters declaring this.
    splits_long_messages = True

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
        self._pending: Dict[tuple[str, str], _PendingTurn] = {}  # (room, member)
        self._pending_lock = threading.Lock()
        self._cycle_stops: Dict[str, threading.Event] = {}
        self._xai_keepalive: Optional[keepalive.XaiKeepalive] = None

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
        # Rooms are containerised by definition, so a docker-backed gateway is
        # a precondition, not something to arrange silently. The adapter used
        # to force TERMINAL_ENV=docker into process env for each cycle, which
        # also moved every other platform in this gateway onto the container
        # backend without asking. Refusing to start with a clear reason is the
        # honest version of that.
        backend_error = ide.docker_backend_error()
        if backend_error:
            logger.error("Retinue rooms: %s", backend_error)
            self._set_fatal_error("terminal_backend", backend_error, retryable=False)
            return False
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
            seeded = hire.ensure_bundled_cloud_presets(self._home_dir())
            if seeded:
                logger.info("Retinue rooms: seeded model presets %s", ", ".join(seeded))
        except Exception:
            logger.debug("Retinue rooms: preset seed at connect failed", exc_info=True)
        try:
            self._rescan_disk_profiles()
        except Exception:
            logger.debug("Retinue rooms: profile rescan at connect failed", exc_info=True)
        try:
            self._sweep_hidden_room_sessions()
        except Exception:
            logger.debug("Retinue rooms: hidden-session sweep failed", exc_info=True)
        try:
            # Back-fill ui_meta for retainers hired before the mirror existed
            # (#137). Idempotent: a second start rewrites nothing.
            synced = uimeta.sync_all(self._home_dir())
            if synced:
                logger.info(
                    "Retinue rooms: mirrored ui_meta for %s", ", ".join(synced)
                )
        except Exception:
            logger.debug("Retinue rooms: ui_meta sync at connect failed", exc_info=True)
        self._start_xai_keepalive()
        try:
            cron_workspace.install(self)
        except Exception:
            logger.debug("Retinue rooms: cron workspace wrap failed", exc_info=True)
        logger.info("Retinue rooms: serving on %s:%s", self.host, self.port)
        return True

    def _sweep_hidden_room_sessions(self) -> int:
        """Hide pre-existing room-owned sessions. Idempotent."""
        return hidden_sessions.sweep_home(self._home_dir())

    def _hide_room_session(self, session_key: str, member: str) -> None:
        """Mark this member's rooms session hidden if the row exists."""
        if not session_key:
            return
        try:
            hidden_sessions.hide_session_in_home(
                self._home_dir(), session_key, member=member
            )
        except Exception:
            logger.debug(
                "Retinue rooms: hide session %s/%s failed",
                member,
                session_key,
                exc_info=True,
            )

    def _start_xai_keepalive(self) -> None:
        """Warm the workspace xAI grant before JWT expiry while idle (#34)."""
        self._stop_xai_keepalive()
        interval = keepalive.interval_from_env()
        if interval is None:
            return
        try:
            self._xai_keepalive = keepalive.XaiKeepalive(
                self._home_dir, interval=interval
            )
            self._xai_keepalive.start()
        except Exception:
            logger.debug("Retinue rooms: xAI keepalive failed to start", exc_info=True)
            self._xai_keepalive = None

    def _stop_xai_keepalive(self) -> None:
        ka = self._xai_keepalive
        if ka is None:
            return
        try:
            ka.stop()
        except Exception:
            logger.debug("Retinue rooms: xAI keepalive stop failed", exc_info=True)
        self._xai_keepalive = None

    async def disconnect(self) -> None:
        cron_workspace.uninstall()
        self._stop_xai_keepalive()
        mgr = getattr(self, "_grok_mgr", None)
        if mgr is not None:
            try:
                await mgr.shutdown()
            except Exception:
                logger.debug("Retinue rooms: grok manager shutdown failed", exc_info=True)
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
        self._cycle_stops.clear()

    # ── reply capture (A2A pattern) ──────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Resolve a pending turn, or land a Hermes cron origin delivery.

        Only sends carrying the gateway's ``notify`` final-reply marker
        satisfy a mention-turn. Progress/preview sends stay silent.
        Cron ``deliver=origin`` calls this with ``job_id`` and no pending
        turn — those must append to the transcript (issue #36).
        """
        message_id = str(int(time.time() * 1000))
        meta = metadata or {}
        member = (
            meta.get("retinue_member")
            or meta.get("thread_id")
            or _turn_member.get()
            or _member_from_scope()
        )
        if meta.get("notify"):
            self._resolve_pending(chat_id, ok=True, text=content or "", member=member)
            return SendResult(success=True, message_id=message_id)
        if meta.get("job_id"):
            return self._append_unsolicited_agent(chat_id, content or "", member, message_id)
        return SendResult(success=True, message_id=message_id)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Post the yes/no on the transcript so Speak Replies can play it.

        Base ``send_clarify`` calls ``send()``, and rooms ``send()`` drops
        anything without ``notify``. The prompt never landed. We write it
        as the retainer and mark the entry awaiting text so the next
        room line resolves it.
        """
        if choices:
            try:
                from tools.clarify_gateway import mark_awaiting_text

                mark_awaiting_text(clarify_id)
            except Exception:
                logger.debug("Retinue rooms: mark_awaiting_text failed", exc_info=True)
        if self.store.get(chat_id) is None:
            return SendResult(success=False, message_id="", error="no such room")
        meta = metadata or {}
        member = (
            meta.get("retinue_member")
            or meta.get("thread_id")
            or _turn_member.get()
            or _member_from_scope()
            or room_clarify.member_from_session_key(session_key)
        )
        speaker = (member or "").strip() or "room"
        posted = self.store.append(
            chat_id,
            RoomMessage(
                seq=0,
                ts=0,
                kind=KIND_AGENT,
                speaker=speaker,
                text=room_clarify.format_prompt(question, choices),
            ),
        )
        self._note_posted(chat_id, posted)
        return SendResult(success=True, message_id=str(posted.seq))

    def _append_unsolicited_agent(
        self,
        room_id: str,
        text: str,
        member: Optional[str],
        message_id: str,
    ) -> SendResult:
        """Persist a cron/origin reply that is not a mention-turn capture."""
        if self.store.get(room_id) is None:
            return SendResult(success=False, message_id=message_id, error="no such room")
        body = (text or "").strip()
        if not body:
            return SendResult(success=True, message_id=message_id)
        speaker = (member or "").strip()
        if not speaker or speaker == "default":
            speaker = "cron"
        posted = self.store.append(
            room_id,
            RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=speaker, text=body),
        )
        self._note_posted(room_id, posted)
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
            self._resolve_pending(
                pending.room_id, ok=False, text="agent processing failed", member=pending.member
            )
        elif outcome == ProcessingOutcome.CANCELLED:
            self._resolve_pending(
                pending.room_id, ok=False, text="turn cancelled", member=pending.member
            )
        else:
            self._resolve_pending(
                pending.room_id, ok=False, text="agent returned no reply", member=pending.member
            )

    def _resolve_pending(
        self,
        room_id: str,
        *,
        ok: bool,
        text: str,
        member: Optional[str] = None,
    ) -> None:
        key = (room_id, member or "")
        with self._pending_lock:
            pending = self._pending.pop(key, None)
            if pending is None and not member:
                # Legacy/single-pending fallback: one turn in this room.
                for candidate_key, candidate in list(self._pending.items()):
                    if candidate_key[0] == room_id:
                        pending = self._pending.pop(candidate_key)
                        break
        if pending is None:
            logger.debug("Retinue rooms: send for room %s/%s had no pending turn", room_id, member)
            return
        if not pending.future.done():
            pending.future.set_result((ok, text))
        if ok and text:
            self._capture_lead_itinerary(room_id, member or pending.member, text)

    def _capture_lead_itinerary(self, room_id: str, member: Optional[str], text: str) -> None:
        """The lead writes the outline; the pane is the user's view of it."""
        room = self.store.get(room_id)
        if room is None or not member:
            return
        lead = room.lead or room.default_responder()
        if member != lead:
            return
        parsed = itinerary.parse_fence(text)
        if not parsed:
            return
        try:
            itinerary.save(
                self._home_dir(), room_id, parsed, updated_by=member
            )
        except Exception:
            logger.debug("Retinue rooms: lead itinerary save failed", exc_info=True)

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
        workspace: Optional[str] = None,
        ide_path: Optional[str] = None,
        shared_mode: Optional[str] = None,
        project_id: Optional[str] = None,
        max_followup_rounds: Optional[int] = None,
        worktree_repos: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        members = [m.strip() for m in members if m and m.strip()]
        if not members:
            raise ValueError("a room needs at least one agent member")
        project_id = (project_id or "").strip() or None
        if project_id and not self._project_exists(project_id):
            raise ValueError(f"no such project: {project_id}")
        room = Room(
            id=engine.new_room_id(name),
            name=name.strip() or "room",
            members=members,
            lead=(lead or "").strip() or None,
            max_agent_turns=max(1, int(max_agent_turns or engine.DEFAULT_MAX_AGENT_TURNS)),
            max_followup_rounds=(
                engine.DEFAULT_MAX_FOLLOWUP_ROUNDS
                if max_followup_rounds is None
                else max(0, int(max_followup_rounds))
            ),
            project_id=project_id,
        )
        ide.apply_workspace_fields(
            room,
            workspace=workspace,
            ide_path=ide_path,
            touching_path=True,
            worktree_repos=worktree_repos,
            touching_worktrees=True,
        )
        room.shared_mode = ide.parse_shared_mode(shared_mode)
        self.store.create(room)
        unknown = [m for m in members if not self._profile_exists(m)]
        payload = room.to_dict()
        if unknown:
            payload["warning"] = (
                "unknown profiles (create them before they can speak): " + ", ".join(unknown)
            )
        return payload

    def patch_room(self, room_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Rename, restaff, archive, or change the lead. Transcript stays."""
        room = self.store.get(room_id)
        if room is None:
            raise KeyError(room_id)
        touched = False
        # Computed BEFORE any mutation. The roster is part of the container's
        # identity now (each member's skills are mounted, #188), and the
        # members edit lands further down this method — reading the key after
        # it would compare the new room against itself and evict nothing.
        overlay_touched = any(
            key in body
            for key in ("workspace", "ide_path", "shared_mode", "worktree_repos", "members")
        )
        overlay_key_before: Optional[str] = (
            _room_container_key(room) if overlay_touched else None
        )
        overlay_key_after: Optional[str] = None
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("room name is required")
            room.name = name
            touched = True
        # Members added through the full-array restaff are joining a live room
        # exactly as much as one added through POST /rooms/{id}/members, so they
        # get the same treatment: a system notice and a seeded cursor. Skipping
        # it here would leave the whole-transcript-on-first-turn bug alive
        # behind the other door — the Edit Room panel is the door most people
        # actually use.
        joined: List[str] = []
        departed: List[str] = []
        if "members" in body:
            members = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
            if not members:
                raise ValueError("a room needs at least one agent member")
            joined = [m for m in members if m not in room.members]
            departed = [m for m in room.members if m not in members]
            room.members = members
            if room.lead and room.lead not in room.members:
                room.lead = room.members[0]
            touched = True
        if "lead" in body:
            lead_raw = body.get("lead")
            lead = (str(lead_raw).strip() if lead_raw is not None else "") or None
            if lead and lead not in room.members:
                raise ValueError(f"lead {lead!r} is not a member")
            room.lead = lead
            touched = True
        if "archived" in body:
            room.archived = bool(body.get("archived"))
            touched = True
        if "max_agent_turns" in body and body.get("max_agent_turns") is not None:
            room.max_agent_turns = max(1, int(body.get("max_agent_turns")))
            touched = True
        if "max_followup_rounds" in body and body.get("max_followup_rounds") is not None:
            room.max_followup_rounds = max(0, int(body.get("max_followup_rounds")))
            touched = True
        if "workspace" in body or "ide_path" in body or "worktree_repos" in body:
            ide.apply_workspace_fields(
                room,
                workspace=body["workspace"] if "workspace" in body else room.workspace,
                ide_path=body.get("ide_path") if "ide_path" in body else room.ide_path,
                touching_path="ide_path" in body,
                worktree_repos=(
                    body.get("worktree_repos")
                    if "worktree_repos" in body
                    else room.worktree_repos
                ),
                touching_worktrees="worktree_repos" in body,
            )
            touched = True
        if "shared_mode" in body:
            room.shared_mode = ide.parse_shared_mode(body.get("shared_mode"))
            touched = True
        if "project_id" in body:
            project_id_raw = body.get("project_id")
            new_project_id = (str(project_id_raw).strip() if project_id_raw is not None else "") or None
            if new_project_id and not self._project_exists(new_project_id):
                raise ValueError(f"no such project: {new_project_id}")
            room.project_id = new_project_id
            touched = True
        if not touched:
            raise ValueError("nothing to update")
        if overlay_touched:
            overlay_key_after = _room_container_key(room)
        self.store.update(room)
        if overlay_key_before and overlay_key_after and overlay_key_before != overlay_key_after:
            _evict_room_environment(overlay_key_before)
        for member in departed:
            self._post_system(room_id, engine.member_excused_notice(member))
        for member in joined:
            posted = self._post_system(room_id, engine.member_joined_notice(member))
            head = posted.seq if posted is not None else 0
            room = self.store.mutate(
                room_id, lambda r, m=member, h=head: engine.seed_invite_last_seen(r, m, h)
            )
        return self._room_payload(room)

    def add_room_member(self, room_id: str, member: str) -> Dict[str, Any]:
        """Invite one agent into a live room (incremental; does not restaff).

        Seeds last_seen only when the member has no existing entry, so a
        re-invite resumes where they left off. The join notice is posted
        first; the cursor is then set from that seq so the newcomer sees
        the last INVITE_TRANSCRIPT_WINDOW messages (including the notice).
        """
        member = (member or "").strip()
        if not member:
            raise ValueError("member is required")

        def add(room: Room) -> None:
            if member in room.members:
                raise ValueError(f"{member} is already a member")
            room.members.append(member)

        # The newcomer's skills can only be mounted by a container created
        # after they joined (#188), so the roster is part of the container
        # key: the room re-keys here and the next cycle builds a container
        # that has their dir. Dispose the old one instead of leaking it.
        key_before = _room_container_key(self.store.get(room_id))
        self.store.mutate(room_id, add)
        self._evict_on_rekey(room_id, key_before)
        posted = self._post_system(room_id, engine.member_joined_notice(member))
        head = posted.seq if posted is not None else 0
        room = self.store.mutate(
            room_id, lambda r: engine.seed_invite_last_seen(r, member, head)
        )
        return self._room_payload(room)

    def remove_room_member(self, room_id: str, member: str) -> Dict[str, Any]:
        """Excuse one agent from a live room. last_seen survives."""
        member = (member or "").strip()
        if not member:
            raise ValueError("member is required")

        def drop(room: Room) -> None:
            if member not in room.members:
                raise ValueError(f"{member} is not a member")
            if len(room.members) == 1:
                raise ValueError("a room needs at least one agent member")
            room.members = [m for m in room.members if m != member]
            if room.lead == member:
                room.lead = room.members[0]
            # last_seen is left in place: a later re-invite resumes
            # at this cursor instead of replaying the room.

        key_before = _room_container_key(self.store.get(room_id))
        room = self.store.mutate(room_id, drop)
        # A departing member's skills stop being mounted for the same reason
        # a joining member's start (#188).
        self._evict_on_rekey(room_id, key_before)
        self._post_system(room_id, engine.member_excused_notice(member))
        return self._room_payload(room)

    def _evict_on_rekey(self, room_id: str, key_before: Optional[str]) -> None:
        """Dispose the room's cached container when its identity changed."""
        if not key_before:
            return
        key_after = _room_container_key(self.store.get(room_id))
        if key_after and key_after != key_before:
            _evict_room_environment(key_before)

    def _room_payload(self, room: Room) -> Dict[str, Any]:
        unknown = [m for m in room.members if not self._profile_exists(m)]
        payload = room.to_dict()
        if unknown:
            payload["warning"] = (
                "unknown profiles (create them before they can speak): " + ", ".join(unknown)
            )
        return payload

    def list_rooms_public(self) -> List[Dict[str, Any]]:
        rooms = self.store.list_rooms()
        layout = self._sidebar_resolved(rooms, hire.list_agents(self._home_dir()))
        order = {rid: i for i, rid in enumerate(layout["rooms"])}
        rooms.sort(key=lambda r: (order.get(r.id, 10_000), r.created_at, r.name))
        return [r.to_dict() for r in rooms]

    def _sidebar_resolved(
        self, rooms: Optional[List[Room]] = None, agents: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        if rooms is None:
            rooms = self.store.list_rooms()
        if agents is None:
            agents = hire.list_agents(self._home_dir())
        return sidebar.load_resolved(
            self._home_dir(),
            [r.id for r in rooms],
            [str(a.get("slug") or "") for a in agents],
        )

    def get_sidebar(self) -> Dict[str, Any]:
        return self._sidebar_resolved()

    def put_sidebar(self, body: Dict[str, Any]) -> Dict[str, Any]:
        sidebar.save(self._home_dir(), body)
        return self._sidebar_resolved()

    # ── projects ──────────────────────────────────────────────────────────

    def list_projects(self) -> List[Dict[str, Any]]:
        return projects.list_projects(self._home_dir())

    def projects_payload(self) -> Dict[str, Any]:
        """``{projects, order}`` — list is already in ``order``."""
        return projects.load(self._home_dir())

    def put_projects(self, body: Dict[str, Any]) -> Dict[str, Any]:
        order = body.get("order")
        if not isinstance(order, list):
            raise ValueError("order must be a list of project ids")
        return projects.reorder(self._home_dir(), order)

    def _project_exists(self, project_id: str) -> bool:
        return any(p["id"] == project_id for p in projects.list_projects(self._home_dir()))

    def create_project(self, name: str) -> Dict[str, Any]:
        return projects.create_project(self._home_dir(), name)

    def patch_project(self, project_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return projects.patch_project(self._home_dir(), project_id, body)

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        """Remove a project and unfile any of its rooms. Transcripts stay."""
        if not projects.delete_project(self._home_dir(), project_id):
            raise KeyError(project_id)
        unfiled = []
        for room in self.store.list_rooms():
            if room.project_id == project_id:
                self.store.mutate(room.id, lambda r: setattr(r, "project_id", None))
                unfiled.append(room.id)
        return {"deleted": project_id, "unfiled_rooms": unfiled}

    @staticmethod
    def _home_dir() -> str:
        return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")

    @classmethod
    def _profile_exists(cls, member: str) -> bool:
        if member == "default":
            return True
        return os.path.isdir(os.path.join(cls._home_dir(), "profiles", member))

    def _display_names(self, room: Room) -> Dict[str, str]:
        """slug → hire display name for members of *room* (slug if unknown)."""
        names: Dict[str, str] = {m: m for m in room.members}
        for agent in hire.list_agents(self._home_dir()):
            slug = str(agent.get("slug") or "")
            if slug in names:
                names[slug] = str(agent.get("display_name") or slug)
        return names

    def _member_jobs(self, room: Room) -> Dict[str, str]:
        """slug → one-line job title for members of *room* (absent if unset)."""
        jobs: Dict[str, str] = {}
        for agent in hire.list_agents(self._home_dir()):
            slug = str(agent.get("slug") or "")
            if slug in room.members:
                job = str(agent.get("job") or "").strip()
                if job:
                    jobs[slug] = job
        return jobs

    # ── agents (the hire flow) ───────────────────────────────────────────

    def list_agents(self) -> List[Dict[str, Any]]:
        agents = hire.list_agents(self._home_dir())
        layout = self._sidebar_resolved(self.store.list_rooms(), agents)
        team_of = sidebar.team_for_agents(layout["items"])
        order = {
            item["slug"]: i
            for i, item in enumerate(layout["items"])
            if item.get("kind") == "agent"
        }
        busy_rooms = self.busy_rooms_by_slug()
        for agent in agents:
            slug = str(agent.get("slug") or "")
            agent["team"] = team_of.get(slug)
            # ``busy`` is gateway-global — true while this profile has a turn
            # in ANY room. Keep it: the model-switch guard needs exactly that.
            # ``busy_rooms`` is what a room view must read, or an agent working
            # in room A renders as thinking in every other room he is a member
            # of, which looks like a hung turn and invites a needless Stop.
            agent["busy_rooms"] = busy_rooms.get(slug, [])
            agent["busy"] = bool(agent["busy_rooms"])
        auth.annotate_agents(self._home_dir(), agents)
        agents.sort(key=lambda a: (order.get(str(a.get("slug") or ""), 10_000), str(a.get("slug") or "")))
        return agents

    def busy_slugs(self) -> set:
        """Profile names that currently have an in-flight room turn, any room."""
        with self._pending_lock:
            return {member for (_room, member) in self._pending}

    def busy_rooms_by_slug(self) -> Dict[str, List[str]]:
        """Room ids each profile currently has an in-flight turn in.

        Same ``_pending`` set as :meth:`busy_slugs`, without projecting the room
        away. A profile can hold turns in several rooms at once, so the value is
        a list; a profile with no in-flight turn is absent rather than mapped to
        an empty list.
        """
        with self._pending_lock:
            pairs = sorted(self._pending)
        by_slug: Dict[str, List[str]] = {}
        for room_id, member in pairs:
            by_slug.setdefault(member, []).append(room_id)
        return by_slug

    def list_model_presets(self) -> List[Dict[str, Any]]:
        try:
            hire.ensure_bundled_cloud_presets(self._home_dir())
        except Exception:
            logger.debug("Retinue rooms: preset seed on list failed", exc_info=True)
        return hire.list_model_presets(self._home_dir())

    def switch_agent_model(self, slug: str, model: str) -> Dict[str, Any]:
        """Point an already-hired profile at another workspace preset.

        Rewrites only the ``model:`` block on disk and evicts cached
        AIAgents so the next room turn loads the new model. No gateway
        restart, no hand-edit of ``profiles/<slug>/config.yaml``.
        """
        return self.patch_agent(slug, {"model": model})

    def patch_agent(self, slug: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Edit persona / archive / switch model. Same slug, no restart."""
        slug = (slug or "").strip()
        model = str(body.get("model") or "").strip()
        persona: Dict[str, Any] = {}
        if "name" in body or "display_name" in body:
            persona["display_name"] = str(body.get("name") or body.get("display_name") or "")
        if "job" in body:
            persona["job"] = str(body.get("job") or "")
        if "how" in body:
            persona["how"] = str(body.get("how") or "")
        if "archived" in body:
            persona["archived"] = bool(body.get("archived"))
        if "avatar_emoji" in body:
            persona["avatar_emoji"] = body.get("avatar_emoji")
        if "avatar_color" in body:
            persona["avatar_color"] = body.get("avatar_color")
        if "voice" in body:
            persona["voice"] = body.get("voice")
        if "persona" in body:
            persona["persona"] = body.get("persona")
        if "governed" in body:
            persona["governed"] = bool(body.get("governed"))
        if not model and not persona:
            raise ValueError("nothing to update")
        if persona:
            hire.update_agent(self._home_dir(), slug, **persona)
        evicted = 0
        if model:
            if slug in self.busy_slugs():
                raise AgentBusy(
                    f"{slug} is mid-turn — wait until they finish before switching models"
                )
            try:
                hire.ensure_bundled_cloud_presets(self._home_dir())
            except Exception:
                logger.debug("Retinue rooms: preset seed on switch failed", exc_info=True)
            meta = hire.apply_model_preset(self._home_dir(), slug, model)
            evicted = hire.evict_profile_agent_cache(self._live_runner(), slug)
        else:
            evicted = hire.evict_profile_agent_cache(self._live_runner(), slug)
            meta = next(
                (a for a in hire.list_agents(self._home_dir()) if a.get("slug") == slug),
                None,
            )
            if meta is None:
                raise KeyError(slug)
        meta["cache_evicted"] = evicted
        layout = self._sidebar_resolved()
        meta["team"] = sidebar.team_for_agents(layout["items"]).get(slug)
        return meta

    def delete_agent(self, slug: str) -> Dict[str, Any]:
        """Remove ``profiles/<slug>/`` and evict the live registration."""
        removed = hire.delete_agent(self._home_dir(), slug)
        dropped = hire.deactivate_hired_profile(removed, runner=self._live_runner())
        # Drop the slug from the persisted order so it does not come back
        # as a ghost if a later hire reuses the name.
        layout = sidebar.load(self._home_dir())
        layout["items"] = [
            item
            for item in layout.get("items") or []
            if not (item.get("kind") == "agent" and item.get("slug") == removed)
        ]
        sidebar.save(self._home_dir(), layout)
        return {"deleted": removed, **dropped}

    def start_provider_reauth(self, provider: str) -> Dict[str, Any]:
        """Begin Hermes device-code login against this workspace HERMES_HOME."""

        def _after(done_provider: str) -> None:
            evicted = auth.finish_reauth_success(
                self._home_dir(), done_provider, runner=self._live_runner()
            )
            with auth._sessions_lock:
                for sess in auth._sessions.values():
                    if (
                        sess.get("provider") == auth.normalize_provider(done_provider)
                        and sess.get("status") == "approved"
                    ):
                        sess["evicted"] = evicted

        return auth.start_reauth(provider, on_success=_after)

    def hire_agent(
        self,
        name: str,
        job: str,
        how: str,
        model: Optional[str] = None,
        avatar_emoji: Any = None,
        avatar_color: Any = None,
        voice: Any = None,
        persona: Any = None,
        runtime: Any = None,
    ) -> Dict[str, Any]:
        try:
            hire.ensure_bundled_cloud_presets(self._home_dir())
        except Exception:
            logger.debug("Retinue rooms: preset seed on hire failed", exc_info=True)
        runtime_id = runtimes.validate_runtime(runtime)
        if runtime_id == runtimes.RUNTIME_GROK_BUILD:
            state = grokbuild.health(self._home_dir())
            if state.get("status") != "available":
                raise ValueError(
                    f"Grok Build runtime is not usable here — "
                    f"{state.get('status')}: {state.get('detail') or ''}".strip()
                )
        meta = hire.scaffold_profile(
            self._home_dir(),
            name,
            job,
            how,
            model_preset=model,
            avatar_emoji=avatar_emoji,
            avatar_color=avatar_color,
            voice=voice,
            persona=persona,
            runtime=runtime_id,
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
        """Locate the built rooms web UI (``retinue-web/dist``).

        Checked in order, first existing directory wins:

          1. ``RETINUE_ROOMS_WEB_DIST`` env override — an explicit path to
             any built ``dist/`` directory. Takes precedence over
             everything else, including a source-tree build, so a
             contributor or packager can point at an alternate build
             without moving files.
          2. The source tree, resolved relative to this file
             (``plugins/platforms/retinue_rooms/adapter.py`` -> repo root
             is 3 up -> ``retinue-web/dist``). This is what a git checkout
             with ``npm run build`` already run gets for free.
          3. A well-known XDG data prefix:
             ``$XDG_DATA_HOME/retinue/web-dist`` (default
             ``~/.local/share/retinue/web-dist`` when ``XDG_DATA_HOME`` is
             unset, matching the fallback ``hermes_cli/linux_desktop_entry.py``
             already uses for installed assets). Nothing populates this
             today — a pip-only install has no source tree to resolve step
             2 against — but it gives a future packaging step (contributor
             issue #9; no new packaging format is introduced here) a
             documented drop location without touching this function's
             callers.

        Returns ``None`` when none of the three exist; ``_serve_static``
        then serves a help page instead of the SPA.
        """
        override = (os.getenv("RETINUE_ROOMS_WEB_DIST") or "").strip()
        if override and os.path.isdir(override):
            return override

        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        source_tree = os.path.join(repo_root, "retinue-web", "dist")
        if os.path.isdir(source_tree):
            return source_tree

        xdg_data_home = (os.getenv("XDG_DATA_HOME") or "").strip()
        if not xdg_data_home:
            xdg_data_home = os.path.join(os.path.expanduser("~"), ".local", "share")
        well_known = os.path.join(xdg_data_home, "retinue", "web-dist")
        if os.path.isdir(well_known):
            return well_known

        return None

    def post_user_message(
        self, room_id: str, text: str, from_name: str, wait: bool = False
    ) -> Dict[str, Any]:
        room = self.store.get(room_id)
        if room is None:
            raise KeyError(room_id)
        if not text.strip():
            raise ValueError("empty message")
        if self._loop is None:
            raise RuntimeError("gateway loop not ready")
        speaker = principal.speaker_name(self._home_dir(), from_name)
        message = self.store.append(
            room_id, RoomMessage(seq=0, ts=0, kind=KIND_USER, speaker=speaker, text=text)
        )
        self._note_posted(room_id, message)
        if room_clarify.try_resolve(room, text):
            # Julio asked a yes/no. This line is the answer — do not
            # start a second cycle on top of the one still waiting.
            return {"seq": message.seq, "planned": [], "clarify": True}
        planned = engine.plan_user_turns(room, text, self._display_names(room))
        fut = asyncio.run_coroutine_threadsafe(self._run_cycle(room_id, message), self._loop)
        if wait:
            home = self._home_dir()
            sample = planned or room.members
            per = (
                hire.local_turn_timeout()
                if any(hire.profile_uses_local_llm(home, m) for m in sample)
                else hire.cloud_turn_timeout()
            )
            fut.result(timeout=per * max(1, room.max_agent_turns) + 30)
        return {"seq": message.seq, "planned": planned}

    def post_user_audio(
        self,
        room_id: str,
        data: bytes,
        *,
        filename: str = "speech.wav",
        from_name: str = _DEFAULT_USER_NAME,
        draft: str = "",
    ) -> Dict[str, Any]:
        """STT then the normal user-message cycle. Transcript is the room line.

        ``draft`` is the composer prefix (usually an ``@Name``). It is
        joined onto the transcript so mention routing sees the same
        text the user typed, then spoke. A leading spoken vocative
        (``at Claude``) is rewritten to ``@Claude`` when the line has
        no live @mention yet.
        """
        spoken = voice.transcribe_dispatch(data, filename)
        text = engine.join_draft_and_speech(draft, spoken)
        room = self.store.get(room_id)
        if room is not None:
            text = engine.rewrite_spoken_address(
                text, room.members, self._display_names(room)
            )
        result = self.post_user_message(room_id, text, from_name)
        result["text"] = text
        return result

    def _stop_event(self, room_id: str) -> threading.Event:
        ev = self._cycle_stops.get(room_id)
        if ev is None:
            ev = self._cycle_stops[room_id] = threading.Event()
        return ev

    def _cycle_active(self, room_id: str) -> bool:
        lock = self._room_locks.get(room_id)
        if lock is not None and lock.locked():
            return True
        with self._pending_lock:
            return any(rid == room_id for rid, _member in self._pending)

    def _session_key_for(self, source: Any) -> str:
        try:
            from gateway.session import build_session_key

            store = getattr(self, "_session_store", None)
            profile = None
            if store is not None:
                try:
                    profile = store._resolve_profile_for_key(source)
                except Exception:
                    profile = getattr(source, "profile", None)
            return build_session_key(
                source,
                group_sessions_per_user=bool(
                    self.config.extra.get("group_sessions_per_user", True)
                ),
                thread_sessions_per_user=bool(
                    self.config.extra.get("thread_sessions_per_user", False)
                ),
                profile=profile,
            )
        except Exception:
            return ""

    def stop_cycle(self, room_id: str, from_name: str = _DEFAULT_USER_NAME) -> Dict[str, Any]:
        """Abort this room's in-flight cycle. Other rooms are untouched.

        Idle stop is a no-op (the web client still cuts leftover TTS).
        """
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        if not self._cycle_active(room_id):
            return {"stopped": False, "idle": True}
        if self._loop is None:
            raise RuntimeError("gateway loop not ready")
        fut = asyncio.run_coroutine_threadsafe(
            self._stop_cycle(room_id, from_name), self._loop
        )
        return fut.result(timeout=15)

    async def _stop_cycle(self, room_id: str, from_name: str) -> Dict[str, Any]:
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        already = self._stop_event(room_id).is_set()
        active = self._cycle_active(room_id)
        if not active and already:
            return {"stopped": True, "already": True}
        if not active:
            return {"stopped": False, "idle": True}
        self._stop_event(room_id).set()
        stored = self.store.get(room_id)
        if stored is not None:
            room_clarify.release_room(stored)
        with self._pending_lock:
            pending_here = [
                pending
                for (rid, _member), pending in list(self._pending.items())
                if rid == room_id
            ]
        for pending in pending_here:
            try:
                await self._interrupt_turn(pending)
            except Exception:
                logger.debug(
                    "Retinue rooms: interrupt of %s/%s failed",
                    room_id,
                    pending.member,
                    exc_info=True,
                )
            self._resolve_pending(
                room_id, ok=False, text="stopped", member=pending.member
            )
        notice_text = engine.cycle_stopped_notice(
            principal.speaker_name(self._home_dir(), from_name)
        )
        notice = None if already else self._post_system(room_id, notice_text)
        return {
            "stopped": True,
            "already": already,
            "seq": notice.seq if notice else None,
            "notice": notice_text,
        }

    async def _interrupt_turn(self, pending: _PendingTurn) -> None:
        """Cancel the in-flight model call for one pending room turn."""
        if pending.grok_cancel is not None:
            # Grok Build turn: cancellation is the runtime's own
            # session/cancel; there is no Hermes session to interrupt.
            await pending.grok_cancel()
            return
        key = pending.session_key
        runner = self._live_runner()
        interrupt_fn = getattr(runner, "_interrupt_and_clear_session", None) if runner else None
        if callable(interrupt_fn) and key and pending.source is not None:
            try:
                from gateway.run import _INTERRUPT_REASON_STOP

                await interrupt_fn(
                    key,
                    pending.source,
                    interrupt_reason=_INTERRUPT_REASON_STOP,
                    invalidation_reason="retinue_room_stop",
                )
                await self._rotate_member_session(key)
                return
            except Exception:
                logger.debug(
                    "Retinue rooms: gateway session interrupt failed", exc_info=True
                )
        if key:
            try:
                await self.interrupt_session_activity(key, pending.room_id)
            except Exception:
                logger.debug(
                    "Retinue rooms: adapter session interrupt failed", exc_info=True
                )
            pending_map = getattr(self, "_pending_messages", None)
            if isinstance(pending_map, dict):
                pending_map.pop(key, None)
        if runner is None or not key:
            return
        try:
            from agent.interrupt_compat import request_hard_interrupt
            from gateway.run import _AGENT_PENDING_SENTINEL

            agent = (getattr(runner, "_running_agents", None) or {}).get(key)
            if agent and agent is not _AGENT_PENDING_SENTINEL:
                request_hard_interrupt(agent, "Stopped.")
        except Exception:
            logger.debug("Retinue rooms: hard interrupt failed", exc_info=True)
        await self._rotate_member_session(key)

    async def _rotate_member_session(self, session_key: str) -> bool:
        """Drop a member's Hermes session history by rotating the session id.

        The room store owns everything durable — transcript, watermark — and
        the briefing is re-sent on every turn, so the Hermes session's only
        cross-turn cargo is tool history. After a Stop that cargo is a
        poisoned prompt the next turn re-pays in full (a 45-60k grep dump
        survived Stop, PATCH-evict, and a "clean context" re-prompt on
        2026-08-20 — #164). Rotation makes the next turn rebuild from the
        room transcript instead. Never raises: Stop must succeed even when
        the session store cannot rotate.
        """
        if not session_key:
            return False
        runner = self._live_runner()
        store = getattr(runner, "async_session_store", None) if runner else None
        reset = getattr(store, "reset_session", None)
        if not callable(reset):
            return False
        try:
            entry = await reset(session_key)
        except Exception:
            logger.debug("Retinue rooms: session rotation failed", exc_info=True)
            return False
        # The evicted cache slot would otherwise rebuild from the OLD
        # session's history (see gateway _interrupt_and_clear_session);
        # after rotation the rebuild source is the fresh, empty session.
        evict = getattr(runner, "_evict_cached_agent", None)
        if callable(evict):
            try:
                evict(session_key)
            except Exception:
                logger.debug("Retinue rooms: post-rotation evict failed", exc_info=True)
        logger.info("Retinue rooms: rotated member session %s (#164)", session_key)
        return entry is not None

    def reset_member_session(self, room_id: str, member: str) -> Dict[str, Any]:
        """Explicit "new session" lever for one room member (#164).

        Idle-only: with a turn in flight, Stop is the right lever (it rotates
        on interrupt); rotating under a running turn would race its writes.
        """
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        if self._loop is None:
            raise RuntimeError("gateway loop not ready")
        fut = asyncio.run_coroutine_threadsafe(
            self._reset_member_session(room_id, member), self._loop
        )
        return fut.result(timeout=15)

    async def _reset_member_session(self, room_id: str, member: str) -> Dict[str, Any]:
        room = self.store.get(room_id)
        if room is None:
            raise KeyError(room_id)
        member = (member or "").strip()
        if member not in room.members:
            raise ValueError(f"no member '{member}' in this room")
        if self._cycle_active(room_id):
            raise AgentBusy(
                f"{member} may be mid-turn — Stop the cycle first; Stop already "
                "starts them a fresh session"
            )
        # Derive the same session key an _agent_turn for this member builds.
        # user_id varies per trigger there, so the key cannot depend on it.
        source = self.build_source(
            chat_id=room.id,
            chat_name=f"room:{room.name}",
            chat_type="group",
            user_id="user:reset",
            user_name=_DEFAULT_USER_NAME,
            thread_id=member,
        )
        source.profile = None if member == "default" else member
        if (
            runtimes.runtime_for_member(self._home_dir(), member)
            == runtimes.RUNTIME_GROK_BUILD
        ):
            # Native runtime: drop the grok session + its persisted id;
            # the next turn re-briefs a genuinely new conversation.
            await self._grok_manager().reset(room_id, member)
            return {"reset": True, "member": member, "room": room_id}
        key = self._session_key_for(source)
        ok = await self._rotate_member_session(key)
        return {"reset": bool(ok), "member": member, "room": room_id}

    def save_routine_from_room(
        self,
        name: str,
        room_id: str,
        since: int = 0,
        until: Optional[int] = None,
        *,
        owner: Optional[str] = None,
        schedule: Optional[str] = None,
        room_for_schedule: Optional[str] = None,
    ) -> Dict[str, Any]:
        room = self.store.get(room_id)
        if room is None:
            raise KeyError(room_id)
        messages = self.store.read_since(room_id, 0)
        prompts = routines.user_prompts_from_messages(
            messages, since=since, until=until
        )
        if not prompts:
            raise ValueError("a routine needs at least one user prompt")
        home = self._home_dir()
        owners = cronjobs.served_owners(home)
        selected_owner = str(owner or "").strip()
        if not selected_owner:
            if room.lead and room.lead in owners:
                selected_owner = room.lead
            else:
                selected_owner = next((member for member in room.members if member in owners), "")
        if not selected_owner:
            raise cronjobs.UnknownOwner("")
        cronjobs.owner_home(home, selected_owner)

        slug = routines.slugify(name)
        if not slug:
            raise ValueError(f"cannot derive a routine id from {name!r}")
        draft_dir = skilldraft.skill_dir(home, selected_owner, slug)
        if routines.get_routine(home, slug) is not None or os.path.exists(draft_dir):
            raise FileExistsError(slug)
        expected_output = ""
        for message in messages:
            if message.kind != KIND_AGENT or message.seq <= since:
                continue
            if until is not None and message.seq > until:
                continue
            expected_output = (message.text or "").strip()[:800]

        record = routines.save_routine(
            home,
            name,
            prompts,
            source_room=room_id,
            owner=selected_owner,
            skill=slug,
            expected_output=expected_output,
        )
        try:
            skilldraft.write_skill_draft(
                home,
                selected_owner,
                slug=slug,
                name=name,
                steps=prompts,
                expected_output=expected_output,
                source_room=room_id,
            )
        except Exception:
            routines.delete_routine(home, slug)
            raise

        if schedule:
            try:
                job = cronjobs.create_job(
                    home,
                    selected_owner,
                    name=name,
                    schedule=schedule,
                    room=room_for_schedule or room_id,
                    skill=slug,
                    kind="routine",
                    routine_slug=slug,
                    room_name=room.name,
                    rooms=self._cron_rooms(),
                )
            except Exception:
                shutil.rmtree(draft_dir, ignore_errors=True)
                routines.delete_routine(home, slug)
                raise
            try:
                record = routines.update_routine(home, slug, {"job_id": job["id"]})
            except Exception:
                logger.warning(
                    "Routine %s was scheduled as job %s but its link could not be saved",
                    slug,
                    job["id"],
                    exc_info=True,
                )
            return {**record, "job": job}
        return record

    def run_routine(self, slug: str, room_id: str) -> Dict[str, Any]:
        meta = routines.get_routine(self._home_dir(), slug)
        if meta is None:
            raise KeyError(slug)
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        if not meta.get("messages"):
            raise ValueError(
                f"routine {slug!r} has no replay prompts; use its linked skill or cron job"
            )
        speaker = f"routine:{slug}"
        ran = []
        for prompt in meta.get("messages") or []:
            ran.append(
                self.post_user_message(room_id, prompt, speaker, wait=True)
            )
        return {"slug": slug, "room": room_id, "steps": ran}

    def _cron_rooms(self) -> Dict[str, str]:
        return {room.id: room.name for room in self.store.list_rooms()}

    def list_cron_jobs(
        self, owner: Optional[str] = None, room: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return cronjobs.list_jobs(
            self._home_dir(), owner=owner, room=room, rooms=self._cron_rooms()
        )

    def list_room_cron_jobs(self, room_id: str) -> List[Dict[str, Any]]:
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        return self.list_cron_jobs(room=room_id)

    def create_cron_job(self, body: Dict[str, Any]) -> Dict[str, Any]:
        room_id = str(body.get("room") or "")
        room = self.store.get(room_id)
        if not room_id:
            raise ValueError("a scheduled job needs a destination room")
        if room is None:
            raise KeyError(room_id)
        return cronjobs.create_job(
            self._home_dir(),
            str(body.get("owner") or ""),
            name=str(body.get("name") or ""),
            schedule=str(body.get("schedule") or ""),
            room=room_id,
            prompt=str(body.get("prompt") or ""),
            skill=str(body.get("skill") or ""),
            kind=str(body.get("kind") or "reminder"),
            routine_slug=str(body.get("routine_slug") or "") or None,
            room_name=room.name,
            rooms=self._cron_rooms(),
        )

    def patch_cron_job(self, job_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if "room" in body:
            room_id = body.get("room")
            if not isinstance(room_id, str) or not room_id:
                raise ValueError("room must be a room id")
            if self.store.get(room_id) is None:
                raise KeyError(room_id)
        return cronjobs.patch_job(
            self._home_dir(), job_id, body, rooms=self._cron_rooms()
        )

    def set_cron_job_enabled(self, job_id: str, enabled: bool) -> Dict[str, Any]:
        operation = cronjobs.resume_job if enabled else cronjobs.pause_job
        return operation(self._home_dir(), job_id, rooms=self._cron_rooms())

    def run_cron_job(self, job_id: str) -> Dict[str, Any]:
        return cronjobs.run_job(self._home_dir(), job_id, rooms=self._cron_rooms())

    def delete_cron_job(self, job_id: str) -> Dict[str, Any]:
        return cronjobs.delete_job(self._home_dir(), job_id)

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
            self._post_system(room_id, engine.cycle_internal_error_notice())

    async def _run_cycle_locked(self, room_id: str, user_message: RoomMessage) -> None:
        room = self.store.get(room_id)
        if room is None:
            return
        # No process-wide lock. The workspace values ride a ContextVar per
        # cycle, so concurrent rooms cannot interleave each other's mounts and
        # nothing has to be serialized to keep them apart. This used to hold
        # one asyncio lock for the WHOLE cycle — every model await included —
        # so one room blocked every other, and a local-model turn could hold
        # the gateway for the full 1800s turn timeout (#67). Per-room
        # serialization still applies, via _room_lock in the caller.
        attachments.sync_uploads_into_room(self._home_dir(), room)
        # Bind the room id for the whole cycle so the cross-room tools know
        # where the turn is speaking from without trusting a tool argument
        # (see crossroom.in_room). Nested inside the workspace overlay for
        # the same per-task propagation guarantees.
        with ide.apply_room_workspace(room, self._home_dir()):
            with crossroom.in_room(room.id):
                await self._run_cycle_workspace(room, user_message)

    async def _run_cycle_workspace(self, room: Room, user_message: RoomMessage) -> None:
        room_id = room.id
        self._stop_event(room_id).clear()
        budget = room.max_agent_turns
        # Snapshot the roster for this user-message cycle. Invite/remove
        # updates stored members immediately (and posts a system line) but
        # must not rewrite a queue that is already running — the newcomer
        # speaks on the next user message, and a removed member still
        # finishes a turn already planned. Model-switch uses AgentBusy
        # because it evicts a running AIAgent; membership does not, so
        # we do not raise AgentBusy here.
        cycle_members = list(room.members)
        names = self._display_names(room)
        max_rounds = room.max_followup_rounds
        planned_room = engine.with_members(room, cycle_members)
        queue = engine.plan_user_turns(planned_room, user_message.text, names)
        attempted: List[str] = []
        replies: List[tuple[str, str]] = []
        noticed: List[str] = []
        posted_drop = False
        turns_taken = 0

        async def run_speaker(member: str) -> tuple[Optional[str], str]:
            """Run one member. Returns (verdict, posted_text).

            *verdict* is None when the turn was a silent no-op skip or the
            cycle was stopped mid-call — the caller must not count those
            against the budget.
            """
            nonlocal room, turns_taken
            if self._stop_event(room_id).is_set():
                return None, ""
            room = self.store.get(room_id) or room
            # Queued for a message it has already read. Announcing the turn
            # and then reporting that it did not reply is pure noise, so
            # skip before the room says anything at all.
            if not self._unseen_delta(room, member):
                return None, ""
            self._post_system(
                room_id, engine.turn_started_notice(names.get(member, member))
            )
            ok, reply = await self._agent_turn(room, member)
            if self._stop_event(room_id).is_set():
                return None, ""
            turns_taken += 1
            attempted.append(member)
            verdict = engine.classify_turn(ok, reply)
            if verdict == engine.TURN_PASS:
                # Explicit pass: no agent line, no did-not-reply notice.
                return verdict, ""
            ask = user_message.text or ""
            already = set(attachments.upload_paths_in(ask))
            found = attachments.harvest(
                self._home_dir(),
                room_id,
                member,
                since=float(room.created_at or 0),
                reply=f"{ask}\n{reply or ''}",
            )
            recalled = [
                item
                for item in attachments.matching_uploads(self._home_dir(), room_id, ask)
                if item.get("path") not in already
            ]
            by_name = {item["name"]: item for item in recalled}
            for item in found:
                by_name[item["name"]] = item
            published = list(by_name.values())
            if published:
                reply = attachments.with_published_paths(reply if ok else "", published)
                ok = True
            if not ok:
                # A turn that ran and failed (timeout, dispatch) must
                # still speak in the member's voice. A system-only
                # notice is silence on Speak Replies and looks like
                # ghosting. The spoken line is failed_turn_reply —
                # distinct from FALLBACK_GENERIC so it is not an empty
                # successful answer (#133). The system notice stays
                # under it with the exact reason and still clears the
                # thinking indicator.
                pending = room_clarify.pending_for_room(room)
                clarify_entry = pending[1] if pending else None
                last_tool = None if clarify_entry is not None else self._last_tool_block(
                    member, room_id
                )
                spoken = engine.failed_turn_reply(
                    reply, last_tool=last_tool, clarify=clarify_entry
                )
                room_clarify.release_room(room)
                posted = self.store.append(
                    room_id,
                    RoomMessage(
                        seq=0,
                        ts=0,
                        kind=KIND_AGENT,
                        speaker=member,
                        text=spoken,
                    ),
                )
                self._note_posted(room_id, posted)
                self._post_system(room_id, engine.did_not_reply_notice(member, reply))
                return engine.TURN_FAIL, ""
            if not (reply or "").strip():
                reply = engine.fallback_reply(ask)
            posted = self.store.append(
                room_id,
                RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=member, text=reply),
            )
            self._note_posted(room_id, posted)
            return engine.TURN_SPEAK, reply

        # A fresh @mention may hand the floor BACK to a member who already
        # spoke — that is how a junior's "@Lead ready for your review" gets
        # the lead a review turn inside the same cycle (#205). Unbounded,
        # that is a ping-pong loop, so each member may speak at most
        # 1 + max_followup_rounds times per cycle: the room's own round
        # dial bounds the back-and-forth, and max_agent_turns stays the
        # hard ceiling. With the default max_followup_rounds=0 this is
        # byte-identical to the old speak-once dedup.
        respeak_cap = 1 + max_rounds
        cap_noticed: set[str] = set()

        def capped_speakers() -> List[str]:
            return [m for m in set(attempted) if attempted.count(m) >= respeak_cap]

        def merge_into(target: List[str], member: str, posted_text: str) -> None:
            if posted_text:
                replies.append((member, posted_text))
            capped = capped_speakers()
            target.extend(
                engine.merge_followups(
                    engine.with_members(room, cycle_members),
                    [(member, posted_text)],
                    target,
                    capped,
                    budget - turns_taken,
                    names,
                )
            )
            # A mention dropped by the re-speak cap must stay transcript-
            # visible (#189's invariant, extended to the #205 cap): the
            # assignment would otherwise vanish silently.
            dropped = [
                m
                for m in engine.parse_mentions(posted_text or "", cycle_members, names)
                if m in capped and m != member and m not in target and m not in cap_noticed
            ]
            if dropped:
                text = engine.dropped_pending_notice(
                    engine.dropped_pending(
                        dropped, reason=engine.REASON_FOLLOWUP_ROUNDS, used=max_rounds
                    ),
                    names,
                    cycle_members,
                )
                if text:
                    self._post_system(room_id, text)
                    cap_noticed.update(dropped)

        def note_queued(queued: List[str]) -> None:
            noticed.extend(queued)

        def post_pending_drop(reason: str, used: int) -> None:
            """One transcript line when a mention/queue dies on a budget (#189)."""
            nonlocal posted_drop
            if posted_drop or self._stop_event(room_id).is_set():
                return
            pending = engine.pending_mentioned(
                planned_room,
                replies,
                attempted,
                names,
                exclude=noticed,
            )
            text = engine.dropped_pending_notice(
                engine.dropped_pending(pending, reason=reason, used=used),
                names,
                cycle_members,
            )
            if not text:
                return
            self._post_system(room_id, text)
            posted_drop = True

        # Phase 1: first planned wave (mentions / lead) plus @mention follow-ups.
        first_wave_budget_hit = False
        while queue:
            if self._stop_event(room_id).is_set():
                break
            if turns_taken >= budget:
                self._post_system(
                    room_id, engine.cycle_budget_notice(budget, queue)
                )
                note_queued(queue)
                first_wave_budget_hit = True
                break
            wave, queue = engine.take_wave(queue, budget - turns_taken)
            if not wave:
                break
            # One speaker at a time. Their reply is on the transcript
            # before the next member starts, so reviewers see the draft.
            member = wave[0]
            verdict, posted_text = await run_speaker(member)
            if self._stop_event(room_id).is_set():
                break
            if verdict in (engine.TURN_SPEAK, engine.TURN_FAIL):
                merge_into(queue, member, posted_text)

        # Phase 2: bounded speak-or-pass follow-up rounds. The room
        # settles when a full round adds no speech, or the round cap
        # / turn budget is hit. Budget remains the hard ceiling.
        rounds_used = 0
        if self._stop_event(room_id).is_set():
            return
        if first_wave_budget_hit:
            post_pending_drop(engine.REASON_AGENT_TURNS, turns_taken)
            return
        # A message that names its members is answered by them. Laps are
        # for an undirected statement, where the default responder spoke
        # and someone else may have something to add (#160).
        if engine.is_directed(user_message.text, cycle_members, names):
            if turns_taken >= budget:
                post_pending_drop(engine.REASON_AGENT_TURNS, turns_taken)
            return
        skip = list(attempted)
        while rounds_used < max_rounds:
            if self._stop_event(room_id).is_set():
                break
            remaining = budget - turns_taken
            if remaining <= 0:
                leftover = engine.plan_followup_round(cycle_members, attempted, 8)
                if leftover:
                    self._post_system(
                        room_id, engine.cycle_budget_notice(budget, leftover)
                    )
                    note_queued(leftover)
                break
            round_queue = engine.plan_followup_round(cycle_members, skip, remaining)
            if not round_queue:
                break
            rounds_used += 1
            round_speakers: List[str] = []
            while round_queue:
                if self._stop_event(room_id).is_set():
                    break
                if turns_taken >= budget:
                    if round_queue:
                        self._post_system(
                            room_id, engine.cycle_budget_notice(budget, round_queue)
                        )
                        note_queued(round_queue)
                        post_pending_drop(engine.REASON_AGENT_TURNS, turns_taken)
                        return
                    break
                wave, round_queue = engine.take_wave(
                    round_queue, budget - turns_taken
                )
                if not wave:
                    break
                member = wave[0]
                verdict, posted_text = await run_speaker(member)
                if self._stop_event(room_id).is_set():
                    break
                if verdict == engine.TURN_SPEAK:
                    round_speakers.append(member)
                    merge_into(round_queue, member, posted_text)
            if engine.followup_round_settled(round_speakers):
                break
            skip = list(round_speakers)

        if self._stop_event(room_id).is_set():
            return
        if turns_taken >= budget:
            post_pending_drop(engine.REASON_AGENT_TURNS, turns_taken)
        elif rounds_used >= max_rounds:
            post_pending_drop(engine.REASON_FOLLOWUP_ROUNDS, rounds_used)

    def _unseen(self, room: Room, member: str) -> tuple[list, list]:
        """(readable, respondable) transcript slices for *member*.

        Both exclude the member's own lines — they are already in its
        session history. *respondable* additionally drops a trailing
        "X is on it." notice, which is the room announcing this very turn:
        left in, a member ends up answering its own turn announcement.
        The cursor advances over *readable* so the announcement is not
        re-delivered later.
        """
        readable = [
            m
            for m in self.store.read_since(room.id, room.last_seen.get(member, 0))
            if not (m.kind == KIND_AGENT and m.speaker == member)
            # Tool-activity lines are presentation-only observability
            # (#218): members never respond to them, so they are neither
            # readable context nor a reason to take a turn.
            and m.kind != KIND_TOOL
        ]
        if not readable:
            return readable, readable
        turn_started = engine.turn_started_notice(self._display_names(room).get(member, member))
        last = readable[-1]
        if last.kind == KIND_SYSTEM and last.speaker == "room" and last.text == turn_started:
            return readable, readable[:-1]
        return readable, readable

    def _unseen_delta(self, room: Room, member: str) -> list:
        """What *member* actually has to respond to. Empty means no-op turn."""
        return self._unseen(room, member)[1]

    def _last_tool_block(self, member: str, room_id: str) -> Optional[Dict[str, Any]]:
        """Last in-flight or completed tool on this member's room session."""
        import json as _json
        import sqlite3

        home = Path(self._home_dir())
        paths = [home / "state.db", home / "profiles" / member / "state.db"]
        keys = room_clarify.session_keys(room_id, member)
        for db_path in paths:
            if not db_path.is_file():
                continue
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            except sqlite3.Error:
                continue
            try:
                row = con.execute(
                    "SELECT id FROM sessions WHERE session_key IN ({}) "
                    "ORDER BY last_activity_at DESC".format(
                        ",".join("?" * len(keys))
                    ),
                    keys,
                ).fetchone()
                if row is None:
                    # Profile DB often has one session with a null key.
                    row = con.execute(
                        "SELECT id FROM sessions ORDER BY last_activity_at DESC LIMIT 1"
                    ).fetchone()
                if row is None:
                    continue
                msg = con.execute(
                    "SELECT role, tool_name, tool_calls, content FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT 8",
                    (row[0],),
                ).fetchall()
            except sqlite3.Error:
                continue
            finally:
                con.close()
            last_tool_out = None
            last_call = None
            for role, tool_name, tool_calls, content in msg:
                if role == "tool" and last_tool_out is None:
                    last_tool_out = {
                        "name": tool_name or "",
                        "output": content or "",
                        "arguments": {},
                    }
                if role == "assistant" and tool_calls and last_call is None:
                    try:
                        calls = _json.loads(tool_calls)
                    except (TypeError, ValueError):
                        calls = []
                    if isinstance(calls, list) and calls:
                        fn = (calls[0] or {}).get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = _json.loads(args)
                            except (TypeError, ValueError):
                                args = {}
                        last_call = {
                            "name": fn.get("name") or "",
                            "arguments": args if isinstance(args, dict) else {},
                            "output": "",
                        }
            if last_call and last_call.get("name") == "clarify":
                return last_call
            if last_tool_out:
                return last_tool_out
            if last_call:
                return last_call
        return None

    def _restore_watermark(
        self, room: Room, member: str, previous: int, tentative: int
    ) -> None:
        """Unstick a failed turn's cursor. See ``_agent_turn`` for why."""
        self.store.restore_last_seen(room.id, member, previous, tentative)
        stored = self.store.get(room.id)
        if stored is not None:
            room.last_seen[member] = int(stored.last_seen.get(member, previous))
        else:
            room.last_seen[member] = max(0, int(previous))

    async def _agent_turn(self, room: Room, member: str) -> tuple[bool, str]:
        """Deliver the unseen transcript to ``member`` and await its reply."""
        readable, delta = self._unseen(room, member)
        if not delta:
            return False, "nothing new to respond to"

        # Governed retainers carry their operating contract into every ide
        # turn — FAIL CLOSED when it cannot be read (governed.py). The
        # (False, reason) return rides the existing failure path, so the
        # transcript shows a did-not-reply notice naming the cause.
        governed_contract: Optional[str] = None
        if (room.workspace or "sandbox") == "ide" and hire.agent_is_governed(
            self._home_dir(), member
        ):
            governed_contract, gc_err = governed.contract_text()
            if not governed_contract:
                return False, (
                    f"governed contract unavailable — {gc_err}; refusing to "
                    "run this governed retainer without its contract"
                )
        delivered_through = readable[-1].seq
        previous = int(room.last_seen.get(member, 0))
        # Cap injection only. The watermark still covers the full readable
        # slice so a completed turn does not re-inject the elided tail.
        capped, omitted = engine.cap_delta(delta)
        trigger = capped[-1]
        context_block = engine.format_delta_context(capped[:-1], omitted)

        # Which runtime executes this member's turn (#218). Grok Build
        # members run host-native, so their briefing must name host paths
        # and their working directory is validated before anything else.
        member_runtime = runtimes.runtime_for_member(self._home_dir(), member)
        grok_cwd: Optional[str] = None
        host_uploads: Optional[str] = None
        grok_worktrees: List[Dict[str, str]] = []
        if member_runtime == runtimes.RUNTIME_GROK_BUILD:
            if (room.workspace or "sandbox") == "ide":
                try:
                    if ide.mounts_ide_root(room):
                        grok_cwd = ide.configured_ide_root()
                    else:
                        grok_cwd = ide.resolve_ide_path(room.ide_path)
                except ValueError as e:
                    return False, f"cannot resolve the room workspace: {e}"
                if not grok_cwd or not os.path.isdir(grok_cwd):
                    return False, (
                        f"room workspace path does not exist on this host: "
                        f"{grok_cwd or room.ide_path!r}"
                    )
                # Isolated worktrees (#223): the container overlays each
                # worktree over its place in /workspace; the host-native
                # equivalent is a briefing that points at the worktree's
                # real host path plus a permission gate that declines the
                # shadowed tree. The worktree dirs themselves are ensured
                # by apply_room_workspace at the top of every cycle.
                wt_root = worktrees.resolve_worktree_root()
                branch = worktrees.branch_for(room.id)
                for rel in getattr(room, "worktree_repos", None) or []:
                    rel = str(rel)
                    grok_worktrees.append(
                        {
                            "rel": rel,
                            "real": os.path.join(grok_cwd, rel),
                            "path": worktrees.worktree_path(room.id, rel, wt_root),
                            "branch": branch,
                        }
                    )
            else:
                grok_cwd = grokbuild.sandbox_workspace_dir(self._home_dir(), room.id)
            host_uploads = attachments.host_dir(self._home_dir(), room.id)

        me = principal.load(self._home_dir())
        speakers = {
            m.speaker for m in self.store.read_since(room.id, 0) if m.kind == KIND_USER
        }
        if me.get("display_name") and me["display_name"] not in {"You", "User"}:
            speakers.discard("You")
            speakers.discard("User")
            speakers.add(str(me["display_name"]))
        user_names = sorted(speakers) or [str(me.get("display_name") or _DEFAULT_USER_NAME)]
        briefing = engine.room_briefing(
            room,
            member,
            user_names,
            self._display_names(room),
            itinerary=itinerary.load(self._home_dir(), room.id),
            artifacts=[
                item["path"]
                for item in attachments.list_uploads(self._home_dir(), room.id)
            ],
            principal_about=str(me.get("about") or "") or None,
            principal_name=str(me.get("display_name") or "") or None,
            other_rooms=crossroom.other_rooms(
                self.store.list_rooms(), member, room.id
            ),
            governed_contract=governed_contract,
            jobs=self._member_jobs(room),
            host_workspace=grok_cwd,
            host_uploads=host_uploads,
            host_worktrees=grok_worktrees or None,
        )

        if member_runtime == runtimes.RUNTIME_GROK_BUILD:
            return await self._grok_turn(
                room,
                member,
                cwd=grok_cwd or "",
                briefing=briefing,
                context_block=context_block,
                trigger=trigger,
                governed_contract=governed_contract,
                previous=previous,
                delivered_through=delivered_through,
                worktree_map=grok_worktrees,
            )

        speaker_display = (
            f"{trigger.speaker} (agent)" if trigger.kind == KIND_AGENT else trigger.speaker
        )
        source = self.build_source(
            chat_id=room.id,
            chat_name=f"room:{room.name}",
            chat_type="group",
            user_id=f"{trigger.kind}:{trigger.speaker}",
            user_name=speaker_display,
            # thread_id is load-bearing for parallelism: BasePlatformAdapter
            # builds the session key WITHOUT the multiplex profile namespace,
            # so two members in the same room would share one _active_sessions
            # slot and one SessionDB row. thread_id splits that key AND is
            # copied into notify metadata so send() can resolve the speaker.
            thread_id=member,
        )
        source.is_bot = trigger.kind == KIND_AGENT
        # Route this turn to the member's profile (in-process multiplexer).
        source.profile = None if member == "default" else member

        task_id = f"room-{room.id}-{member}-{int(time.time() * 1000)}"
        fut: Future = Future()
        key = (room.id, member)
        session_key = self._session_key_for(source)
        with self._pending_lock:
            stale = self._pending.pop(key, None)
            if stale is not None and not stale.future.done():
                stale.future.set_result((False, "superseded by a newer turn"))
            self._pending[key] = _PendingTurn(
                task_id=task_id,
                room_id=room.id,
                member=member,
                future=fut,
                session_key=session_key,
                source=source,
            )
        # Existing row from a previous turn: hide before dispatch.
        self._hide_room_session(session_key, member)

        media_urls, media_types = attachments.host_media_for_text(
            self._home_dir(),
            room.id,
            "\n".join(
                p
                for p in (trigger.text, context_block)
                if p
            ),
        )
        has_image = any(
            (t or "").startswith("image/") or attachments._IMAGE_EXT.search(u or "")
            for u, t in zip(media_urls, media_types)
        )
        event = MessageEvent(
            text=trigger.text,
            message_type=MessageType.PHOTO if has_image else MessageType.TEXT,
            source=source,
            message_id=task_id,
            internal=True,  # queue behind a busy turn; never interrupt
            channel_prompt=briefing,
            channel_context=context_block,
            media_urls=media_urls,
            media_types=media_types,
            metadata={"retinue_room": room.id, "retinue_member": member},
        )

        # Tentatively mark the delta delivered, then stick or restore.
        # Speak and explicit pass both count as completion (ok=True) and
        # keep the cursor; timeout / dispatch error restore *previous* so
        # the member re-sees this delta on the next cycle.
        #
        # Per-room turns are serialized: _run_cycle holds _room_lock for
        # the whole user-message cycle, and run_speaker awaits one member
        # at a time. The same member therefore cannot complete a later
        # turn that would make restore_last_seen regress a legitimate
        # advance. touch_last_seen still merges under the store lock so
        # a parallel *other* member cannot lose their cursor on our write.
        self.store.touch_last_seen(room.id, member, delivered_through)
        room.last_seen[member] = max(room.last_seen.get(member, 0), delivered_through)

        completed = False
        token = _turn_member.set(member)
        # Broker identity rides every command this turn executes
        # (tools/turn_env.py) — the container is shared per room, so this is
        # the only carrier that is per-member. Never fails the turn: a turn
        # without a token simply cannot use the broker.
        _tenv_token = None
        try:
            from tools import turn_env as _turn_env_mod

            _tenv = {
                brokertoken.TOKEN_ENV: brokertoken.mint(self._home_dir(), member)
            }
            try:
                # What /workspace actually IS on the host this turn, so the
                # broker translates a container cwd instead of assuming the
                # room is mounted at the IDE root (infra-90xc). Computed
                # separately: a room record the mount map cannot resolve must
                # not also cost the retainer its broker identity.
                _tenv[ide.MOUNT_MAP_ENV] = json.dumps(
                    ide.workspace_mount_map(room), separators=(",", ":")
                )
            except Exception:
                logger.debug("workspace mount map failed", exc_info=True)
            try:
                # Which of the room's member skill mounts is THIS member's
                # (#188). The container holds every member's dir; only the
                # turn knows whose turn it is. Unset when the member has no
                # skills dir, so the var never names a path that is not
                # mounted.
                _skills = ide.member_skills_mount_for(member)
                if _skills:
                    _tenv[ide.MEMBER_SKILLS_ENV] = _skills
            except Exception:
                logger.debug("member skills path failed", exc_info=True)
            _tenv_token = _turn_env_mod.set_turn_env(_tenv)
        except Exception:
            logger.debug("broker token bind failed", exc_info=True)
        # Provider stalls/retries surface on the transcript instead of only
        # the journal (#166) — same ContextVar carrier as the broker token.
        _vis_token = None
        try:
            from tools import turn_visibility as _turn_visibility

            _vis_token = _turn_visibility.set_notifier(
                self._provider_event_notifier(room.id, member)
            )
        except Exception:
            logger.debug("turn visibility bind failed", exc_info=True)
        try:
            try:
                # Dispatch under the member's Hermes-home override. The
                # gateway scopes model/SOUL/credential resolution itself, but
                # config-reading side paths — most visibly the tool
                # registry's availability check_fns — otherwise evaluate
                # against the workspace ROOT config and silently drop
                # per-profile capabilities like image_gen from the turn
                # (novique-ai/retinue#207). Deliberately ONLY the home
                # override (hermes_constants, a leaf module): entering the
                # gateway's full _profile_runtime_scope here would import
                # gateway.run and re-hydrate secret scopes mid-dispatch.
                # The override is a contextvar stack, so the gateway
                # re-scoping the same profile downstream is harmless.
                from hermes_constants import (
                    reset_hermes_home_override,
                    set_hermes_home_override,
                )

                profile_home = os.path.join(self._home_dir(), "profiles", member)
                if os.path.isdir(profile_home):
                    _home_token = set_hermes_home_override(profile_home)
                    try:
                        await self.handle_message(event)
                    finally:
                        reset_hermes_home_override(_home_token)
                else:
                    await self.handle_message(event)
            except Exception as e:
                self._resolve_pending(
                    room.id, ok=False, text=f"dispatch failed: {e}", member=member
                )
            finally:
                _turn_member.reset(token)
                # Newly created row: hide immediately after dispatch.
                self._hide_room_session(session_key, member)

            budget = hire.turn_timeout_for(
                self._home_dir(), member, room.workspace or "sandbox"
            )
            try:
                ok, text = await asyncio.wait_for(
                    asyncio.wrap_future(fut), timeout=budget
                )
            except asyncio.TimeoutError:
                self._resolve_pending(
                    room.id, ok=False, text="turn timed out", member=member
                )
                return False, f"no reply within {int(budget)}s"
            completed = bool(ok)
            return ok, text
        finally:
            if _vis_token is not None:
                try:
                    _turn_visibility.reset(_vis_token)
                except Exception:
                    pass
            if _tenv_token is not None:
                try:
                    _turn_env_mod.reset(_tenv_token)
                except Exception:
                    pass
            if not completed:
                self._restore_watermark(room, member, previous, delivered_through)

    def _grok_manager(self) -> "grokbuild.GrokBuildManager":
        mgr = getattr(self, "_grok_mgr", None)
        if mgr is None:
            mgr = grokbuild.GrokBuildManager(self._home_dir())
            self._grok_mgr = mgr
        return mgr

    async def _grok_turn(
        self,
        room: Room,
        member: str,
        *,
        cwd: str,
        briefing: str,
        context_block: Optional[str],
        trigger: RoomMessage,
        governed_contract: Optional[str],
        previous: int,
        delivered_through: int,
        worktree_map: Optional[List[Dict[str, str]]] = None,
    ) -> tuple[bool, str]:
        """One member turn executed by the Grok Build runtime (#218).

        The whole tool loop runs inside ``grok agent stdio``; this method
        supplies the prompt, streams tool activity onto the transcript,
        answers nothing itself (permission requests are decided by
        ``grokbuild.decide_permission``), and maps the stop reason back to
        the ``(ok, text)`` contract every caller of ``_agent_turn`` expects.
        Watermark semantics mirror the Hermes path exactly: the delta is
        tentatively marked delivered, and restored unless the turn
        completes.
        """
        manager = self._grok_manager()
        member_meta = self._member_meta(member)
        approval = grokbuild.approval_mode(member_meta)
        budget = hire.turn_timeout_for(
            self._home_dir(), member, room.workspace or "sandbox"
        )

        trigger_line = engine.format_lines([trigger])
        delta_text = (
            f"{context_block}\n{trigger_line}" if context_block else trigger_line
        )

        def build_prompt(fresh: bool) -> str:
            parts: List[str] = []
            if fresh:
                parts.append(briefing)
                parts.append("New activity in the room:")
            else:
                parts.append(
                    "New activity in the room since your last turn "
                    "(same rules as before — reply as yourself; pass with "
                    f"{engine.pass_payload_text()} if this adds nothing):"
                )
                # The briefing rides only the first prompt of a session, but
                # a governed retainer's contract must bind every turn.
                if governed_contract:
                    parts.append(
                        "## OPERATING CONTRACT (binding)\n"
                        + governed_contract.strip()
                    )
            parts.append(delta_text)
            return "\n\n".join(p for p in parts if p)

        cancel_ev = asyncio.Event()

        async def grok_cancel() -> None:
            cancel_ev.set()
            await manager.cancel(room.id, member)

        task_id = f"room-{room.id}-{member}-{int(time.time() * 1000)}"
        fut: Future = Future()
        key = (room.id, member)
        with self._pending_lock:
            stale = self._pending.pop(key, None)
            if stale is not None and not stale.future.done():
                stale.future.set_result((False, "superseded by a newer turn"))
            self._pending[key] = _PendingTurn(
                task_id=task_id,
                room_id=room.id,
                member=member,
                future=fut,
                grok_cancel=grok_cancel,
            )

        def on_activity(event: str, payload: Dict[str, Any]) -> None:
            # Called on the event loop (ACP reader task). store.append is
            # thread-safe and wakes the SSE/long-poll watchers itself.
            # needs_user bookkeeping is skipped on purpose: a tool line
            # cannot @ the principal.
            title = str(payload.get("title") or "tool")
            if event == "tool_start":
                text = title
            elif event == "tool_failed":
                text = f"{title} — failed"
            elif event == "rejected":
                text = f"declined: {payload.get('reason') or title}"
            elif event == "resumed":
                # The runtime cancelled the prompt over a policy rejection
                # and the manager re-prompted the same session (#231).
                text = f"resumed after declined: {title}"
            else:
                return  # tool_done duplicates tool_start's line for the UI
            try:
                self.store.append(
                    room.id,
                    RoomMessage(seq=0, ts=0.0, kind=KIND_TOOL, speaker=member, text=text),
                )
            except Exception:
                logger.debug("grok tool activity post failed", exc_info=True)

        self.store.touch_last_seen(room.id, member, delivered_through)
        room.last_seen[member] = max(room.last_seen.get(member, 0), delivered_through)

        completed = False
        try:
            try:
                result = await manager.run_turn(
                    room.id,
                    member,
                    cwd,
                    build_prompt=build_prompt,
                    approval=approval,
                    timeout=budget,
                    on_activity=on_activity,
                    cancel_event=cancel_ev,
                    # Isolated worktrees (#223): the room's checkouts are
                    # writable roots; the shadowed real trees are declined
                    # with a redirect.
                    extra_roots=tuple(w["path"] for w in worktree_map or ()),
                    denied_roots={
                        w["real"]: w["path"] for w in worktree_map or ()
                    }
                    or None,
                )
            except grokbuild.GrokBuildAuthRequired as e:
                ok, text = False, (
                    f"Grok Build needs a login on this machine — run "
                    f"`grok login` as the gateway user ({e})"
                )
            except grokbuild.GrokBuildUnavailable as e:
                ok, text = False, f"Grok Build runtime unavailable: {e}"
            except grokbuild.GrokBuildError as e:
                ok, text = False, f"Grok Build turn failed: {e}"
            else:
                stop = result.stop_reason
                reply = result.text.strip()
                if stop == "end_turn":
                    ok, text = (True, reply) if reply else (
                        False,
                        "agent returned no reply",
                    )
                elif stop == "cancelled":
                    # Runtime reject-cancel that resuming could not recover
                    # (#231) names its blocker; a genuine cancel stays terse.
                    ok, text = False, (
                        f"turn ended after a declined action ({result.last_reject})"
                        if result.last_reject
                        else "turn cancelled"
                    )
                elif stop in ("max_turn_requests", "max_tokens"):
                    ok, text = (
                        False,
                        f"stopped early ({stop})"
                        + (f": {reply[:400]}" if reply else ""),
                    )
                elif stop == "refusal":
                    ok, text = False, "the model declined this request"
                else:
                    ok, text = False, f"unexpected stop reason {stop!r}"
            self._resolve_pending(room.id, ok=ok, text=text, member=member)
            # Stop may have resolved the pending entry first; its verdict
            # (False, "stopped") is the one the cycle must see.
            if fut.done():
                ok, text = fut.result()
            completed = bool(ok)
            return ok, text
        finally:
            if not completed:
                self._restore_watermark(room, member, previous, delivered_through)

    def _member_meta(self, member: str) -> Dict[str, Any]:
        path = os.path.join(
            self._home_dir(), "profiles", member, hire.AGENT_META_FILENAME
        )
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def session_cwd_for(self, source) -> str:
        """Host-side working directory for this turn's prompt context.

        ide rooms anchor the session at the room's ide_path so the standard
        project-context chain (AGENTS.md / CLAUDE.md via
        build_context_files_prompt) loads exactly as it does for a host CLI
        session working in that tree. Sandbox rooms return "" — their
        /workspace is disposable and maps to no host project. Called by the
        gateway when binding session vars for a turn; must never raise.
        """
        try:
            room = self.store.get(str(getattr(source, "chat_id", "") or ""))
        except Exception:
            return ""
        if room is None or (room.workspace or "sandbox") != "ide":
            return ""
        path = (room.ide_path or "").strip()
        return path if path and os.path.isdir(path) else ""

    def _note_posted(self, room_id: str, message: RoomMessage) -> None:
        """Set or clear the room's needs_user flag for a just-posted line."""
        if message.kind == KIND_SYSTEM:
            return
        room = self.store.get(room_id)
        if room is None:
            return
        name = str(principal.load(self._home_dir()).get("display_name") or "")
        names = self._display_names(room)

        def apply(stored: Room) -> None:
            engine.apply_needs_user(
                stored,
                message,
                principal_name=name,
                member_names=names,
            )

        try:
            self.store.mutate(room_id, apply)
        except KeyError:
            return

    def _provider_event_notifier(self, room_id: str, member: str):
        """Per-turn callback for tools.turn_visibility (#166).

        Runs in the conversation loop's worker thread, so the transcript
        write hops to the gateway loop when one is available. Rate-limited
        per turn: provider kills arrive 60-120s apart, so one line each is
        signal; a misbehaving provider must not become transcript spam.
        """
        room = self.store.get(room_id)
        display = self._display_names(room).get(member, member) if room else member
        last_post = [0.0]

        def _notify(message: str) -> None:
            now = time.monotonic()
            if now - last_post[0] < 20.0:
                return
            last_post[0] = now
            text = engine.provider_event_notice(display, message)
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._post_system, room_id, text)
            else:
                self._post_system(room_id, text)

        return _notify

    def _post_system(self, room_id: str, text: str) -> Optional[RoomMessage]:
        try:
            return self.store.append(
                room_id, RoomMessage(seq=0, ts=0, kind=KIND_SYSTEM, speaker="room", text=text)
            )
        except Exception:
            logger.exception("Retinue rooms: failed to post system notice to %s", room_id)
            return None


# ── HTTP surface ─────────────────────────────────────────────────────────

# Shown for GET / (and any other non-API path) when web_dist_dir() finds no
# built SPA at all — as opposed to a single missing asset within an existing
# dist/, which stays a JSON 404 in _serve_static. Plain self-contained
# string: no templating engine, no external assets, so it renders even with
# nothing else running.
_WEB_UI_NOT_BUILT_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Retinue rooms UI is not built</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto;
         padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
  code, pre { background: #f2f2f2; border-radius: 4px; font-size: 0.95em; }
  code { padding: 0.15rem 0.4rem; }
  pre { padding: 0.75rem 1rem; overflow-x: auto; }
  h1 { font-size: 1.3rem; }
</style>
</head>
<body>
<h1>Retinue rooms UI is not built</h1>
<p>The gateway is up, but <code>retinue-web/dist/</code> was not found, so there is no
built single-page app to serve at this path.</p>
<p>Build it once from a checkout:</p>
<pre>cd retinue-web &amp;&amp; npm run build</pre>
<p>Or run the guided setup script from the repo root, which also installs
dependencies:</p>
<pre>./scripts/retinue-dev-setup.sh</pre>
<p>Then reload this page. The API is already running &mdash; see
<a href="/health">/health</a>.</p>
</body>
</html>
"""


# Lazily resolved once; never shells out per /health poll. "unknown" when the
# process is not a git checkout (pip install), git is missing, or the probe fails.
_retinue_git_sha: Optional[str] = None


def retinue_git_sha() -> str:
    """Short ``git rev-parse --short HEAD`` for bug reports, or ``"unknown"``."""
    global _retinue_git_sha
    if _retinue_git_sha is not None:
        return _retinue_git_sha
    sha = "unknown"
    try:
        import subprocess

        # plugins/platforms/retinue_rooms/adapter.py → repo root is 3 up.
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        # Worktrees use a .git *file*; bare checkouts use a directory.
        if os.path.exists(os.path.join(root, ".git")):
            proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            text = (proc.stdout or "").strip()
            if proc.returncode == 0 and text:
                sha = text
    except Exception:
        sha = "unknown"
    _retinue_git_sha = sha
    return _retinue_git_sha


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

    def _read_raw(self, max_bytes: int) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > max_bytes:
            return None
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    def _no_content(self, status: int = 204) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

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

    _API_PREFIXES = (
        "rooms",
        "agents",
        "models",
        "runtimes",
        "health",
        "routines",
        "cron",
        "workspace",
        "voice",
        "tts",
        "sidebar",
        "auth",
        "principal",
        "identity",
        "projects",
    )

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["health"]:
            adapter = self.server.adapter
            payload = auth.health_payload(
                adapter._home_dir(),
                len(adapter.store.list_rooms()),
            )
            payload["git_sha"] = retinue_git_sha()
            try:
                payload["runtimes"] = {
                    entry["id"]: entry["health"]
                    for entry in runtimes.list_runtimes(adapter._home_dir())
                }
            except Exception:
                logger.debug("runtime health failed", exc_info=True)
            return self._json(200, payload)
        if not parts or parts[0] not in self._API_PREFIXES:
            if self._serve_static(parsed.path):
                return
            if self.server.adapter.web_dist_dir() is None:
                # No dist/ anywhere (see web_dist_dir's search order) — a
                # contributor who hasn't run npm run build yet needs a next
                # step, not a bare JSON error. A dist/ that IS present but
                # missing this one file (path traversal, corrupt build)
                # keeps the terse JSON 404 below.
                body = _WEB_UI_NOT_BUILT_HTML.encode("utf-8")
                return self._bytes(404, body, "text/html; charset=utf-8")
            return self._json(404, {"error": "not found (web UI not built — see retinue-web)"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        adapter = self.server.adapter
        if parts == ["agents"]:
            return self._json(200, {"agents": adapter.list_agents()})
        if len(parts) == 2 and parts[0] == "agents":
            for agent in adapter.list_agents():
                if agent.get("slug") == parts[1]:
                    return self._json(200, agent)
            return self._json(404, {"error": "no such agent"})
        if parts == ["models"]:
            return self._json(200, {"models": adapter.list_model_presets()})
        if parts == ["runtimes"]:
            return self._json(
                200, {"runtimes": runtimes.list_runtimes(adapter._home_dir())}
            )
        if parts == ["routines"]:
            return self._json(200, {"routines": routines.list_routines(adapter._home_dir())})
        if parts == ["cron", "jobs"]:
            query = parse_qs(parsed.query)
            owner = (query.get("owner") or [None])[0]
            room = (query.get("room") or [None])[0]
            try:
                jobs = adapter.list_cron_jobs(owner=owner, room=room)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except cronjobs.UnknownOwner:
                return self._json(404, {"error": "no such retainer"})
            return self._json(
                200,
                {
                    "jobs": jobs,
                    "owners": cronjobs.served_owners(adapter._home_dir()),
                    "timezone": cronjobs.timezone_display(),
                },
            )
        if len(parts) == 2 and parts[0] == "routines":
            meta = routines.get_routine(adapter._home_dir(), parts[1])
            if meta is None:
                return self._json(404, {"error": "no such routine"})
            return self._json(200, meta)
        if parts == ["principal"]:
            return self._json(200, principal.load(adapter._home_dir()))
        if parts == ["workspace"]:
            return self._json(200, workspace.workspace_status())
        if parts == ["workspace", "folders"]:
            query = parse_qs(parsed.query)
            try:
                return self._json(200, ide.list_folders((query.get("path") or [""])[0]))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if parts == ["voice"]:
            return self._json(200, voice.status(adapter._home_dir()))
        if parts == ["identity", "palette"]:
            return self._json(200, identity.palette_payload())
        if parts == ["sidebar"]:
            return self._json(200, adapter.get_sidebar())
        if parts == ["projects"]:
            return self._json(200, adapter.projects_payload())
        if parts == ["auth"] or (len(parts) == 2 and parts[0] == "auth" and parts[1] == "reauth"):
            query = parse_qs(parsed.query)
            session_id = (query.get("session") or [""])[0].strip()
            if session_id:
                sess = auth.get_session(session_id)
                if sess is None:
                    return self._json(404, {"error": "no such reauth session"})
                return self._json(200, sess)
            return self._json(
                200,
                {
                    "providers": auth.workspace_provider_status(adapter._home_dir()),
                    "accounts": auth.account_status(adapter._home_dir()),
                    "session": auth.active_pending("xai-oauth")
                    or auth.active_pending("openai-codex"),
                },
            )
        if parts == ["rooms"]:
            return self._json(200, {"rooms": adapter.list_rooms_public()})
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
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "routines":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            owned = [
                r
                for r in routines.list_routines(adapter._home_dir())
                if r.get("source_room") == parts[1]
            ]
            return self._json(200, {"routines": owned})
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "cron":
            return self._json(404, {"error": "not found"})
        if len(parts) == 4 and parts[0] == "rooms" and parts[2:] == ["cron", "jobs"]:
            try:
                jobs = adapter.list_room_cron_jobs(parts[1])
            except KeyError:
                return self._json(404, {"error": "no such room"})
            return self._json(200, {"jobs": jobs})
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "itinerary":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            return self._json(200, itinerary.load(adapter._home_dir(), parts[1]))
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "files":
            room = adapter.store.get(parts[1])
            if room is None:
                return self._json(404, {"error": "no such room"})
            query = parse_qs(parsed.query)
            raw = (query.get("path") or [""])[0] or ""
            try:
                upload = attachments.read_upload(adapter._home_dir(), parts[1], raw)
                if upload is not None:
                    data, ctype = upload
                else:
                    data, ctype = workspace.read_workspace_file(room, raw)
            except workspace.WorkspaceFileError as e:
                return self._json(e.status, {"error": str(e)})
            return self._bytes(200, data, ctype)
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

    def _filename_for_audio(self, parsed) -> str:
        query = parse_qs(parsed.query)
        name = (query.get("filename") or [""])[0].strip()
        if name:
            return Path(name).name
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = {
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/wave": ".wav",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/webm": ".webm",
            "audio/ogg": ".ogg",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
        }.get(ctype, ".wav")
        return f"speech{ext}"

    def _post_audio(self, adapter: RetinueRoomsAdapter, room_id: str, parsed) -> None:
        raw = self._read_raw(_MAX_AUDIO)
        if raw is None:
            return self._json(400, {"error": "invalid or oversized audio body"})
        query = parse_qs(parsed.query)
        from_name = (query.get("from") or [_DEFAULT_USER_NAME])[0] or _DEFAULT_USER_NAME
        draft = (query.get("draft") or [""])[0]
        try:
            result = adapter.post_user_audio(
                room_id,
                raw,
                filename=self._filename_for_audio(parsed),
                from_name=from_name,
                draft=draft,
            )
        except KeyError:
            return self._json(404, {"error": "no such room"})
        except voice.VoiceError as e:
            return self._json(502, {"error": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except RuntimeError as e:
            return self._json(503, {"error": str(e)})
        return self._json(202, result)

    def _post_attachment(self, adapter: RetinueRoomsAdapter, room_id: str, parsed) -> None:
        if adapter.store.get(room_id) is None:
            return self._json(404, {"error": "no such room"})
        raw = self._read_raw(attachments.MAX_ATTACHMENT)
        if raw is None:
            return self._json(400, {"error": "invalid or oversized attachment"})
        query = parse_qs(parsed.query)
        filename = (query.get("filename") or ["file"])[0] or "file"
        try:
            payload = attachments.save(adapter._home_dir(), room_id, filename, raw)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(201, payload)

    def _post_tts(self) -> None:
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "invalid or oversized JSON body"})
        text = str(body.get("text") or "")
        speaker = str(body.get("speaker") or body.get("voice") or "")
        summary_raw = body.get("spoken_summary")
        spoken_summary = str(summary_raw) if summary_raw not in (None, "") else None
        # A turn that is only an itinerary card has no spoken script (#158).
        # That is silence, not a TTS failure -- 204 keeps Speak Replies quiet
        # instead of surfacing a provider error under the message.
        try:
            audio = voice.synthesize_dispatch(
                text,
                speaker,
                home_dir=self.server.adapter._home_dir(),
                spoken_summary=spoken_summary,
            )
        except voice.VoiceError as e:
            # Silence (itinerary-only) is 204, not a TTS failure (#158).
            if str(e) == "empty text":
                return self._no_content()
            return self._json(502, {"error": str(e)})
        ctype = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"
        return self._bytes(200, audio, ctype)

    def do_POST(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        adapter = self.server.adapter
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "audio":
            return self._post_audio(adapter, parts[1], parsed)
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "attachments":
            return self._post_attachment(adapter, parts[1], parsed)
        if parts == ["tts"]:
            return self._post_tts()
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "invalid or oversized JSON body"})
        if parts == ["auth", "apikey"]:
            try:
                payload = auth.save_api_key(
                    adapter._home_dir(),
                    str(body.get("provider") or "anthropic"),
                    str(body.get("api_key") or ""),
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if parts == ["auth", "reauth"] or parts == ["auth"]:
            try:
                payload = adapter.start_provider_reauth(
                    str(body.get("provider") or "xai-oauth")
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(502, {"error": str(e)})
            return self._json(202, payload)
        if parts == ["agents"]:
            try:
                payload = adapter.hire_agent(
                    name=str(body.get("name") or ""),
                    job=str(body.get("job") or ""),
                    how=str(body.get("how") or ""),
                    model=str(body.get("model") or "") or None,
                    avatar_emoji=body.get("avatar_emoji") if "avatar_emoji" in body else None,
                    avatar_color=body.get("avatar_color") if "avatar_color" in body else None,
                    voice=body.get("voice") if "voice" in body else None,
                    persona=body.get("persona") if "persona" in body else None,
                    runtime=str(body.get("runtime") or "") or None,
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
                    max_followup_rounds=body.get("max_followup_rounds"),
                    workspace=body.get("workspace"),
                    ide_path=body.get("ide_path"),
                    worktree_repos=body.get("worktree_repos"),
                    shared_mode=body.get("shared_mode"),
                    project_id=body.get("project_id"),
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(201, payload)
        if parts == ["projects"]:
            try:
                payload = adapter.create_project(str(body.get("name") or ""))
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
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "stop":
            try:
                result = adapter.stop_cycle(
                    parts[1],
                    from_name=str(body.get("from") or _DEFAULT_USER_NAME),
                )
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            return self._json(200, result)
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "reset-session":
            try:
                result = adapter.reset_member_session(
                    parts[1], str(body.get("member") or "")
                )
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except AgentBusy as e:
                return self._json(409, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            return self._json(200, result)
        if parts == ["routines"]:
            try:
                payload = adapter.save_routine_from_room(
                    name=str(body.get("name") or ""),
                    room_id=str(body.get("room") or body.get("room_id") or ""),
                    since=int(body.get("since") or 0),
                    until=(int(body["until"]) if body.get("until") is not None else None),
                    owner=(str(body.get("owner") or "") or None),
                    schedule=(str(body.get("schedule") or "") or None),
                )
            except cronjobs.UnknownOwner:
                return self._json(404, {"error": "no such retainer"})
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except FileExistsError as e:
                return self._json(409, {"error": f"a routine named '{e}' already exists"})
            return self._json(201, payload)
        if parts == ["cron", "jobs"]:
            try:
                payload = adapter.create_cron_job(body)
            except cronjobs.UnknownOwner:
                return self._json(404, {"error": "no such retainer"})
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(201, payload)
        if len(parts) == 4 and parts[:2] == ["cron", "jobs"]:
            action = parts[3]
            try:
                if action == "pause":
                    payload = adapter.set_cron_job_enabled(parts[2], False)
                elif action == "resume":
                    payload = adapter.set_cron_job_enabled(parts[2], True)
                elif action == "run":
                    payload = adapter.run_cron_job(parts[2])
                else:
                    return self._json(404, {"error": "not found"})
            except cronjobs.UnknownJob:
                return self._json(404, {"error": "no such job"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 3 and parts[0] == "routines" and parts[2] == "run":
            try:
                payload = adapter.run_routine(
                    parts[1], str(body.get("room") or body.get("room_id") or "")
                )
            except KeyError as e:
                return self._json(404, {"error": f"not found: {e}"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            return self._json(202, payload)
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "members":
            try:
                payload = adapter.add_room_member(parts[1], str(body.get("member") or ""))
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(201, payload)
        return self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        adapter = self.server.adapter
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "invalid or oversized JSON body"})
        if len(parts) == 2 and parts[0] == "agents":
            try:
                payload = adapter.patch_agent(parts[1], body)
            except KeyError:
                return self._json(404, {"error": "no such agent"})
            except AgentBusy as e:
                return self._json(409, {"error": str(e)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 2 and parts[0] == "rooms":
            try:
                payload = adapter.patch_room(parts[1], body)
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 2 and parts[0] == "projects":
            try:
                payload = adapter.patch_project(parts[1], body)
            except KeyError:
                return self._json(404, {"error": "no such project"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 3 and parts[:2] == ["cron", "jobs"]:
            try:
                payload = adapter.patch_cron_job(parts[2], body)
            except cronjobs.UnknownJob:
                return self._json(404, {"error": "no such job"})
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        return self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "invalid or oversized JSON body"})
        if parts == ["principal"]:
            try:
                return self._json(200, principal.save(self.server.adapter._home_dir(), body))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if parts == ["sidebar"]:
            try:
                return self._json(200, self.server.adapter.put_sidebar(body))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if parts == ["projects"]:
            try:
                return self._json(200, self.server.adapter.put_projects(body))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "itinerary":
            if self.server.adapter.store.get(parts[1]) is None:
                return self._json(404, {"error": "no such room"})
            updated_by = str(body.get("updated_by") or "user")
            return self._json(
                200,
                itinerary.save(
                    self.server.adapter._home_dir(),
                    parts[1],
                    body,
                    updated_by=updated_by,
                ),
            )
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "rooms":
            if self.server.adapter.store.delete(parts[1]):
                return self._json(200, {"deleted": parts[1]})
            return self._json(404, {"error": "no such room"})
        if len(parts) == 2 and parts[0] == "agents":
            try:
                payload = self.server.adapter.delete_agent(parts[1])
            except KeyError:
                return self._json(404, {"error": "no such agent"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 4 and parts[0] == "rooms" and parts[2] == "members":
            try:
                payload = self.server.adapter.remove_room_member(parts[1], parts[3])
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, payload)
        if len(parts) == 2 and parts[0] == "routines":
            if routines.delete_routine(self.server.adapter._home_dir(), parts[1]):
                return self._json(200, {"deleted": parts[1]})
            return self._json(404, {"error": "no such routine"})
        if len(parts) == 3 and parts[:2] == ["cron", "jobs"]:
            try:
                payload = self.server.adapter.delete_cron_job(parts[2])
            except cronjobs.UnknownJob:
                return self._json(404, {"error": "no such job"})
            return self._json(200, payload)
        if len(parts) == 2 and parts[0] == "projects":
            try:
                payload = self.server.adapter.delete_project(parts[1])
            except KeyError:
                return self._json(404, {"error": "no such project"})
            return self._json(200, payload)
        return self._json(404, {"error": "not found"})
