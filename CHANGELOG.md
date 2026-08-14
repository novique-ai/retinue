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

- The room **lead** writes the itinerary (fenced `itinerary` block in
  their reply). The right pane is the user’s view and can still edit.
  The lead is briefed to author the outline — they do not wait for the
  pane. Refs #37.
- Pulsing orange ring on a working member’s icon (welcome cast, sidebar
  faces, in-room thinking row). Uses `GET /agents` `busy`, already
  polled every 2s. Solid ring when `prefers-reduced-motion`. Closes #22.

### Fixed

- Sidebar **Rename** remints the team id from the new name (so a bar
  labeled Development is not still `cloud`). New teams get a slug id.
- A member “thinking” in one room no longer appears after you switch
  rooms. RoomView is keyed by room id so draft, send, and thinking stay
  with that transcript. Closes #47.
- Room turns are sequential. Mentioned members used to start as one
  concurrent wave, so reviewers ran against an empty draft. The queue is
  now mention order, then follow-up `@mention`s; each reply is on the
  transcript before the next speaker starts. Closes #17.
- `@Sheila` and `@slug` now address the same member. Composer chips and
  an `@` picker insert the display / first name; the scheduled member is
  still the slug. Ambiguous prefixes do not steal a turn. Closes #8.
- Hire SOUL and the per-turn room briefing teach members to `@`call a
  teammate by display name, then stop, instead of "say so briefly."
  Existing profiles get the rule from the briefing. Closes #21.
- Live `@` chips in the transcript; mentions inside fenced code are
  literal copy and do not schedule a turn. Headings and lists still
  hand off. Closes #20.
- Model dropdown greys out while that member is mid-turn. A `PATCH`
  that would evict them returns 409. Closes #28.
- Workspace files named in a reply appear in the room. Images render
  inline; other `/workspace` paths become download links. Closes #19.

### Fixed

- Reauth modal now polls every 2s and closes on approve. A leftover
  `last_auth_error` (or hired profiles with no local xAI block) no
  longer keeps the workspace at `relogin_required` / `missing` after a
  successful device-code login. Refs #18.

### Added

- Settings menu for Grok, Claude, and Codex logins, plus voice,
  workspace, and hire presets. Claude takes an API key; Grok and Codex
  use in-product device-code Sign in. Bundled `claude-sonnet` and
  `openai-codex` hire presets. Closes #40. Closes #41.
- Routines saved from a room show up in that room's chrome (name,
  step count, Run) so you do not hunt the global sidebar. Closes #39.
- Composer `+` (and drag-and-drop) attaches files and images to a
  room send. They are stored with the room and shown as
  `/workspace/uploads/…` chips in the transcript. Closes #38.
- Slide-out room itinerary. The lead keeps a short living outline
  (title, where we are, todo/doing/done steps). Persisted server-side;
  the user can open the right pane and edit it. The lead's turn briefing
  includes the current outline. Closes #37.
- Idle xAI OAuth keepalive. While a hired cloud member uses `xai-oauth`
  and the workspace grant is ok, rooms refresh that grant shortly
  before the access JWT expires (same Hermes skew as on-turn refresh).
  Terminal `invalid_grant` lights the existing Reauth banner. No
  second rotating copy in profile `auth.json`. Closes #34.
- In-product provider reauth when a cloud grant dies. `GET /health`
  and `GET /agents` expose `ok` / `relogin_required` / `missing`; the
  rooms UI shows a banner and a **Reauth** control that runs Hermes
  device-code login against this workspace. Success evicts cached
  agents and drops profile-local xAI token copies so they inherit the
  workspace grant. No gateway restart. Refs #18.
- IDE-attached rooms (`workspace=sandbox|ide`). Same podman/docker
  runtime; IDE rooms bind-mount `ide_path` / `RETINUE_IDE_ROOT` at
  `/workspace`. Loud UI confirm. Per-room container keys so sandbox
  rooms never inherit the mount. Closes #13.
- Contributor-facing README, CONTRIBUTING, Code of Conduct, security
  reporting wrapper, development/architecture/roadmap docs, issue and
  PR templates, and a fast **Retinue delta** GitHub Actions workflow.
- GitHub Issues, Discussions, label taxonomy, and a `main` ruleset
  requiring the Retinue delta check. Starter issues #1–#9 and Ideas
  discussions #10–#12.

## 2026-08-13

### Added

- IDE-attached room design (`workspace=sandbox|ide`) locked; shipped in Unreleased.
- Sidebar: edit / archive / delete rooms and bots, operator-named team
  separators, click-and-drag reorder. Layout in
  `$HERMES_HOME/retinue_sidebar.json`.
- Hold-to-talk voice in the room UI (`GET /voice`, `POST /rooms/{id}/audio`,
  `POST /tts`). Default backend is xAI STT/TTS; an OpenAI-compatible
  sidecar is supported.
- Versioned hire-time cloud model presets. The legacy unversioned id
  remains accepted.
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
