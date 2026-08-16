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

from . import attachments, auth, engine, hire, ide, itinerary, keepalive, principal, routines, sidebar, voice, workspace
from .engine import KIND_AGENT, KIND_SYSTEM, KIND_USER, Room, RoomMessage
from .store import RoomStore

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8643
_MAX_BODY = 262_144  # 256 KB is plenty for a chat message
_MAX_AUDIO = 8 * 1024 * 1024  # 8 MiB ≈ 4 min of 16 kHz mono WAV
_DEFAULT_USER_NAME = "User"


class AgentBusy(ValueError):
    """Raised when a model switch would evict a mid-turn agent."""

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
        self._pending: Dict[tuple[str, str], _PendingTurn] = {}  # (room, member)
        self._pending_lock = threading.Lock()
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
        self._start_xai_keepalive()
        logger.info("Retinue rooms: serving on %s:%s", self.host, self.port)
        return True

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
        self._stop_xai_keepalive()
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
        self.store.append(
            room_id,
            RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=speaker, text=body),
        )
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
        ide.apply_workspace_fields(room, workspace=workspace, ide_path=ide_path, touching_path=True)
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
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name:
                raise ValueError("room name is required")
            room.name = name
            touched = True
        if "members" in body:
            members = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
            if not members:
                raise ValueError("a room needs at least one agent member")
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
        if "workspace" in body or "ide_path" in body:
            ide.apply_workspace_fields(
                room,
                workspace=body["workspace"] if "workspace" in body else room.workspace,
                ide_path=body.get("ide_path") if "ide_path" in body else room.ide_path,
                touching_path="ide_path" in body,
            )
            touched = True
        if not touched:
            raise ValueError("nothing to update")
        self.store.update(room)
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
        busy = self.busy_slugs()
        for agent in agents:
            slug = str(agent.get("slug") or "")
            agent["team"] = team_of.get(slug)
            agent["busy"] = slug in busy
        auth.annotate_agents(self._home_dir(), agents)
        agents.sort(key=lambda a: (order.get(str(a.get("slug") or ""), 10_000), str(a.get("slug") or "")))
        return agents

    def busy_slugs(self) -> set:
        """Profile names that currently have an in-flight room turn."""
        with self._pending_lock:
            return {member for (_room, member) in self._pending}

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
        self, name: str, job: str, how: str, model: Optional[str] = None
    ) -> Dict[str, Any]:
        try:
            hire.ensure_bundled_cloud_presets(self._home_dir())
        except Exception:
            logger.debug("Retinue rooms: preset seed on hire failed", exc_info=True)
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
    ) -> Dict[str, Any]:
        """STT then the normal user-message cycle. Transcript is the room line."""
        text = voice.transcribe_dispatch(data, filename)
        result = self.post_user_message(room_id, text, from_name)
        result["text"] = text
        return result

    def save_routine_from_room(
        self, name: str, room_id: str, since: int = 0, until: Optional[int] = None
    ) -> Dict[str, Any]:
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        prompts = routines.user_prompts_from_messages(
            self.store.read_since(room_id, 0), since=since, until=until
        )
        return routines.save_routine(self._home_dir(), name, prompts, source_room=room_id)

    def run_routine(self, slug: str, room_id: str) -> Dict[str, Any]:
        meta = routines.get_routine(self._home_dir(), slug)
        if meta is None:
            raise KeyError(slug)
        if self.store.get(room_id) is None:
            raise KeyError(room_id)
        speaker = f"routine:{slug}"
        ran = []
        for prompt in meta.get("messages") or []:
            ran.append(
                self.post_user_message(room_id, prompt, speaker, wait=True)
            )
        return {"slug": slug, "room": room_id, "steps": ran}

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
        with ide.apply_room_workspace(room, self._home_dir()):
            await self._run_cycle_workspace(room, user_message)

    async def _run_cycle_workspace(self, room: Room, user_message: RoomMessage) -> None:
        room_id = room.id
        budget = room.max_agent_turns
        names = self._display_names(room)
        queue = engine.plan_user_turns(room, user_message.text, names)
        spoken: List[str] = []
        turns_taken = 0
        while queue:
            if turns_taken >= budget:
                self._post_system(
                    room_id, engine.cycle_budget_notice(budget, queue)
                )
                break
            wave, queue = engine.take_wave(queue, budget - turns_taken)
            if not wave:
                break
            # One speaker at a time. Their reply is on the transcript
            # before the next member starts, so reviewers see the draft.
            member = wave[0]
            room = self.store.get(room_id) or room
            ok, reply = await self._agent_turn(room, member)
            turns_taken += 1
            spoken.append(member)
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
            if not ok or not (reply or "").strip():
                reply = engine.fallback_reply(ask)
            self.store.append(
                room_id,
                RoomMessage(seq=0, ts=0, kind=KIND_AGENT, speaker=member, text=reply),
            )
            queue.extend(
                engine.merge_followups(
                    room, [(member, reply)], queue, spoken, budget - turns_taken, names
                )
            )

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
        with self._pending_lock:
            stale = self._pending.pop(key, None)
            if stale is not None and not stale.future.done():
                stale.future.set_result((False, "superseded by a newer turn"))
            self._pending[key] = _PendingTurn(
                task_id=task_id, room_id=room.id, member=member, future=fut
            )

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

        # Mark the delta delivered before dispatch; on failure the trigger is
        # re-shown next turn only if new messages arrive — acceptable v1.
        # touch_last_seen merges under the store lock so parallel members
        # cannot clobber each other's cursor.
        self.store.touch_last_seen(room.id, member, delta[-1].seq)
        room.last_seen[member] = max(room.last_seen.get(member, 0), delta[-1].seq)

        token = _turn_member.set(member)
        try:
            await self.handle_message(event)
        except Exception as e:
            self._resolve_pending(
                room.id, ok=False, text=f"dispatch failed: {e}", member=member
            )
        finally:
            _turn_member.reset(token)

        budget = hire.turn_timeout_for(self._home_dir(), member)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=budget)
        except asyncio.TimeoutError:
            self._resolve_pending(room.id, ok=False, text="turn timed out", member=member)
            return False, f"no reply within {int(budget)}s"

    def _post_system(self, room_id: str, text: str) -> None:
        try:
            self.store.append(
                room_id, RoomMessage(seq=0, ts=0, kind=KIND_SYSTEM, speaker="room", text=text)
            )
        except Exception:
            logger.exception("Retinue rooms: failed to post system notice to %s", room_id)


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
        "health",
        "routines",
        "workspace",
        "voice",
        "tts",
        "sidebar",
        "auth",
        "principal",
    )

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts == ["health"]:
            adapter = self.server.adapter
            return self._json(
                200,
                auth.health_payload(
                    adapter._home_dir(),
                    len(adapter.store.list_rooms()),
                ),
            )
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
        if parts == ["routines"]:
            return self._json(200, {"routines": routines.list_routines(adapter._home_dir())})
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
            return self._json(200, voice.status())
        if parts == ["sidebar"]:
            return self._json(200, adapter.get_sidebar())
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
        try:
            result = adapter.post_user_audio(
                room_id,
                raw,
                filename=self._filename_for_audio(parsed),
                from_name=from_name,
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
        try:
            audio = voice.synthesize_dispatch(text, speaker)
        except voice.VoiceError as e:
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
                    workspace=body.get("workspace"),
                    ide_path=body.get("ide_path"),
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
        if parts == ["routines"]:
            try:
                payload = adapter.save_routine_from_room(
                    name=str(body.get("name") or ""),
                    room_id=str(body.get("room") or body.get("room_id") or ""),
                    since=int(body.get("since") or 0),
                    until=(int(body["until"]) if body.get("until") is not None else None),
                )
            except KeyError:
                return self._json(404, {"error": "no such room"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except FileExistsError as e:
                return self._json(409, {"error": f"a routine named '{e}' already exists"})
            return self._json(201, payload)
        if len(parts) == 3 and parts[0] == "routines" and parts[2] == "run":
            try:
                payload = adapter.run_routine(
                    parts[1], str(body.get("room") or body.get("room_id") or "")
                )
            except KeyError as e:
                return self._json(404, {"error": f"not found: {e}"})
            except RuntimeError as e:
                return self._json(503, {"error": str(e)})
            return self._json(202, payload)
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
        if len(parts) == 2 and parts[0] == "routines":
            if routines.delete_routine(self.server.adapter._home_dir(), parts[1]):
                return self._json(200, {"deleted": parts[1]})
            return self._json(404, {"error": "no such routine"})
        return self._json(404, {"error": "not found"})
