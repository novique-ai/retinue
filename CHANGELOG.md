# Changelog

Notable changes to the **Retinue delta** (rooms, web UI, hire flow, and
fork policy). Inherited Hermes Agent releases are tracked
[upstream](https://github.com/NousResearch/hermes-agent).

This project does not yet cut versioned Retinue releases. Dates are
commit dates on `main`. The rooms plugin currently reports `0.1.0` in
`plugins/platforms/retinue_rooms/plugin.yaml`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Contributor-facing README, CONTRIBUTING, Code of Conduct, security
  reporting wrapper, development/architecture/roadmap docs, issue and
  PR templates, and a fast **Retinue delta** GitHub Actions workflow.

## 2026-08-13

### Added

- IDE-attached room design (`workspace=sandbox|ide`) locked; not shipped.
- Sidebar: edit / archive / delete rooms and bots, operator-named team
  separators, click-and-drag reorder. Layout in
  `$HERMES_HOME/retinue_sidebar.json`.
- Hold-to-talk voice in the room UI (`GET /voice`, `POST /rooms/{id}/audio`,
  `POST /tts`). Default backend is xAI STT/TTS; an OpenAI-compatible
  sidecar is supported.
- Versioned hire-time model presets (`grok-4.5`, `grok-4.6`). Legacy
  `grok` remains accepted.
- Separate turn budgets for cloud (default 300s) and local-LLM
  (default 1800s) members.

## 2026-08-12

### Added

- Rooms v1: shared attributed transcript, `@mention` turn-taking,
  per-user-message turn budget, parallel independent waves.
- Web UI (`retinue-web/`) served by the rooms adapter; three-field hire
  flow (name / job / how).
- Shared workspace computer via the
  `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` carried patch (podman or docker).
- Routines: save a room's user prompts and replay them.
- Hot-hire: `POST /agents` registers the new profile without a gateway
  restart.
- SSE transcript stream (`GET /rooms/{id}/stream`).
- Fork policy, pinned `retinue-base-*` tags, first upstream sync.

### Fixed

- Secondary-profile rooms-adapter declines stay on the quiet enablement
  path (no registry WARNING spam).
- Hired agents get a SOUL identity block so they do not introduce
  themselves as the Hermes engine.
