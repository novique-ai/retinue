# Retinue architecture

This page is the public map of how Retinue is put together. Design detail
for rooms lives in [retinue/ROOMS.md](../retinue/ROOMS.md). How the fork
tracks Hermes lives in [retinue/FORK-POLICY.md](../retinue/FORK-POLICY.md).

Do not treat this file as a substitute for reading those two.

## What Retinue is

Retinue is **not** a from-scratch agent runtime. It is a thin product
layer on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent):

- Hermes owns the agent loop, tools, skills, memory, providers, and the
  multiplexing gateway.
- Retinue owns **rooms** (a shared multi-agent transcript + turn-taking),
  a **web UI** for hiring staff and talking in those rooms, and a small
  set of **carried patches** that unblock that product.

The load-bearing rule: **the Retinue delta stays plugin-shaped**.
Upstream core files are not edited to land features.

## Process shape

```
browser  ──HTTP/SSE──▶  RetinueRoomsAdapter (:8643)
                              │
                              │  MessageEvent(profile=member)
                              ▼
                     Hermes gateway process
                     (gateway.multiplex_profiles = true)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         profile A       profile B       profile C
         (SOUL, model,   (SOUL, model,   (SOUL, model,
          memory,         memory,         memory,
          toolset)        toolset)        toolset)
```

One gateway process hosts every room member. Each member is a Hermes
**profile** (its own persona file, model block, memory, toolset). The
room is the shared transcript and the turn-taking rule — not a second
agent runtime.

The adapter binds `127.0.0.1:8643` by default. Setting
`RETINUE_ROOMS_API_KEY` enables bearer auth and is required before the
bind is allowed to leave localhost.

## Where the code lives

| Piece | Path | Notes |
|---|---|---|
| Rooms platform plugin | `plugins/platforms/retinue_rooms/` | Adapter, turn engine, hire, sidebar, voice, routines, workspace status, tests |
| Web UI | `retinue-web/` | React 19 + Vite + TypeScript. Built to `retinue-web/dist/`, served by the adapter |
| Product docs | `retinue/`, `docs/` | Roadmap, rooms design, voice notes, fork policy |
| Carried patches | `agent/prompt_builder.py`, `tools/environments/docker.py` | Listed in the fork policy; drift-guarded by `test_carried_patches.py` |

`retinue-web` is **not** an npm workspace of the root `package.json` on
purpose. The root workspace glob is `apps/*`; putting the SPA there
rewrites upstream `package-lock.json`. Keep it top-level.

## Turn-taking (v1, summary)

1. A user message that `@mention`s members queues those members, in
   mention order. No mention → the room **lead** answers.
2. An agent reply is scanned for `@name` mentions of other members;
   those names are appended to the queue.
3. Independent names in the same wave run concurrently. A later mention
   of someone who just spoke is a new wave.
4. `max_agent_turns` (default 8) is a hard cap per user message.

Final replies only — the room does not currently stream tokens into the
transcript. Approvals fall back to the gateway's text path inside the
member's turn.

## Workspace computer

Hermes already runs terminal commands in Docker, and `find_docker()`
already falls back to `podman` on `PATH`. Retinue adds *sharing*: the
carried `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` makes every room member
attach to one long-lived container instead of one container per
profile.

Only the terminal tool family runs in that container today.
Browser / file / MCP tools remain host-side unless you change Hermes
config.

A later increment (not shipped) adds two room kinds: `sandbox`
(isolated container) and `ide` (same container plus an opt-in bind-mount
of a host path on the machine running the gateway).

## State on disk

Default contributor home is `HERMES_HOME=~/.retinue` so a Retinue
checkout does not collide with a stock Hermes `~/.hermes`.

| Path | What |
|---|---|
| `$HERMES_HOME/config.yaml`, `.env`, `auth.json` | Workspace defaults + credentials |
| `$HERMES_HOME/profiles/<slug>/` | One hired agent (SOUL, config, memory) |
| `$HERMES_HOME/retinue_rooms/` | Per-room meta JSON + append-only transcript JSONL |
| `$HERMES_HOME/retinue_sidebar.json` | Room order, team separators, agent order |
| `$HERMES_HOME/retinue_models/` | Hire-time model presets (seeded from the plugin) |

## Providers

Retinue does not reimplement LLM providers. A hire copies a **model
preset** (a `model:` YAML block) into the new profile. Bundled cloud
presets live in `plugins/platforms/retinue_rooms/model_presets/`. Local
or LAN presets are operator-owned because they carry a `base_url`.

Naming a preset `grok-4.5` / `grok-4.6` is interoperability with the
xAI API, not an affiliation claim. See the README affiliation section.

## What this page is not

- It is not a list of every Hermes subsystem. Use
  [upstream docs](https://hermes-agent.nousresearch.com/docs/).
- It is not an operator runbook for a specific host. Private install
  topology does not belong here.
