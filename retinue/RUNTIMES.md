# Agent runtimes

A Retinue member is defined by *who it is* (name, job, SOUL, persona) and by
*which agent runtime executes its turns*. Retinue supports two runtimes as
peers:

```
                     Retinue rooms
                (transcript · turn-taking · UI)
                          │
                 runtime dispatch (per member)
                          │
          ┌───────────────┴────────────────┐
          │                                │
   Hermes runtime                  Grok Build runtime
   the built-in agent loop         xAI's native agent harness
   any supported model provider    Grok 4.6
   tools in the room container     tools on this machine (ACP)
```

- **Hermes** (default) — the generic agent loop this project is built on.
  Pick any model preset (Claude, GPT, Grok via `xai-oauth`, local llama);
  terminal tools run inside the room's shared workspace container.
- **Grok Build** — Retinue delegates the *entire agent loop* to the
  [Grok Build](https://x.ai/) CLI over the Agent Client Protocol (ACP).
  Grok reasons, calls tools, inspects results, and iterates inside its own
  harness; Retinue manages the session, streams its activity into the room,
  answers its permission requests, and posts the final reply.

The runtime is chosen at hire (Hire panel → "Agent runtime") and recorded
in the member's `retinue-agent.json` (`runtime: "grok-build"`; absent =
Hermes). After hire, the roster (and Edit panel) grouped dropdown can
switch both axes: Hermes presets stay bare (`grok-4.5`); Grok Build
catalog ids are qualified (`grok-build:grok-4.5`). A cross-runtime pick
asks for confirmation — it resets sessions, and Grok Build tools run on
the host, not in the room container. Identity (slug, rooms, lead, voice,
SOUL) is unchanged.

## Two ways to run Grok — do not confuse them

