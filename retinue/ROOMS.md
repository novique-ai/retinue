# Rooms — design (P1)

A **room** is a shared conversation between the user and N named agents. Each agent is a
Hermes **profile** (its own persona, model, memory, toolset); the room gives them one
transcript and a turn-taking rule. This is the core Retinue feature.

## Architecture

One **multiplexing gateway process** (`gateway.multiplex_profiles: true`) hosts every room
member: per-profile runtime scoping (`HERMES_HOME` contextvar + secret scope) already lets
N profiles with different models/SOULs live in one process. The room bus is the bundled
platform plugin `plugins/platforms/retinue_rooms/` — zero upstream-core edits, per
FORK-POLICY.

```
user ──HTTP──▶ RetinueRoomsAdapter ──MessageEvent(profile=member)──▶ gateway session
                 │   ▲                                                  (agent:<member>:retinue_rooms:group:<room>)
                 │   └── send() with metadata['notify'] = final reply
                 ├── room store: meta JSON + transcript JSONL
                 └── turn engine: mentions → queue → budget
```

- **Turn injection**: the adapter submits `MessageEvent`s via `handle_message()` with
  `source.profile = <member>`, `chat_type="group"`, `chat_id = room id`. Shared-group
  sessions get automatic `[speaker]` attribution from the gateway
  (`group_sessions_per_user=False` is forced in the adapter config).
- **Reply capture** (the A2A pattern): the adapter overrides `send()`; only sends whose
  `metadata` carries the gateway's `notify` marker (the documented final-reply marker)
  resolve the pending turn future. `on_processing_complete` resolves
  failures/cancellations promptly. `internal=True` events queue behind a busy turn
  instead of interrupting it.
- **Context delivery**: each member tracks `last_seen` transcript seq. On its turn it
  receives the newest message as `event.text` (gateway prefixes `[speaker]`) and the
  earlier unseen, attributed lines as `event.channel_context`; the room briefing (roster,
  rules) rides `event.channel_prompt`. Feeding context this way — instead of writing into
  the member's SessionDB — preserves the agent cache and provider prompt caching.

## Turn-taking (v1)

1. A user message mentions members with `@name` → each mentioned member responds, in
   mention order. No mention → the room's **lead** (default responder) takes the turn.
2. An agent reply is scanned for `@name` mentions of other members → they are appended to
   the turn queue (self-mentions and already-queued members are skipped).
3. **Budget**: at most `max_agent_turns` agent turns per user message (default 8). On
   exhaustion the room posts a system notice and waits for the user.
4. **Independent waves run concurrently.** Members scheduled together (a user's
   `@scout @editor`, or follow-ups collected from the previous wave) do not
   depend on each other's replies, so their turns run in parallel. Replies are
   appended in mention order. A later `@mention` of someone who just spoke is
   a new wave and waits. One user-message cycle still holds the room lock, so
   a second user message queues behind the current cycle.
5. Reply capture is per `(room, member)` so two in-flight speakers cannot
   steal each other's notify.

## Surfaces

The adapter runs a small stdlib HTTP server (default `127.0.0.1:8643`, the A2A bind-safety
convention: no `RETINUE_ROOMS_API_KEY` → localhost-only):

| Route | Purpose |
|---|---|
| `GET /health` | liveness (unauthenticated) |
| `GET /models` | workspace model presets a hire can choose from |
| `GET/POST /agents` | roster / hire (`{name, job, how, model?}` — `model` names a preset) |
| `GET/POST /rooms` | list / create (`{name, members[], lead?, max_agent_turns?}`) |
| `GET/DELETE /rooms/{id}` | inspect / remove |
| `POST /rooms/{id}/messages` | user speaks (`{text, from?}`) → 202, cycle runs async |
| `GET /rooms/{id}/transcript?since=N&wait=S` | poll (optionally long-poll) the transcript — CLI / fallback |
| `GET /rooms/{id}/stream?since=N` | SSE transcript (`event: messages`); `access_token` query accepted |
| `GET/POST /routines` | list / save a demonstration (`{name, room, since?, until?}`) |
| `GET/DELETE /routines/{slug}` | inspect / remove |
| `POST /routines/{slug}/run` | replay the prompts into `{room}` (waits each cycle) |
| `GET /workspace` | shared workspace-computer status + attach command |

