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

## Phase 4 — Routines + workspace take-over (v1)
- **Routines = skill + schedule**: saving a room demonstration creates a
  per-retainer skill (the how) and can attach an editable Hermes cron job (the
  when, who, and destination). The rooms UI lists every served profile's jobs,
  including raw reminders, with edit, pause, resume, run-now, and delete controls.
  The clock remains the existing Hermes cron store and multiplex ticker.
- **Workspace take-over (status)**: `GET /workspace` reports the shared
  podman/docker computer (label `hermes-profile=<TERMINAL_DOCKER_SHARED_CONTAINER_KEY>`)
  and the attach command. Full noVNC screen take-over is the next increment.

## Voice (v1 testable 2026-08-13)

Transcript-preserving hold-to-talk in the room UI. Live default is Track A
(xAI STT/TTS). Track B is a self-hosted LAN sidecar (whisper.cpp + piper). Detail
and flip instructions: [VOICE.md](VOICE.md). Bead `infra-ivl9.2` CLOSED.

## Sidebar (`infra-ivl9.3`)

Edit / archive / delete rooms and bots, operator-named team separators,
and click-and-drag reorder. Layout lives in `$HERMES_HOME/retinue_sidebar.json`
(not only localStorage). Archive hides without wiping a transcript or
profile; delete confirms and never touches the default profile. Persona
edits rewrite SOUL + meta in place (same slug, no restart).

NoVNC (`infra-dfc1`) stays a separate gated increment.

## IDE-attached rooms — shipped (option A)

A room is **sandbox** (default, isolated container) or **IDE** (opt-in).
Both use the workspace-computer container runtime. IDE mode bind-mounts a
host path on the machine running the gateway (`ide_path` /
`RETINUE_IDE_ROOT`) at `/workspace`. Not SSHFS, not a remote-IDE protocol.
UI copy: Isolated container vs This machine’s IDE, with a confirm checkbox
before attach. Only IDE-marked rooms get the mount; each room has its own
container key so the mount cannot leak into a sandbox room.

If the browser and the IDE tree live on different machines, install the
gateway on the IDE host and browse from the other.
