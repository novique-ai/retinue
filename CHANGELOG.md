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

- **Agent runtimes** (#218): a member is now hired onto a runtime, not just
  a model. **Hermes** (default, unchanged) or **Grok Build** — xAI's native
  agent harness around Grok 4.6, driven over ACP (`grok agent stdio`).
  Grok Build owns its whole tool loop; Retinue manages the session
  (native `session/load` resume across gateway restarts), streams tool
  activity onto the transcript as muted `kind: "tool"` lines, answers
  permission requests via a workspace-scoped policy
  (`workspace`/`read-only`/`always`, per-member overridable), maps
  Stop to `session/cancel`, and reports availability per runtime
  (`GET /runtimes`, `/health.runtimes`: available / not installed /
  login required / error). Reuses the operator's existing `grok login`
  (SuperGrok/xAI OAuth) token store; agent processes run under an
  isolated `GROK_HOME` so rooms never inherit personal MCP servers,
  skills, or an always-approve default. The Hermes `xai-oauth` model
  presets are unchanged and remain the "Grok as a model" path.
  Docs: `retinue/RUNTIMES.md`.

- Grok Build MCP bridge (#220): `$HERMES_HOME/grokbuild/mcp.json`
  declares stdio/http/sse MCP servers for Grok Build sessions, passed on
  `session/new` and `session/load`; invalid entries are skipped with a
  logged warning. The agent process env carries the member's
  `RETINUE_BROKER_TOKEN` so a broker-client MCP server can authenticate
  per member.

- Projects in the left pane drag-reorder (⋮⋮ + up/down), same chrome as
  rooms. Order lives in ``retinue_projects.json`` ``order`` — not in
  ``/sidebar``. Unfiled stays last and is not a project.

### Fixed

- Phone browsers no longer get the desktop three-pane squeezed into a sliver.
  Below 720px the sidebar is an overlay drawer (closed by default), the team
  home and room transcript are full width, and the composer stays pinned
  above the home indicator. Settings is a top-bar control, not a clipped
  "Settin" chip.

### Changed

- A timed-out or failed room turn speaks in the retainer's voice
  instead of only posting a system `did not reply` notice. Speak
  Replies can play it. The spoken line names the blocker when we
  know it (a yes/no the room must show, missing permission, missing
  file/path) instead of "ask me to continue". The system line still
  records the exact reason. Distinct from the empty-answer apology.
  Briefing and hire SOUL tell members to say they are blocked and
  stop rather than tool-loop until the budget dies.

- Hermes ``clarify`` now posts in the room (numbered choices) so the
  human can hear it and answer with 1 / the option / their own words.
  That answer resolves the prompt and does not start a second cycle.
  Stop and turn-timeout release a still-waiting clarify so the agent
  thread does not leak.

### Added

- **Broker identity.** Every command a room member's turn executes carries a
  per-turn HMAC credential (`RETINUE_BROKER_TOKEN`), minted by the gateway
  with a key outside the bind-mount, so the operator's host broker can tell
  members apart on a shared socket. Subshell-scoped injection: the token
  never persists into the next command or the next member's turn (new
  carried patch `tools/turn_env.py`, injection in `BaseEnvironment.execute`).

- **Governed retainers.** `PATCH /agents/{slug}` accepts `{"governed": true}`;
  a governed member's every turn in an ide room carries the operator's
  operating contract (`RETINUE_GOVERNED_CONTRACT` file) as the briefing's
  final, binding section. Fail closed: contract unreadable → the turn is
  refused with `governed contract unavailable (…)`, never run ungoverned.
  Sandbox rooms are out of scope.
- ide rooms pin the turn's session cwd to the room's `ide_path`, so
  `AGENTS.md`/`CLAUDE.md` project context loads for room members the same
  way it does for a host CLI session in that tree (new adapter hook
  `session_cwd_for`, consumed by the gateway's session binding).

- Room messages show a tiny Discord-style stamp next to the speaker
  (`Today at 3:14 PM`, `Yesterday at 11:08 PM`). Uses the existing
  `ts`. Closes #126.

- **Stop** in the room chrome (Escape too) cuts Speak Replies — the
  current clip and the queue — and aborts this room's in-flight cycle
  so the next line is a redirect. Unchecking Speak Replies also stops
  the clip that is already playing. Closes #120.

- Hold to talk includes the composer draft as a prefix, so tapping
  `@Patty` then speaking posts `@Patty …` and Patty takes the turn.
  The voice bar shows “Will send to @Patty” (or “Will include typed
  text”). Empty draft still goes to the lead. Closes #118.
- Spoken “at Claude” / “hey Ellie” / “Hi, Patty” / “Claude,” at the
  start of a voice take is rewritten to a live `@Handle` on the
  transcript so that member takes the turn. Mid-sentence “look at
  Patty” is left alone. A composer `@` still wins. Closes #121.
  ``Hey, Dave`` (comma after the cue) and a unique one-letter STT
  miss (`Mingus` → Mangus) also resolve.

- The room **lead** writes the itinerary (fenced `itinerary` block in
  their reply). The right pane is the user’s view and can still edit.
  The lead is briefed to author the outline — they do not wait for the
  pane. Refs #37.
- Pulsing orange ring on a working member’s icon (welcome cast, sidebar
  faces, in-room thinking row). Uses `GET /agents` `busy`, already
  polled every 2s. Solid ring when `prefers-reduced-motion`. Closes #22.

### Fixed

- A directed `@mention` no longer convenes the whole room. Follow-up
  speak-or-pass rounds ran after every user message regardless of how it
  was addressed, so `@Mangus can you get started on this?` gave one
  answer and then polled the other four members — two of whom restated
  what had just been said — burning the room's entire 8-turn budget on a
  question aimed at one person. Laps now run only for an undirected
  message. Closes #160.
- `max_followup_rounds` defaults to `0`. Room-wide laps are a deliberate
  mode, not the resting state of a room: each lap costs every member a
  turn out of the same budget the addressed member needs. Rooms opt in.
  The lead delegating with its own `@mention` is unaffected.

- Speak Replies reads a spoken script, not raw chat Markdown. The rooms
  `POST /tts` path was the one TTS surface that never called
  `tools.tts_text_normalize.prepare_spoken_text`, so every turn was read
  aloud with its ```` ```itinerary ```` card — title, where, and each
  `[doing]`/`[todo]`/`[done]` line. Since the card is a running recap of
  the whole thread, a normal cycle sounded like the room being read back
  from the beginning. A turn that is only a card now returns `204` and
  plays nothing instead of surfacing a TTS error. Closes #158.
- Underscore emphasis in the shared TTS cleaner now requires a word
  boundary, so `libs/email_automation/urgency.py` is no longer spoken as
  `emailautomation`. Affected every TTS path, not just rooms.

- **Hold to talk** is a real push-to-talk control. Android no longer
  opens the browser long-press menu (Back / Forward / Reload / Download)
  on a hold. The button captures the pointer so sliding off still stops
  recording; blur, a hidden tab, pointer cancel, and leaving the room
  also stop. Space holds PTT when you are not typing in the composer.
  Right-click does not start a take. Closes #115.
- Hire / Edit Voice listed roster slugs as option values, so picking
  `leo` stored `editor` and Speak replies 404'd at xAI. The picker now
  uses `GET /voice.available` (narrator ids). A non-narrator stored or
  env value is ignored; hire/patch of a staff slug is a 400. Closes #113.
- Every hire SOUL (not only one senior) teaches: if you are the room
  lead, you write the itinerary fence; if you are not, you do not.
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