| | Grok through Hermes | Grok Build runtime |
|---|---|---|
| Path | `Hermes loop → xai-oauth provider → Grok API` | `Retinue → grok agent stdio (ACP) → Grok Build harness` |
| Who runs the tool loop | Hermes (generic loop, Hermes tools) | Grok Build (xAI's own harness and tools) |
| Select via | model preset `grok-4.6` / `grok-4.5` | runtime `grok-build` + catalog id (`grok-4.6` / `grok-4.5`) |
| Tools run | in the room container | on the host, in the room's project tree |
| Best for | chat-shaped room members on Grok | long-horizon agentic work with Grok's native behavior |

A failure in the `xai-oauth` path (tool-call adaptation, streaming quirks,
reserved tool names) says nothing about Grok Build — they share nothing but
the model weights. The Hermes presets remain available and unchanged.

## Setup

1. **Install Grok Build** so the gateway user can run `grok`
   (`~/.grok/bin/grok` and `$PATH` are both checked; override with
   `RETINUE_GROKBUILD_BIN`).
2. **Log in once**: `grok login` as the gateway user. Retinue reuses that
   token store (`~/.grok/auth.json`, or `RETINUE_GROKBUILD_AUTH_PATH`) by
   pointing the agent process at it via `GROK_AUTH_PATH`. SuperGrok/X
   subscriptions work — no paid API key is required. Retinue never copies,
   parses tokens out of, or logs this file.
3. **Hire**: the Hire panel shows *Agent runtime: Hermes | Grok Build*.
   When Grok Build is not usable the option is visible but disabled with
   the reason (`not installed` / `login required` / `runtime error`) —
   also reported by `GET /runtimes` and in `/health` under `runtimes`.

### Configuration

| Env var | Meaning | Default |
|---|---|---|
| `RETINUE_GROKBUILD_BIN` | grok executable | `grok` on PATH, else `~/.grok/bin/grok` |
| `RETINUE_GROKBUILD_AUTH_PATH` | token store handed to the agent | `~/.grok/auth.json` |
| `RETINUE_GROKBUILD_APPROVAL` | `workspace` \| `read-only` \| `always` | `workspace` |
| `RETINUE_GROKBUILD_MODEL` | workspace default for `grok agent -m` when a member has no `runtime_model` | grok's default (grok-4.6) |
| `RETINUE_ROOMS_GROK_TURN_TIMEOUT` | per-turn budget (s) | ide-class (900s min) |
| `RETINUE_GROKBUILD_IDLE_SECS` | reap idle agent processes after | 1800 |
| `RETINUE_GROKBUILD_SANDBOX` | force a grok sandbox profile (see caveats) | unset |

Per-member overrides in `retinue-agent.json`: `grok_approval` (permission
mode) and `runtime_model` (Grok catalog id, e.g. `grok-4.5`). Changing
`runtime_model` or converting runtime drops that member's Grok sessions
in every room they are in — `session/load` would otherwise resume the
old `-m`.

### MCP servers (workspace-declared)

Grok Build sessions never inherit the operator's personal `~/.grok` MCP
config. To give room members MCP tools, declare servers in
`$HERMES_HOME/grokbuild/mcp.json`:

```json
{"servers": [
  {"name": "broker", "type": "stdio", "command": "/path/to/client",
   "args": ["--socket", "/run/broker.sock"], "env": {"K": "V"}},
  {"name": "docs", "type": "http", "url": "https://example/mcp",
   "headers": {"Authorization": "Bearer …"}}
]}
```

`type` is `stdio` (default), `http`, or `sse`. The list is passed on the
ACP wire in both `session/new` and `session/load`; an invalid entry or a
malformed file is skipped with a logged warning rather than taking the
runtime down. Changes apply to the member's **next new/resumed process**
(reset the session, or wait for the idle reap).

Each agent process also carries the member's broker identity
(`RETINUE_BROKER_TOKEN`, same HMAC scheme as container turns), which MCP
server child processes inherit — so a host-broker client declared here
can authenticate per member. Note the token is minted per *process*
(TTL 6h), not per turn; idle reaping keeps processes short-lived.

## How a Grok Build turn works

`plugins/platforms/retinue_rooms/grokbuild.py` manages one
`grok agent stdio` process per active (room, member), speaking ACP
(JSON-RPC over stdio, protocol v1):

- **Isolation** — the process runs with `GROK_HOME` pointed at
  `$HERMES_HOME/grokbuild/home`, whose `config.toml` disables the
  Claude/Cursor compat bridges. Room agents do NOT inherit the operator's
  personal MCP servers, skills, or an always-approve default.
- **Session** — `session/new` scoped to the room's working directory; the
  grok session id is persisted (`retinue_rooms/grok_sessions.json`) and a
  gateway restart resumes it with `session/load` — the transcript is never
  restuffed into a fresh process. "New session" in the room UI resets it.
- **Working directory** — ide rooms: the room's host tree (`ide_path`).
  Sandbox rooms: a dedicated per-room host folder
  (`retinue_rooms/grok_workspaces/<room>/`).
- **Isolated worktrees** (#223) — in a room with `worktree_repos`, the
  container overlays each room worktree over its place in `/workspace`;
  the host-native equivalent is enforced at the permission gate. The
  member's briefing points at the room's own checkout
  (`$HERMES_HOME/worktrees/<room>/<rel>`, branch `retinue/room/<room>`,
  git fully working — the worktree's gitdir pointer is host-native), that
  checkout is an explicitly allowed write root, and any tool call whose
  target resolves under the shadowed real repo (`<ide_path>/<rel>`) is
  declined with a redirect to the checkout — **reads included**, because
  the shadowed tree holds another branch's content. Residual risk: shell
  commands are opaque to the gate and could still name the real path; the
  briefing instruction plus the file-tool gate covers the common paths,
  and `always` mode bypasses the redirect like every other guard.
- **Streaming** — `session/update` events map to the room: assistant text
  chunks become the reply; `tool_call` / failures / policy rejections
  appear as muted `kind: "tool"` activity lines on the transcript
  ("⚙ Write `calc.py`"), visible live in the web UI. Grok's internal
  reasoning (`agent_thought_chunk`) is deliberately dropped — never
  shown, stored, or logged.
- **Completion / failure** — the prompt's `stopReason` maps to the normal
  room turn contract (`end_turn` → reply; `cancelled` → stopped;
  `refusal`, budget stops, process death, protocol errors → a
  did-not-reply notice naming the cause).
- **Cancel** — the room's Stop button sends `session/cancel`; the turn
  ends with `stopReason: cancelled`.

## Permissions

Room turns have no human watching a modal, so permission requests are
answered by policy (`grokbuild.decide_permission`), per approval mode:

| Mode | read-only tools | file edits | commands | anything else |
|---|---|---|---|---|
| `workspace` (default) | allow | allow **iff every target is inside the room's tree** (symlink-escape checked) | allow (run in the project cwd) | deny |
| `read-only` | allow | deny | deny | deny |
| `always` | allow | allow | allow | allow |

Every decision is per-request (`allow_once` / `reject_once` — never a
sticky `*_always` grant), and rejections are visible on the transcript.
`always` exists for trusted setups and must be chosen explicitly — it is
never the default.

**Trust note:** unlike Hermes room members, Grok Build's tools run on the
**host**, not in the room container. Shell commands approved under
`workspace` mode can, like any host shell, reach beyond the project tree.
Treat a Grok Build member with the same trust as an ide room. (Grok's own
`--sandbox` bwrap profiles are not used: under `agent stdio` on v0.2.93
they either fail open or kill the session; `RETINUE_GROKBUILD_SANDBOX`
passes one through if you want to experiment.)

## Health and troubleshooting

`GET /runtimes` (and `/health` → `runtimes`) reports per-runtime state:

- `available` — executable found, version readable, token store present
- `not_installed` — no `grok` executable (install, or set `RETINUE_GROKBUILD_BIN`)
- `auth_required` — no token store (run `grok login` as the gateway user)
- `error` — executable present but `--version` fails

Per-turn failures name their cause on the transcript. Useful facts:

- The first turn of a session is slow (agent boot + session hooks); later
  turns reuse the warm process.
- Known v0.2.93 quirk: Claude-compat SessionStart/Stop *lifecycle* hooks
  from `~/.claude/settings.json` run at grok session start/end even though
  the Retinue `GROK_HOME` config disables compat hooks (tool-level hooks
  are correctly disabled). Cost is once per session.
- Structured logs under the `retinue_rooms.grokbuild` logger: process
  lifecycle, session ids, stop reasons, permission decisions (tool titles
  only), durations, exit codes. Never tokens, prompts, or reasoning.

## Adding another runtime later

`plugins/platforms/retinue_rooms/runtimes.py` is the registry: id, label,
capability flags, health. The dispatch point is `adapter._agent_turn`;
everything above it (planning, budgets, transcript, stop, watermarks) is
runtime-agnostic. A future native runtime (another ACP agent, a dedicated
local agent) means: a module like `grokbuild.py`, a registry entry, and a
`turn_timeout_for` case — not a rewrite.
