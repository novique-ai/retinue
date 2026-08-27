"""Grok Build agent runtime — ACP client + managed sessions (#218).

Grok Build (the ``grok`` CLI) exposes xAI's native agent harness over the
Agent Client Protocol: newline-delimited JSON-RPC on the stdio of
``grok agent stdio``.  This module drives that process so a room member
whose runtime is ``grok-build`` executes its whole turn — reasoning,
tool calls, verification — inside Grok Build's own loop.  Retinue does
NOT interpret tool calls or feed results back per turn; it observes
``session/update`` notifications, answers ``session/request_permission``
per an operator-controlled policy, and collects the final message.

Design points, each verified against grok v0.2.93 before this was built:

* **Isolation.** The child runs with ``GROK_HOME`` pointed at a
  Retinue-owned directory (``$HERMES_HOME/grokbuild/home``) whose
  ``config.toml`` disables the Claude/Cursor compat bridges, so room
  sessions do not inherit the operator's personal MCP servers, skills,
  or always-approve default.  Known v0.2.93 limitation: Claude-compat
  SessionStart/Stop *lifecycle* hooks still execute even with
  ``[compat.claude] hooks = false`` (tool-level hooks are correctly
  disabled); the cost is paid once per session, not per turn.
* **Auth.** ``GROK_AUTH_PATH`` points at the operator's existing Grok
  Build token store (``~/.grok/auth.json`` by default), so the
  SuperGrok/xAI OAuth login made through ``grok login`` is reused.
  Retinue never reads token values, never copies the file, and never
  logs it — a missing/invalid store surfaces as JSON-RPC error -32000
  ("Authentication required") on ``session/new``, which maps to the
  ``auth_required`` health state.
* **Sessions are native.** One ``grok agent stdio`` process per active
  (room, member); the grok session id is persisted so a gateway restart
  resumes via ``session/load`` instead of restuffing the transcript.
* **Cancellation** is ``session/cancel`` → the in-flight
  ``session/prompt`` returns ``stopReason: "cancelled"``.
* **Sandbox profiles are deliberately NOT used**: under ``agent stdio``
  a ``GROK_SANDBOX`` profile either fails open (unknown profile → runs
  unsandboxed) or kills the process on ``session/new`` (defined
  profile).  Safety rides the permission gate here instead.  The
  operator can still force one via ``RETINUE_GROKBUILD_SANDBOX``.

Hidden reasoning: ``agent_thought_chunk`` updates are received and
dropped.  They are never surfaced, stored, or logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

BIN_ENV = "RETINUE_GROKBUILD_BIN"
AUTH_PATH_ENV = "RETINUE_GROKBUILD_AUTH_PATH"
APPROVAL_ENV = "RETINUE_GROKBUILD_APPROVAL"
MODEL_ENV = "RETINUE_GROKBUILD_MODEL"
SANDBOX_ENV = "RETINUE_GROKBUILD_SANDBOX"
IDLE_ENV = "RETINUE_GROKBUILD_IDLE_SECS"

APPROVAL_WORKSPACE = "workspace"
APPROVAL_READ_ONLY = "read-only"
APPROVAL_ALWAYS = "always"
_APPROVAL_MODES = (APPROVAL_WORKSPACE, APPROVAL_READ_ONLY, APPROVAL_ALWAYS)

_DEFAULT_IDLE_SECS = 1800.0
_START_TIMEOUT = 60.0
_LOAD_TIMEOUT = 120.0
_CANCEL_GRACE = 20.0
_HEALTH_CACHE_SECS = 60.0

# ACP tool-call kinds that never mutate anything.
_SAFE_KINDS = {"read", "search", "fetch", "think", "plan"}
# Kinds whose targets are files; approval is scoped to the session cwd.
_FS_KINDS = {"edit", "write", "delete", "move"}

# rawInput keys that name filesystem targets across grok's tool variants.
_PATH_INPUT_KEYS = (
    "file_path",
    "target_file",
    "path",
    "file",
    "directory",
    "dest",
    "destination",
    "source",
)


class GrokBuildError(Exception):
    """Base for runtime failures the adapter turns into a failed turn."""


class GrokBuildUnavailable(GrokBuildError):
    """The grok executable is missing or the process could not start."""


class GrokBuildAuthRequired(GrokBuildError):
    """The token store is missing/invalid — the operator must ``grok login``."""


class GrokBuildProcessExit(GrokBuildError):
    """The agent process died while a request was in flight."""


# ── discovery / health ───────────────────────────────────────────────────


def grok_binary() -> Optional[str]:
    """Absolute path of the grok executable, or None."""
    override = (os.getenv(BIN_ENV) or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("grok")
    if found:
        return found
    fallback = os.path.expanduser("~/.grok/bin/grok")
    return fallback if os.path.isfile(fallback) else None


def auth_path() -> str:
    override = (os.getenv(AUTH_PATH_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.expanduser("~/.grok/auth.json")


def grok_home(home_dir: str) -> str:
    """Retinue-owned ``GROK_HOME``; created with compat bridges disabled.

    ``config.toml`` is written only when absent so operator edits stick
    (grok itself also appends marketplace state to this file).
    """
    root = os.path.join(home_dir, "grokbuild", "home")
    os.makedirs(root, exist_ok=True)
    cfg = os.path.join(root, "config.toml")
    if not os.path.isfile(cfg):
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(
                "# Written by Retinue (retinue_rooms.grokbuild). Room sessions must not\n"
                "# inherit the operator's personal harness config.  Known v0.2.93 gap:\n"
                "# Claude SessionStart/Stop lifecycle hooks run despite hooks=false.\n"
                "[compat.claude]\n"
                "skills = false\n"
                "mcps = false\n"
                "hooks = false\n"
                "agents = false\n"
                "rules = false\n"
                "[compat.cursor]\n"
                "skills = false\n"
                "mcps = false\n"
                "hooks = false\n"
                "agents = false\n"
                "rules = false\n"
            )
    return root


def _auth_store_has_credentials(path: str) -> bool:
    """Structural check only — token values are never read into logs."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return bool(data) if isinstance(data, dict) else False