`python -m plugins.platforms.retinue_rooms.cli` is the reference client (create / list /
send / watch / chat). The web UI consumes the SSE stream; the CLI keeps long-poll.

## Model presets (per-hire model selection)

By default a hire copies the **root config's `model:` block** — every new agent uses the
workspace's default provider. To offer a choice, drop preset files in
`$HERMES_HOME/retinue_models/`:

```yaml
# $HERMES_HOME/retinue_models/local.yaml
model:
  provider: custom
  model: my-served-model
  base_url: http://127.0.0.1:8000/v1
  api_key: "none"
```

Each `<name>.yaml` holds a literal `model:` block that is copied **verbatim** into the new
profile's config (comments included). `GET /models` lists them; the web UI's hire form shows
them as a dropdown next to "Workspace default"; `POST /agents` takes the preset via
`"model": "<name>"`. An unknown or malformed preset is a 400 and creates nothing.

Credentials: a hire seeds the profile's `.env` **and `auth.json`** from the workspace root,
so presets can target any provider the workspace owner has configured or OAuth-logged-into
(e.g. run `hermes auth login` once in the workspace, then hire agents onto that provider).

A hire is hot-registered into the live multiplexer (pairing store, busy-mode
snapshot, `served_profiles`) so the new agent can join a room **without a
gateway restart**. `POST /agents` returns `{online: true, activation: "online"}`
when that succeeded; if no gateway is running, `online` is false and the
profile comes up on the next `hermes gateway` start. Connect also rescans
disk profiles so a hire that landed while the gateway was down is picked up.

## State

`$HERMES_HOME/retinue_rooms/` (default profile's home): `<room>.json` meta (atomic
tmp+rename) and `<room>.transcript.jsonl` (append-only). Room members must exist as
profiles (`~/.hermes/profiles/<name>/`, or `default`); creation warns about unknown names.

## Env

| Var | Default | Meaning |
|---|---|---|
| `RETINUE_ROOMS_ENABLED` | unset | enable the platform (or set an API key) |
| `RETINUE_ROOMS_API_KEY` | unset | bearer auth; unset ⇒ localhost-only bind |
| `RETINUE_ROOMS_HOST` / `_PORT` | `127.0.0.1` / `8643` | bind address |
| `RETINUE_ROOMS_TURN_TIMEOUT` | `300` | seconds to wait for one agent turn |

## The workspace computer (P3)

Grok Bot's model, reproduced locally: **all of a workspace's agents share one persistent
container** — shared files and state are what make agent-to-agent handoffs cheap. Hermes'
Docker backend already runs on podman (upstream `find_docker()` falls back to `podman` on
PATH; force with `HERMES_DOCKER_BINARY`). What Retinue adds is the *sharing*: container
identity is normally per-profile, so the carried patch `TERMINAL_DOCKER_SHARED_CONTAINER_KEY`
(upstream feature request [#84671](https://github.com/NousResearch/hermes-agent/issues/84671))
keys it by workspace instead — every member attaches to the same long-lived container via
the existing cross-process reuse.

Gateway environment for workspace-computer mode:

```bash
TERMINAL_ENV=docker                              # container-backed terminals (podman auto-detected)
TERMINAL_DOCKER_SHARED_CONTAINER_KEY=<workspace> # ONE container for every member
TERMINAL_DOCKER_IMAGE=docker.io/library/python:3.12-slim   # or your workspace image
```

Live-verified 2026-08-12 (rootless podman 4.9.3, no docker binary on the host): scout wrote
`/root/retinue-proof.txt` inside the container; editor — a different profile — read and
appended to the same file in the same container; both proven host-side via `podman exec`,
with the container hostname matching the agents' reports. Only the terminal tool family runs
in the container; browser/file/MCP tools remain host-side (stricter per-agent isolation =
just omit the shared key).

## Deliberate v1 limits

No token-streaming into the room (finals only — the
adapter declares no message editing, so the gateway skips the stream consumer); approvals
degrade to the gateway's text fallback inside the member's turn; mention token = profile
name (display aliases later); transcript is plain text (media later).
