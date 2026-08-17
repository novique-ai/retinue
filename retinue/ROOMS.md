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
- **Idle xAI keepalive**: while any hired cloud member uses `xai-oauth` and the
  workspace grant is `ok`, a daemon tick calls Hermes'
  `resolve_xai_oauth_runtime_credentials(refresh_if_expiring=True)` against the
  workspace `auth.json` (context-local `HERMES_HOME`, never a profile copy).
  Same lifetime-aware skew as on-turn refresh. Terminal `invalid_grant` stays
  on the existing Reauth banner. Disable with `RETINUE_XAI_KEEPALIVE_SECONDS=0`.
- **Hermes cron → room**: a member can schedule a one-shot with the inherited
  `cronjob` tool (`deliver=origin`). The multiplex ticker fires that
  profile's cron store. The rooms adapter appends the reply to the
  transcript as the member (`thread_id`). It does not start a new mention
  cycle. Progress sends without `job_id` stay off the transcript.

## Turn-taking (v1)

1. A user message mentions members with `@name` → each mentioned member responds, in
   mention order. No mention → the room's **lead** (default responder) takes the turn.
2. An agent reply is scanned for `@name` mentions of other members → they are appended to
   the turn queue (self-mentions and already-queued members are skipped).
3. **Budget**: at most `max_agent_turns` agent turns per user message (default 8). On
   exhaustion the room posts a system notice and waits for the user.
4. **Turns are sequential.** The queue is mention order, then follow-up
   `@mention`s from each reply. A speaker finishes and their reply is on the
   transcript before the next speaker starts, so a reviewer sees the draft.
   One user-message cycle still holds the room lock, so a second user message
   queues behind the current cycle. An explicit "run these in parallel"
   control is later; it is not the default.
5. Reply capture is per `(room, member)` so two in-flight speakers cannot
   steal each other's notify.
6. **Rooms run concurrently with each other.** Sequencing is per room, not
   gateway-wide: a slow turn in one room no longer blocks the others. Each
   cycle binds its workspace (container key + mounts) to a ContextVar rather
   than to process env — see `tools/workspace_context.py` — so overlapping
   cycles cannot see each other's container. Until this landed, the key
   travelled through `os.environ` and every cycle had to serialize behind one
   process-wide lock, which meant a single local-model turn could hold the
   whole gateway for the full turn timeout.

## Surfaces

The adapter runs a small stdlib HTTP server (default `127.0.0.1:8643`, the A2A bind-safety
convention: no `RETINUE_ROOMS_API_KEY` → localhost-only):

| Route | Purpose |
|---|---|
| `GET /health` | liveness (unauthenticated) plus `auth.providers[]` (`ok` / `relogin_required` / `missing`) |
| `GET /auth` | workspace provider auth + in-flight reauth session |
| `POST /auth/reauth` | start Hermes device-code login (`{provider?}` = `xai-oauth` or `openai-codex`) → `{user_code, verification_url}` |
| `POST /auth/apikey` | save a workspace API key (`{provider:anthropic, api_key}`) into `$HERMES_HOME/.env` |
| `GET /auth/reauth?session=` | poll that login (`pending` / `approved` / `error` / `expired`) |
| `GET /models` | workspace model presets a hire can choose from (versioned; an unversioned cloud id is hidden once versioned files exist) |
| `GET/POST /agents` | roster / hire (`{name, job, how, model?}` — `model` names a preset) |
| `GET/PATCH /agents/{slug}` | inspect / edit (`{name?, job?, how?, model?, archived?}`) — SOUL rewrite in place; `model` still switches the preset. No restart. |
| `DELETE /agents/{slug}` | remove `profiles/<slug>/` (never `default`); evicts the live registration |
| `GET/POST /rooms` | list / create (`{name, members[], lead?, max_agent_turns?, workspace?, ide_path?, shared_mode?}`) — `workspace` is `sandbox` (default) or `ide`; `shared_mode` is `ro` (default) or `rw`. List is sidebar-ordered and includes `archived` |
| `GET/PATCH/DELETE /rooms/{id}` | inspect / edit (`{name?, members?, lead?, archived?, max_agent_turns?, workspace?, ide_path?, shared_mode?}`) / remove. Archive hides without wiping the transcript. |
| `GET /rooms/{id}/routines` | routines whose `source_room` is this room |
| `GET/PUT /rooms/{id}/itinerary` | living outline. The **lead** authors it (fenced `itinerary` block in their reply). The user can view/edit the right pane. |
| `GET/PUT /sidebar` | room order + team separators + agent order (`{rooms[], items:[{kind:team,id,label}|{kind:agent,slug}]}`) |
| `POST /rooms/{id}/messages` | user speaks (`{text, from?}`) → 202, cycle runs async |
| `POST /rooms/{id}/attachments` | raw file body + `filename=` query → `{path:/workspace/uploads/…}` (composer `+`) |
| `GET /rooms/{id}/transcript?since=N&wait=S` | poll (optionally long-poll) the transcript — CLI / fallback |
| `GET /rooms/{id}/stream?since=N` | SSE transcript (`event: messages`); `access_token` query accepted |
| `GET /rooms/{id}/files?path=` | Bytes of a `/workspace/…` file from that room's computer (images inline in the UI) |
| `GET/POST /routines` | list / save a demonstration (`{name, room, since?, until?}`). `source_room` is set on save; the room chrome lists matching routines. |
| `GET/DELETE /routines/{slug}` | inspect / remove |
| `POST /routines/{slug}/run` | replay the prompts into `{room}` (waits each cycle) |
| `GET /workspace` | workspace-computer status + attach command + shared-folder report (`shared_dir`, `shared_mount`, `shared_error`) |
| `GET /voice` | STT/TTS backend status (`xai` or OpenAI-compat sidecar) |
| `POST /rooms/{id}/audio` | hold-to-talk: raw audio → STT → same cycle as `/messages` |
| `POST /tts` | `{text, speaker?}` → audio/mpeg (or wav); per-slug voice map |

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