_health_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def health(home_dir: str, *, force: bool = False) -> Dict[str, Any]:
    """Availability of the Grok Build runtime on this host.

    ``status``: ``available`` | ``not_installed`` | ``auth_required`` |
    ``error``.  Cheap by design (no agent launch): executable + version +
    token-store presence.  A live failure still surfaces per-turn with a
    specific reason.
    """
    now = time.time()
    if (
        not force
        and _health_cache["value"] is not None
        and now - _health_cache["at"] < _HEALTH_CACHE_SECS
    ):
        return dict(_health_cache["value"])

    binary = grok_binary()
    if not binary:
        value = {
            "status": "not_installed",
            "detail": "grok executable not found (install Grok Build, or set "
            f"{BIN_ENV})",
        }
    else:
        version = ""
        detail = "grok --version failed"
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            version = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
            ok = proc.returncode == 0
            if not ok:
                detail = f"grok --version exited {proc.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            ok = False
            detail = f"grok --version failed: {e}"
        if not ok:
            value = {"status": "error", "detail": detail}
        elif not _auth_store_has_credentials(auth_path()):
            value = {
                "status": "auth_required",
                "version": version,
                "detail": f"no credentials at {auth_path()} — run `grok login` "
                "as the gateway user",
            }
        else:
            value = {"status": "available", "version": version}
    _health_cache["at"] = now
    _health_cache["value"] = dict(value)
    return value


def _invalidate_health_cache() -> None:
    _health_cache["at"] = 0.0
    _health_cache["value"] = None


# ── approval policy ──────────────────────────────────────────────────────


def approval_mode(member_meta: Optional[Dict[str, Any]] = None) -> str:
    """Effective approval mode: member meta override, then env, then default.

    ``always`` is an explicit operator choice — it is never the default.
    """
    for candidate in (
        (member_meta or {}).get("grok_approval"),
        os.getenv(APPROVAL_ENV),
    ):
        raw = str(candidate or "").strip().lower().replace("_", "-")
        if raw in _APPROVAL_MODES:
            return raw
    return APPROVAL_WORKSPACE


