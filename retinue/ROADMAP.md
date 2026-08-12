# Retinue roadmap

## Phase 0 — Repo & fork strategy ✅
Public fork under `novique-ai/retinue`, upstream remote + pinned-base tag model, plugin-delta policy ([FORK-POLICY.md](FORK-POLICY.md)), this scaffold.

## Phase 1 — Rooms ✅ (v1)
The core new product logic: a room is a shared transcript that N agents (Hermes profiles) and the user participate in together. Agents see the same conversation and take turns — mention-based addressing plus an orchestrator default (a "chief of staff" agent routes work when nobody is addressed directly). Built as a platform adapter on the gateway's typed event stream and multi-profile multiplexing; agents keep their own persona (SOUL.md), memory, model, and toolset.

## Phase 2 — Web UI ✅ (v1)
A native web chat client (`retinue-web/`, served by the rooms adapter itself — same
origin, no CORS), with:
- room view (agents' turns visually distinct, tool activity summarized),
- a three-field **hire flow** — name, primary job, how it should work — that templates a profile: persona file, model choice, toolset,
- agent management (roster, memory inspection, model switching).

## Phase 3 — Podman execution ✅ (v1)
- A `podman` execution-environment backend alongside the existing local/docker/ssh/... backends.
- **Workspace computer** mode: one long-lived container per workspace that every agent's terminal targets — the team shares files and state the way it would on one machine.
- Optional stricter mode: per-agent containers.

## Phase 4 — Later
- **Routines**: demonstrate a task once, save it, re-run on a schedule.
- **Take-over view**: watch an agent's screen and take the controls (e.g., to perform a login), via VNC into the workspace container.