Shipped cloud presets live next to the plugin (`model_presets/`) and are
seeded into `$HERMES_HOME/retinue_models/` on connect if missing. A legacy
unversioned cloud preset is promoted to a versioned filename (never
overwritten) and hidden from `GET /models` once the versioned files exist;
`POST /agents` still accepts the unversioned id. `PATCH /agents/{slug}`
rewrites only the profile's `model:` block and evicts that member's cached
AIAgent so cloud staff can move between versioned presets without a
hand-edit or gateway restart. Local / LAN presets stay operator-owned
(they carry a host `base_url`).

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
`$HERMES_HOME/retinue_sidebar.json` holds room order and the agent/team list
(team membership is the nearest preceding separator). Archive flags live on
the room meta / `retinue-agent.json`; they hide an entry without deleting it.

## Env

| Var | Default | Meaning |
|---|---|---|
| `RETINUE_ROOMS_ENABLED` | unset | enable the platform (or set an API key) |
| `RETINUE_ROOMS_API_KEY` | unset | bearer auth; unset ⇒ localhost-only bind |
| `RETINUE_ROOMS_HOST` / `_PORT` | `127.0.0.1` / `8643` | bind address |
| `RETINUE_ROOMS_TURN_TIMEOUT` | `300` | seconds to wait for one **cloud** agent turn |
| `RETINUE_ROOMS_LOCAL_TURN_TIMEOUT` | `1800` | seconds to wait for one **local-LLM** turn (covers a slow first token and a sibling queued on the same llama-server) |
| `RETINUE_SHARED_DIR` | unset | absolute host path mounted at `/shared` in every room container. Unset = off (no mount, no directory created). Must already exist; a missing path is an error, not a silent create. |

## The workspace computer (P3)

The shared-computer model, reproduced locally: **all of a workspace's agents share one persistent
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

**`TERMINAL_ENV=docker` is a precondition, not a default.** The rooms adapter
refuses to start without it and says so, rather than setting it for you. It is
process-wide and read at ~30 sites across the engine, so a room cycle writing
it would move every other platform sharing the gateway onto the container
backend without asking. If `connect()` fails with a `terminal_backend` error,
set it on the gateway process — a systemd unit is the usual place.

Live-verified 2026-08-12 (rootless podman 4.9.3, no docker binary on the host): scout wrote
`/root/retinue-proof.txt` inside the container; editor — a different profile — read and
appended to the same file in the same container; both proven host-side via `podman exec`,
with the container hostname matching the agents' reports. Only the terminal tool family runs
in the container; browser/file/MCP tools remain host-side (stricter per-agent isolation =
just omit the shared key).

## Workspace modes (`sandbox` | `ide`)

Two room kinds, **same container runtime** (option A). Terminal tools still
run in podman/docker. File/browser/MCP tools stay host-side (unchanged).