def _tool_meta(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    meta = tool_call.get("_meta")
    if isinstance(meta, dict):
        tool = meta.get("x.ai/tool")
        if isinstance(tool, dict):
            return tool
    return {}


def _candidate_paths(tool_call: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for loc in tool_call.get("locations") or []:
        if isinstance(loc, dict) and isinstance(loc.get("path"), str):
            paths.append(loc["path"])
    raw = tool_call.get("rawInput")
    if isinstance(raw, dict):
        for key in _PATH_INPUT_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)
    meta_input = _tool_meta(tool_call).get("input")
    if isinstance(meta_input, dict):
        value = meta_input.get("path")
        if isinstance(value, str) and value.strip():
            paths.append(value)
    return paths


def _path_inside(path: str, root: str) -> bool:
    """True when *path* (resolved against *root*) stays under *root*.

    ``realpath`` on the DIRNAME so a symlink cannot smuggle a write out of
    the tree while a not-yet-existing target file still resolves.
    """
    if not root:
        return False
    return _inside(_resolve_candidate(path, root), root)


def _resolve_candidate(path: str, cwd: str) -> str:
    """Absolute, symlink-resolved form of a tool-call target path.

    Relative paths resolve against *cwd* — grok's tools run there — and
    the realpath is taken on the DIRNAME so a symlinked directory cannot
    smuggle a target out of a tree while a not-yet-existing file still
    resolves.
    """
    candidate = path if os.path.isabs(path) else os.path.join(cwd, path)
    parent_real = os.path.realpath(os.path.dirname(candidate) or cwd)
    return os.path.join(parent_real, os.path.basename(candidate))


def _inside(resolved: str, root: str) -> bool:
    """Containment for an already-resolved candidate (see _resolve_candidate)."""
    if not root:
        return False
    root_real = os.path.realpath(root)
    return resolved == root_real or resolved.startswith(root_real + os.sep)


def decide_permission(
    tool_call: Dict[str, Any],
    *,
    mode: str,
    cwd: str,
    mcp_server_names: frozenset = frozenset(),
    extra_roots: Tuple[str, ...] = (),
    denied_roots: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Answer one ``session/request_permission`` without human input.

    Room turns have no human watching an approval prompt (the same fact
    that motivated ``approvals.room_mode`` for Hermes turns), so the
    policy must resolve deterministically:

    * ``always``     — allow everything (explicit operator opt-in).
    * ``read-only``  — allow only tools flagged read-only / safe kinds.
    * ``workspace``  — allow safe kinds; allow file mutations whose every
      target stays inside the session cwd or an ``extra_roots`` tree;
      allow command execution (the command runs with the project as cwd —
      the same trust class as an ide room's shell, but on the HOST, which
      is why grok-build members are documented as ide-trust); allow MCP
      calls to servers the workspace ``mcp.json`` declares (declaring a
      server IS the operator's grant, #220); reject anything else,
      including any file mutation that names a path outside the tree.

    ``denied_roots`` (#223) maps real paths this room ISOLATES to the
    room's own checkout of the same repo — worktree rooms.  Any tool
    call whose target resolves under a denied root is declined with a
    reason that redirects to the room's checkout, reads included: the
    shadowed tree holds another branch's content, so a read there is a
    correctness hazard, not just a write hazard.  This is the host-native
    equivalent of the container's worktree overlay mount, enforced at the
    permission gate.  ``extra_roots`` are the worktree host paths — they
    live outside cwd, so writes there must be explicitly allowed.

    MCP calls arrive as grok's builtin ``use_tool``
    (``rawInput.tool_name = "<server>__<tool>"``); tool discovery is
    ``search_tool``.  Verified against grok v0.2.93.
    """
    meta = _tool_meta(tool_call)
    # The precise tool identity lives in _meta["x.ai/tool"]; the top-level
    # ACP kind is the coarse enum and DEGRADES to "other" on permission
    # requests (an MCP use_tool request arrives as kind "other" while its
    # meta kind/name still say use_tool — observed live on v0.2.93, and
    # exactly the shape the regression test pins).
    meta_kind = str(meta.get("kind") or "").lower()
    acp_kind = str(tool_call.get("kind") or "").lower()
    kind = meta_kind or acp_kind
    read_only = bool(meta.get("read_only"))
    title = str(tool_call.get("title") or meta.get("label") or kind or "tool")

    if mode == APPROVAL_ALWAYS:
        return True, "always-approve mode"
    # Isolation redirect BEFORE the read-only allow: a read of the
    # shadowed tree returns another branch's content (#223).
    if denied_roots:
        for raw_path in _candidate_paths(tool_call):
            resolved = _resolve_candidate(raw_path, cwd)
            for real, redirect in denied_roots.items():
                if _inside(resolved, real):
                    return False, (
                        f"{title}: {raw_path} is inside {real}, which this "
                        f"room isolates — use the room's own checkout at "
                        f"{redirect} instead"
                    )
    if (
        read_only
        or kind in _SAFE_KINDS
        or acp_kind in _SAFE_KINDS
        or kind == "search_tool"
    ):
        return True, "read-only tool"
    if mode == APPROVAL_READ_ONLY:
        return False, f"{title}: blocked by read-only approval mode"

    # workspace mode
    if kind == "execute" or acp_kind == "execute":
        return True, "command execution allowed in workspace mode"
    if kind == "use_tool" or str(meta.get("name") or "").lower() == "use_tool":
        raw = tool_call.get("rawInput")
        tool_name = str((raw or {}).get("tool_name") or "") if isinstance(raw, dict) else ""
        server = tool_name.split("__", 1)[0] if "__" in tool_name else ""
        if server and server in mcp_server_names:
            return True, f"workspace-declared MCP server '{server}'"
        return False, (
            f"{title}: MCP server {server or '?'!r} is not declared in this "
            f"workspace's grokbuild/mcp.json"
        )
    paths = _candidate_paths(tool_call)
    if kind in _FS_KINDS or acp_kind in _FS_KINDS or paths:
        if not paths:
            return False, f"{title}: no target path visible; declined"
        outside = [
            p
            for p in paths
            if not (
                _inside(_resolve_candidate(p, cwd), cwd)
                or any(
                    _inside(_resolve_candidate(p, cwd), root)
                    for root in extra_roots
                )
            )
        ]
        if outside:
            return False, (
                f"{title}: target outside the room workspace ({outside[0]})"
            )
        return True, "workspace-scoped file operation"
    return False, f"{title}: unrecognized mutating tool; declined"


# ── ACP wire client ──────────────────────────────────────────────────────


@dataclass
class TurnResult:
    stop_reason: str
    text: str
    usage: Dict[str, Any] = field(default_factory=dict)


class AcpProcess:
    """One ``grok agent stdio`` child speaking newline-delimited JSON-RPC.

    A single reader task routes: responses to pending request futures,
    ``session/update`` notifications to the active turn's callback, and
    server→client requests (permissions) to the permission handler.
    Unknown extension notifications (``_x.ai/*``) are ignored.  A
    malformed line is logged and skipped — one bad frame must not kill
    the session.
    """

    def __init__(self, home_dir: str, *, env_extra: Optional[Dict[str, str]] = None):
        self._home_dir = home_dir
        self._env_extra = dict(env_extra or {})
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: List[str] = []
        self._next_id = 0
        self._pending: Dict[Any, asyncio.Future] = {}
        self._on_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_permission: Optional[
            Callable[[Dict[str, Any]], Tuple[bool, str]]
        ] = None
        self.initialize_result: Dict[str, Any] = {}
        self.pid: Optional[int] = None

    # -- lifecycle --

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        binary = grok_binary()
        if not binary:
            raise GrokBuildUnavailable("grok executable not found")
        env = dict(os.environ)
        env["GROK_HOME"] = grok_home(self._home_dir)
        env["GROK_AUTH_PATH"] = auth_path()
        # GROK_SANDBOX fails open (or kills session/new) under agent
        # stdio on v0.2.93 — never inherit one from the gateway env.
        env.pop("GROK_SANDBOX", None)
        sandbox = (os.getenv(SANDBOX_ENV) or "").strip()
        if sandbox:
            env["GROK_SANDBOX"] = sandbox
        env.update(self._env_extra)
        argv = [binary, "agent"]
        model = (os.getenv(MODEL_ENV) or "").strip()
        if model:
            argv += ["-m", model]
        argv.append("stdio")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,  # never inherit the gateway's signal group
                # ACP frames carry whole diffs / tool outputs in one line;
                # the 64 KiB asyncio default truncates them and wedges the
                # stream.
                limit=16 * 1024 * 1024,
            )
        except OSError as e:
            raise GrokBuildUnavailable(f"failed to launch grok: {e}") from e
        self.pid = self._proc.pid
        self._reader = asyncio.ensure_future(self._read_loop())
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        try:
            self.initialize_result = await asyncio.wait_for(
                self.request(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "clientCapabilities": {
                            "fs": {"readTextFile": False, "writeTextFile": False}
                        },
                    },
                ),
                _START_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await self.close()
            raise GrokBuildUnavailable("grok agent did not answer initialize")
        logger.info(
            "grokbuild: agent up pid=%s version=%s",
            self.pid,
            (self.initialize_result.get("_meta") or {}).get("agentVersion", "?"),
        )

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        for task in (self._reader, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader = self._stderr_task = None
        self._fail_pending(GrokBuildProcessExit("agent process closed"))
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        if proc is not None:
            logger.info(
                "grokbuild: agent down pid=%s exit=%s", self.pid, proc.returncode
            )

    # -- wire --

    def bind_turn(
        self,
        on_update: Optional[Callable[[Dict[str, Any]], None]],
        on_permission: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]],
    ) -> None:
        self._on_update = on_update
        self._on_permission = on_permission

    def _send(self, msg: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise GrokBuildProcessExit("agent process is not running")
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.alive:
            raise GrokBuildProcessExit("agent process is not running")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await fut
        finally:
            self._pending.pop(rid, None)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text[:500])
                    del self._stderr_tail[:-20]
        except asyncio.CancelledError:
            return

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-5:])

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # A frame longer than the stream limit: skip it rather
                    # than killing the session.
                    logger.warning("grokbuild: oversized frame skipped pid=%s", self.pid)
                    continue
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.warning(
                        "grokbuild: malformed frame skipped pid=%s (%d bytes)",
                        self.pid,
                        len(line),
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._route(msg)
                except Exception:
                    logger.exception("grokbuild: event handler failed pid=%s", self.pid)
        except asyncio.CancelledError:
            return
        finally:
            exit_code = proc.returncode
            self._fail_pending(
                GrokBuildProcessExit(
                    f"grok agent exited (code={exit_code}); {self.stderr_tail()}".strip()
                )
            )

    def _route(self, msg: Dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:
            self._handle_server_request(msg)
        elif "method" in msg:
            method = msg.get("method") or ""
            if method == "session/update":
                cb = self._on_update
                if cb is not None:
                    cb(msg.get("params") or {})
            # _x.ai/* extension notifications are intentionally ignored.
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(_rpc_error(msg["error"]))
                else:
                    fut.set_result(msg.get("result") or {})

    def _handle_server_request(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method") or ""
        rid = msg["id"]
        if method == "session/request_permission":
            params = msg.get("params") or {}
            tool_call = params.get("toolCall") or {}
            allow, reason = False, "no permission handler bound"
            handler = self._on_permission
            if handler is not None:
                try:
                    allow, reason = handler(tool_call)
                except Exception:
                    logger.exception("grokbuild: permission handler failed")
                    allow, reason = False, "permission handler error"
            option_id = _pick_option(params.get("options") or [], allow)
            if option_id is None:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"outcome": {"outcome": "cancelled"}},
                    }
                )
                return
            logger.info(
                "grokbuild: permission %s (%s) — %s",
                "allowed" if allow else "rejected",
                str(tool_call.get("title") or "?")[:120],
                reason,
            )
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "outcome": {"outcome": "selected", "optionId": option_id}
                    },
                }
            )
        else:
            # fs/* and anything else we did not advertise.
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"{method} not supported"},
                }
            )


def _pick_option(options: List[Dict[str, Any]], allow: bool) -> Optional[str]:
    """Choose the once-scoped option matching the decision.

    Never *_always: the policy re-decides every request, so a sticky
    grant would widen future turns beyond what the policy said.
    """
    want = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in want:
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == kind:
                oid = opt.get("optionId")
                if isinstance(oid, str):
                    return oid
    return None


def _rpc_error(err: Any) -> GrokBuildError:
    if isinstance(err, dict):
        code = err.get("code")
        message = str(err.get("message") or "agent error")
        data = err.get("data")
        detail = f"{message}" + (f" ({data})" if data else "")
        if code == -32000 or "authentication required" in message.lower():
            return GrokBuildAuthRequired(detail)
        return GrokBuildError(f"agent error {code}: {detail}")
    return GrokBuildError(str(err))


# ── workspace MCP servers (#220) ─────────────────────────────────────────


def mcp_config_path(home_dir: str) -> str:
    return os.path.join(home_dir, "grokbuild", "mcp.json")


def mcp_servers(home_dir: str) -> List[Dict[str, Any]]:
    """Workspace-declared MCP servers for Grok Build sessions (#220).

    ``$HERMES_HOME/grokbuild/mcp.json``::

        {"servers": [
          {"name": "broker", "type": "stdio", "command": "/path/client",
           "args": ["--socket", "/run/broker.sock"], "env": {"K": "V"}},
          {"name": "docs", "type": "http", "url": "https://…",
           "headers": {"Authorization": "Bearer …"}}
        ]}

    Normalized to the ACP wire shape (stdio: ``{name, command, args,
    env: [{name, value}]}``; http/sse: ``{type, name, url, headers:
    [{name, value}]}`` — both verified against grok v0.2.93) and passed
    on ``session/new`` AND ``session/load``.  This is deliberately NOT
    the operator's personal ``~/.grok`` MCP config: room sessions only
    get what the workspace file declares.  A malformed file or entry is
    skipped with a warning — a typo must not take the runtime down, and
    the log names what was dropped.  No file = no servers (today's
    behavior).
    """
    path = mcp_config_path(home_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logger.warning("grokbuild: unreadable mcp config %s (%s); no MCP servers", path, e)
        return []
    entries = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        logger.warning("grokbuild: %s has no 'servers' list; no MCP servers", path)
        return []
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(entries):
        normalized = _normalize_mcp_entry(entry)
        if normalized is None:
            logger.warning("grokbuild: skipping invalid MCP server entry %d in %s", i, path)
        else:
            out.append(normalized)
    return out


def _normalize_mcp_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    kind = str(entry.get("type") or "stdio").strip().lower()
    if kind == "stdio":
        command = str(entry.get("command") or "").strip()
        if not command:
            return None
        args = entry.get("args") or []
        if not isinstance(args, list):
            return None
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            return None
        return {
            "name": name,
            "command": command,
            "args": [str(a) for a in args],
            "env": [{"name": str(k), "value": str(v)} for k, v in env.items()],
        }
    if kind in ("http", "sse"):
        url = str(entry.get("url") or "").strip()
        if not url:
            return None
        headers = entry.get("headers") or {}
        if not isinstance(headers, dict):
            return None
        return {
            "type": kind,
            "name": name,
            "url": url,
            "headers": [{"name": str(k), "value": str(v)} for k, v in headers.items()],
        }
    return None


def _member_env_extra(home_dir: str, member: str) -> Dict[str, str]:
    """Per-member env for the agent process (#220).

    Carries the broker identity token, so a broker-client MCP server
    declared in ``mcp.json`` (spawned by grok, inheriting its env) can
    authenticate to the host capability broker as this member — the
    host-native analog of the per-turn token container turns get.
    Best-effort: a workspace with no broker key simply mints nothing.
    Note the token TTL (6h) is per PROCESS here, not per turn; idle
    reaping keeps processes far shorter-lived in practice.
    """
    try:
        from . import brokertoken

        return {brokertoken.TOKEN_ENV: brokertoken.mint(home_dir, member)}
    except Exception:
        logger.debug("grokbuild: broker token mint failed", exc_info=True)
        return {}


# ── managed member sessions ──────────────────────────────────────────────


@dataclass
class _MemberSession:
    key: str
    process: AcpProcess
    session_id: str
    cwd: str
    last_used: float = field(default_factory=time.time)
    turn_active: bool = False
    # Names of the workspace MCP servers THIS session was started with —
    # the allow-list decide_permission grants use_tool against (#220).
    mcp_names: frozenset = frozenset()


class GrokBuildManager:
    """All live Grok Build sessions for one workspace (``home_dir``).

    Keyed by ``(room_id, member)``.  Session ids are persisted in
    ``$HERMES_HOME/retinue_rooms/grok_sessions.json`` so a gateway
    restart resumes conversations via ``session/load``.  Idle processes
    are reaped opportunistically (default 30 min); their sessions remain
    resumable.
    """

    def __init__(self, home_dir: str):
        self._home_dir = home_dir
        self._sessions: Dict[Tuple[str, str], _MemberSession] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}

    # -- persistence --

    def _state_path(self) -> str:
        return os.path.join(self._home_dir, "retinue_rooms", "grok_sessions.json")

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        path = self._state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)

    def _remember(self, key: Tuple[str, str], session_id: str, cwd: str) -> None:
        state = self._load_state()
        state["|".join(key)] = {"session_id": session_id, "cwd": cwd}
        self._save_state(state)

    def _forget(self, key: Tuple[str, str]) -> None:
        state = self._load_state()
        if state.pop("|".join(key), None) is not None:
            self._save_state(state)

    def _recall(self, key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        entry = self._load_state().get("|".join(key))
        return entry if isinstance(entry, dict) else None

    # -- session acquisition --

    def _lock(self, key: Tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def _acquire(
        self, room_id: str, member: str, cwd: str
    ) -> Tuple[_MemberSession, bool]:
        """Live session for (room, member); second value = fresh session.

        Fresh means the grok session has no prior context, so the caller
        must lead with the full briefing.  A resumed (``session/load``)
        session is NOT fresh.
        """
        key = (room_id, member)
        await self._reap_idle(exclude=key)
        sess = self._sessions.get(key)
        if sess is not None and sess.process.alive and sess.cwd == cwd:
            sess.turn_active = True  # claimed before another key's reap can look
            return sess, False
        if sess is not None:
            await self._drop(key)

        process = AcpProcess(
            self._home_dir, env_extra=_member_env_extra(self._home_dir, member)
        )
        await process.start()
        mcp = mcp_servers(self._home_dir)
        remembered = self._recall(key)
        if remembered and remembered.get("session_id") and remembered.get("cwd") == cwd:
            sid = str(remembered["session_id"])
            try:
                await asyncio.wait_for(
                    process.request(
                        "session/load",
                        {"sessionId": sid, "cwd": cwd, "mcpServers": mcp},
                    ),
                    _LOAD_TIMEOUT,
                )
                sess = _MemberSession(
                    key="|".join(key),
                    process=process,
                    session_id=sid,
                    cwd=cwd,
                    mcp_names=frozenset(s["name"] for s in mcp),
                )
                self._sessions[key] = sess
                logger.info(
                    "grokbuild: resumed session %s for %s/%s", sid, room_id, member
                )
                sess.turn_active = True
                return sess, False
            except GrokBuildAuthRequired:
                await process.close()
                raise
            except (GrokBuildError, asyncio.TimeoutError) as e:
                logger.warning(
                    "grokbuild: resume of %s failed (%s); starting fresh", sid, e
                )
        try:
            result = await asyncio.wait_for(
                process.request("session/new", {"cwd": cwd, "mcpServers": mcp}),
                _LOAD_TIMEOUT,
            )
        except (GrokBuildError, asyncio.TimeoutError):
            await process.close()
            raise
        sid = str(result.get("sessionId") or "")
        if not sid:
            await process.close()
            raise GrokBuildError("session/new returned no sessionId")
        sess = _MemberSession(
            key="|".join(key),
            process=process,
            session_id=sid,
            cwd=cwd,
            mcp_names=frozenset(s["name"] for s in mcp),
        )
        self._sessions[key] = sess
        self._remember(key, sid, cwd)
        logger.info("grokbuild: new session %s for %s/%s", sid, room_id, member)
        sess.turn_active = True  # claimed by the caller before the lock drops
        return sess, True

    async def _drop(self, key: Tuple[str, str]) -> None:
        sess = self._sessions.pop(key, None)
        if sess is not None:
            await sess.process.close()

    async def _reap_idle(self, exclude: Optional[Tuple[str, str]] = None) -> None:
        idle_cap = _env_float(IDLE_ENV, _DEFAULT_IDLE_SECS)
        now = time.time()
        for key, sess in list(self._sessions.items()):
            if key == exclude or sess.turn_active:
                continue
            if not sess.process.alive or now - sess.last_used > idle_cap:
                logger.info(
                    "grokbuild: reaping idle session %s/%s (pid=%s)",
                    key[0],
                    key[1],
                    sess.process.pid,
                )
                await self._drop(key)

    # -- turns --

    async def run_turn(
        self,
        room_id: str,
        member: str,
        cwd: str,
        *,
        build_prompt: Callable[[bool], str],
        approval: str,
        timeout: float,
        on_activity: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        extra_roots: Tuple[str, ...] = (),
        denied_roots: Optional[Dict[str, str]] = None,
    ) -> TurnResult:
        """One member turn, executed entirely inside Grok Build's loop.

        ``build_prompt(fresh)`` supplies the prompt text — the full
        briefing for a fresh session, the delta otherwise.  ``approval``
        names the policy mode for this turn's permission requests.
        ``on_activity(event, payload)`` receives user-appropriate events:
        ``tool_start`` / ``tool_done`` / ``tool_failed`` / ``rejected``.
        Thought chunks are dropped, never forwarded.
        """
        key = (room_id, member)
        async with self._lock(key):
            # _acquire returns with turn_active already claimed, so an idle
            # reap triggered from another member's turn cannot close this
            # process between acquisition and the prompt below.
            sess, fresh = await self._acquire(room_id, member, cwd)
            chunks: List[str] = []
            # Message chunks before and after a tool run are separate
            # assistant messages; joined bare they read as one run-on
            # sentence. A break is queued whenever tool activity lands
            # between chunks.
            pending_break = [False]

            def on_update(params: Dict[str, Any]) -> None:
                if params.get("sessionId") not in (None, sess.session_id):
                    return
                update = params.get("update") or {}
                utype = update.get("sessionUpdate")
                if utype == "agent_message_chunk":
                    content = update.get("content") or {}
                    if content.get("type") == "text":
                        if pending_break[0] and chunks:
                            chunks.append("\n\n")
                        pending_break[0] = False
                        chunks.append(str(content.get("text") or ""))
                elif utype == "tool_call":
                    pending_break[0] = True
                    if on_activity is not None:
                        on_activity("tool_start", _activity_payload(update))
                elif utype == "tool_call_update":
                    pending_break[0] = True
                    if on_activity is not None:
                        status = str(update.get("status") or "")
                        if status == "completed":
                            on_activity("tool_done", _activity_payload(update))
                        elif status == "failed":
                            on_activity("tool_failed", _activity_payload(update))
                # agent_thought_chunk: hidden reasoning — deliberately dropped.

            def on_permission(tool_call: Dict[str, Any]) -> Tuple[bool, str]:
                allow, reason = decide_permission(
                    tool_call,
                    mode=approval,
                    cwd=cwd,
                    mcp_server_names=sess.mcp_names,
                    extra_roots=extra_roots,
                    denied_roots=denied_roots,
                )
                if not allow and on_activity is not None:
                    on_activity(
                        "rejected",
                        {
                            "title": str(tool_call.get("title") or "tool"),
                            "reason": reason,
                        },
                    )
                return allow, reason

            sess.process.bind_turn(on_update, on_permission)
            prompt_text = build_prompt(fresh)
            started = time.time()
            request = asyncio.ensure_future(
                sess.process.request(
                    "session/prompt",
                    {
                        "sessionId": sess.session_id,
                        "prompt": [{"type": "text", "text": prompt_text}],
                    },
                )
            )
            watcher: Optional[asyncio.Task] = None
            if cancel_event is not None:

                async def watch_cancel() -> None:
                    await cancel_event.wait()
                    self._request_cancel(sess)

                watcher = asyncio.ensure_future(watch_cancel())
            try:
                try:
                    result = await asyncio.wait_for(request, timeout)
                except asyncio.TimeoutError:
                    # Ask the agent to stop, then give it a short grace to
                    # return `cancelled` before the process is dropped.
                    self._request_cancel(sess)
                    try:
                        result = await asyncio.wait_for(request, _CANCEL_GRACE)
                    except (asyncio.TimeoutError, GrokBuildError):
                        await self._drop(key)
                        raise GrokBuildError(
                            f"turn exceeded {int(timeout)}s and did not cancel"
                        ) from None
                stop = str(result.get("stopReason") or "unknown")
                meta = result.get("_meta") or {}
                usage = {
                    k: meta.get(k)
                    for k in ("inputTokens", "outputTokens", "totalTokens", "modelId")
                    if meta.get(k) is not None
                }
                logger.info(
                    "grokbuild: turn done %s/%s stop=%s dur=%.1fs session=%s",
                    room_id,
                    member,
                    stop,
                    time.time() - started,
                    sess.session_id,
                )
                return TurnResult(stop_reason=stop, text="".join(chunks), usage=usage)
            finally:
                if watcher is not None:
                    watcher.cancel()
                request.cancel()
                sess.turn_active = False
                sess.last_used = time.time()
                if sess.process.alive:
                    sess.process.bind_turn(None, None)

    def _request_cancel(self, sess: _MemberSession) -> None:
        try:
            sess.process.notify("session/cancel", {"sessionId": sess.session_id})
        except GrokBuildError:
            pass

    async def cancel(self, room_id: str, member: str) -> None:
        """Cancel an in-flight turn (Stop button). The turn's own
        ``session/prompt`` request resolves with ``stopReason: cancelled``."""
        sess = self._sessions.get((room_id, member))
        if sess is not None and sess.turn_active:
            self._request_cancel(sess)

    async def reset(self, room_id: str, member: str) -> None:
        """Drop the member's session AND its persisted id — next turn is
        a genuinely new conversation (the room transcript re-briefs it)."""
        key = (room_id, member)
        async with self._lock(key):
            await self._drop(key)
            self._forget(key)

    async def shutdown(self) -> None:
        for key in list(self._sessions):
            await self._drop(key)
        self._locks.clear()


def _activity_payload(update: Dict[str, Any]) -> Dict[str, Any]:
    meta = _tool_meta(update)
    return {
        "title": str(update.get("title") or meta.get("label") or "tool")[:200],
        "kind": str(update.get("kind") or meta.get("kind") or "")[:32],
        "tool_call_id": str(update.get("toolCallId") or "")[:80],
    }


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(30.0, float(raw))
    except (ValueError, TypeError):
        return default


# ── sandbox-room workspace dirs ──────────────────────────────────────────


def sandbox_workspace_dir(home_dir: str, room_id: str) -> str:
    """Host working directory for a grok-build member in a sandbox room.

    Sandbox rooms give Hermes members a container with no host mount;
    the grok equivalent is a dedicated per-room host directory.
    """
    path = os.path.join(home_dir, "retinue_rooms", "grok_workspaces", room_id)
    os.makedirs(path, exist_ok=True)
    return path
