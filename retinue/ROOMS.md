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
4. Turns are strictly sequential per room (an asyncio lock); one agent speaks at a time.
   A user message that arrives mid-cycle starts its cycle after the current one finishes.

## Surfaces

The adapter runs a small stdlib HTTP server (default `127.0.0.1:8643`, the A2A bind-safety
convention: no `RETINUE_ROOMS_API_KEY` → localhost-only):

| Route | Purpose |
|---|---|
| `GET /health` | liveness (unauthenticated) |
| `GET/POST /rooms` | list / create (`{name, members[], lead?, max_agent_turns?}`) |
| `GET/DELETE /rooms/{id}` | inspect / remove |
| `POST /rooms/{id}/messages` | user speaks (`{text, from?}`) → 202, cycle runs async |
| `GET /rooms/{id}/transcript?since=N&wait=S` | poll (optionally long-poll) the transcript |

`python -m plugins.platforms.retinue_rooms.cli` is the reference client (create / list /
send / watch / chat). The P2 web UI replaces it as the human surface; this HTTP API is
what the web room view will consume (SSE upgrade planned then).

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

## Deliberate v1 limits

Sequential turns (no parallel speakers); no streaming into the room (finals only — the
adapter declares no message editing, so the gateway skips the stream consumer); approvals
degrade to the gateway's text fallback inside the member's turn; mention token = profile
name (display aliases later); transcript is plain text (media later).