| `workspace` | Computer | Who |
|---|---|---|
| `sandbox` (default) | Isolated container, no host mount | Every room unless marked IDE |
| `ide` | Same runtime + bind-mount of a **host path** at `/workspace` | Only rooms created/patched with `workspace=ide` |

Composer **+** attachments live in the room catalog and are served as
`/workspace/uploads/<name>`. Each turn bind-mounts that folder at
`/workspace/uploads` (and copies into an already-running container) so
members can open the files. Image paths on the user line are also passed
as inbound media for vision.

IDE attach is **local to the gateway host**. The web UI folder picker sets
`ide_path` on create/patch (type a path or browse under `RETINUE_IDE_ROOT`).
`GET /workspace/folders?path=` lists immediate subdirectories, scoped to that
root when it is set. It is not SSHFS and not a remote-IDE protocol. If the
browser and the IDE tree live on different machines, install the gateway on
the machine that owns the tree and browse the UI from the other.

Each room gets its own `TERMINAL_DOCKER_SHARED_CONTAINER_KEY`
(`retinue-<mode>-<room-id>`) so a sandbox room cannot share a container — or a
mount — with an IDE room. The web UI asks for a loud confirm before creating
or switching a room to `ide`.

That key is also the **environment cache key**, not just the container's
identity label. Upstream caches one environment per process under `"default"`
and builds it on first use, so before #16 the container created for whichever
room spoke first was handed back to every later turn: a member who had worked
in a sandbox room kept writing into that sandbox container when they next
spoke in an IDE room, and only a gateway restart cleared it. Keying the cache
by the room's container string keeps one long-lived container per room
instead of one per process. Carried patch in `tools/terminal_tool.py`
(`_resolve_container_task_id`); per-session isolation and RL/benchmark
overrides still outrank it, and callers with no room overlay still get
`"default"`.

Default cwd inside the container is `/workspace` (the isolated tree, or the
mounted host path).

## Shared folder (`/shared`)

A workspace-level host directory that every room can reach, sandbox and `ide`
alike. It is a deliberate hole in per-room isolation (issue #67) — opt-in,
never on by accident.

**Off unless configured.** If `RETINUE_SHARED_DIR` is unset, nothing is
mounted, nothing is created, and the container looks exactly as it did
before. Set it to an **existing** absolute host path to turn the feature on:

```bash
RETINUE_SHARED_DIR=/var/lib/retinue/shared
```

The path is resolved the same way as `RETINUE_IDE_ROOT` (`abspath` +
`expanduser`). If the path is set but is not a directory, the gateway does
**not** create it and does **not** skip the mount: `GET /workspace` reports
the error in `shared_error`, and a room cycle that would have mounted it
fails loudly.

**Mount path is `/shared`, not `/workspace/shared`.** For an `ide` room,
`/workspace` is a bind-mount of the user's real project tree, so
`/workspace/shared` would drop a foreign directory inside their source. `/shared`
is the same path in both room kinds, which is what makes it explainable to
an agent.

`GET /workspace` reports:

| field | meaning |
|---|---|
| `shared_dir` | resolved host path, or `null` if unset |
| `shared_mount` | `/shared` when configured, else `null` |
| `shared_error` | why the path cannot be mounted, or `null` |

**Default mode is read-only.** A room's `shared_mode` is `ro` or `rw`.
Absent or unknown values are treated as `ro`. The API rejects any other
value (it is not silently coerced). Opt a room into writes with
`shared_mode: "rw"` on create or patch. Read-only is the default because
this is a workspace-wide surface: one room writing into it is visible to
every other room, and that should be an explicit choice.

**Members are told about it.** When the folder is configured, the per-turn
briefing gains a line naming `/shared` and saying whether this room may write
there. A mount nobody is told about is a mount nobody uses — and an agent that
does not know a path is read-only discovers it as a terminal error mid-task.
When the folder is unset, the briefing is unchanged.

## Deliberate v1 limits

No token-streaming into the room (finals only — the
adapter declares no message editing, so the gateway skips the stream consumer); approvals
degrade to the gateway's text fallback inside the member's turn; mention
token is the slug, the unique display / first name, or a unique alias prefix
(ambiguous prefixes do not steal a turn); `/workspace` paths in a reply
are served by `GET /rooms/{id}/files` and shown inline (images) or as
downloads.
