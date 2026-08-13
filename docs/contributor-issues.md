# Starter issues for contributors

These are grounded in the current tree (rooms plugin, web UI, fork
docs). They are drafts for the maintainer to file on GitHub once
**Issues are enabled** on `novique-ai/retinue`. Do not treat this file
as the issue tracker.

Suggested labels use the taxonomy in the open-source readiness notes
(`good first issue`, `help wanted`, `bug`, `enhancement`,
`documentation`, `ui/ux`, `linux`, `wayland`, `packaging`,
`ai-provider`).

---

## Good first issues

### 1. Helpful page when `retinue-web/dist` is missing

- **Labels:** `good first issue`, `ui/ux`, `enhancement`
- **Why:** `RetinueRoomsAdapter._serve_static` returns a JSON 404 if
  `retinue-web/dist/` was not built. A new contributor who started the
  gateway without `npm run build` sees a blank error, not a next step.
- **Files:** `plugins/platforms/retinue_rooms/adapter.py`
  (`_serve_static`, the 404 branch around the static handler)
- **Acceptance:**
  - With no `dist/`, `GET /` returns `text/html` explaining to run
    `cd retinue-web && npm run build` (or `./scripts/retinue-dev-setup.sh`).
  - With `dist/` present, the SPA is unchanged.
  - A unit test covers both branches.

### 2. Show Retinue commit in the web UI footer

- **Labels:** `good first issue`, `ui/ux`, `enhancement`
- **Why:** Bug reports need a commit. `hermes version` is the Hermes
  package version, not this fork's SHA.
- **Files:** `plugins/platforms/retinue_rooms/adapter.py` (`GET /health`
  or a small `GET /version`), `retinue-web/src/App.tsx`,
  `retinue-web/src/api.ts`
- **Acceptance:**
  - Health or version payload includes `git rev-parse --short HEAD`
    when `.git` is available, else a clear `"unknown"`.
  - Footer (or a discreet settings line) shows that string.
  - No network call at import time; fail soft if git is missing.

### 3. Keyboard reorder for sidebar rooms and agents

- **Labels:** `good first issue`, `ui/ux`, `enhancement`
- **Why:** Sidebar order is drag-and-drop only
  (`retinue-web/src/App.tsx` `draggable` / `parseDrag`). Keyboard and
  screen-reader users cannot reorder.
- **Files:** `retinue-web/src/App.tsx`, `retinue-web/src/styles.css`
- **Acceptance:**
  - Focused room or agent has move-up / move-down controls (buttons or
    documented keys) that call the existing `PUT /sidebar` layout.
  - Drag-and-drop still works.
  - Controls are labelled (`aria-label` or visible text).

### 4. Public-safe pass over `retinue/VOICE.md`

- **Labels:** `good first issue`, `documentation`
- **Why:** The voice design note still contains a private-install
  example (host names, unit drop-ins, sidecar paths) and at least one
  stale claim that `retinue-web` has zero voice code. Hold-to-talk
  already shipped.
- **Files:** `retinue/VOICE.md`
- **Acceptance:**
  - Operator-private hostnames, IPs, and unit paths are removed or
    replaced with generic examples (`RETINUE_VOICE_BACKEND=openai`,
    `RETINUE_VOICE_BASE_URL=http://127.0.0.1:8104/v1`).
  - The document matches the shipped `GET /voice` / hold-to-talk UI.
  - No new undocumented backends are invented.

### 5. Banner on inherited translated READMEs

- **Labels:** `good first issue`, `documentation`
- **Why:** `README.es.md`, `README.zh-CN.md`, and `README.ur-pk.md`
  still describe Hermes Agent as if this repository *is* upstream.
- **Files:** those three READMEs
- **Acceptance:**
  - Each file opens with a short notice (in that language if you can)
    that this tree is **Retinue**, with a link to `README.md`.
  - Upstream Hermes content below the banner is otherwise untouched.

---

## Help wanted (medium)

### 6. First-run empty state in the web UI

- **Labels:** `help wanted`, `ui/ux`, `enhancement`
- **Why:** A fresh `$HERMES_HOME` has no hired agents and no rooms.
  The mention bar and lead copy assume a room already exists.
- **Files:** `retinue-web/src/App.tsx`
- **Acceptance:**
  - Zero agents → hire panel is the obvious next step, with one-line
    copy about the three-field brief.
  - Agents but zero rooms → create-room panel is the obvious next step.
  - Existing rooms still open as they do today.

### 7. Collect rooms tests from the default pytest invocation

- **Labels:** `help wanted`, `enhancement`
- **Why:** `pyproject.toml` `testpaths = ["tests"]` is upstream and
  must stay that way (fork policy). Rooms tests therefore live next to
  the plugin and are invisible to bare `pytest`.
- **Files:** a new `tests/plugins/platforms/retinue_rooms/` shim *or*
  a documented collector that does not edit `pyproject.toml`
- **Acceptance:**
  - `pytest` (no args) plus the existing `-m 'not integration'` addopts
    also runs the rooms suite **or** a thin wrapper under `tests/`
    imports it.
  - `scripts/retinue-check.sh` still works.
  - No change to upstream `testpaths`.

### 8. Wayland / pipewire notes for hold-to-talk

- **Labels:** `help wanted`, `linux`, `wayland`, `documentation`
- **Why:** Voice capture is `getUserMedia` in the browser. On Linux
  that fails in ways that look like a Retinue bug (Flatpak/browser
  permission, PipeWire vs Pulse, Chromium vs Firefox).
- **Files:** `docs/development.md` or a short `docs/linux-voice.md`,
  maybe a line in the bug template
- **Acceptance:**
  - Documents the permission surfaces that were actually reproduced
    (name the distro + browser). Mark unverified distros as TODO.
  - Does not claim a native desktop app that does not exist.

### 9. Display-name mentions without changing slugs

- **Labels:** `help wanted`, `enhancement`, `ui/ux`
- **Why:** [retinue/ROOMS.md](../retinue/ROOMS.md) v1 limit: mention
  token = profile name. Editing a display name does not change `@slug`.
- **Files:** `plugins/platforms/retinue_rooms/engine.py`,
  `retinue-web/src/App.tsx`
- **Acceptance:**
  - `@DisplayName` and `@slug` both address the same member.
  - Self-mentions and already-queued members still skip.
  - Tests cover the alias table.
  - Slug remains the stable id (no rename-in-place).

### 10. Packaging sketch: install the rooms UI without a git checkout

- **Labels:** `help wanted`, `packaging`, `linux`
- **Why:** Today the adapter resolves `retinue-web/dist` relative to
  the source tree. A pip-only install has no UI.
- **Files:** `plugins/platforms/retinue_rooms/adapter.py`
  (`web_dist_dir`), possibly package data in a Retinue-only path
- **Acceptance:**
  - Documented search order: env override, then source-tree `dist/`,
    then a well-known prefix.
  - A test with a fake prefix.
  - No new packaging format invented in the same PR (no Flatpak yet).

---

## Larger projects (discuss first)

Open a Discussion before writing one of these.

### 11. Token streaming into the room transcript

- **Labels:** `enhancement`, `ui/ux`
- **Why:** v1 posts finals only. The adapter declares no message
  editing, so the gateway skips the stream consumer
  ([retinue/ROOMS.md](../retinue/ROOMS.md)).
- **Shape:** adapter opts into stream events; SSE grows an
  in-progress event; UI shows a single in-flight bubble per speaker
  that becomes the final line. Parallel waves must not steal each
  other's in-flight id.
- **Not:** replacing the room bus with a speech-to-speech model.

### 12. IDE-attached rooms (`workspace=sandbox|ide`)

- **Labels:** `enhancement`, `linux`
- **Why:** Design is locked in [retinue/ROADMAP.md](../retinue/ROADMAP.md)
  and [retinue/ROOMS.md](../retinue/ROOMS.md). Not shipped.
- **Shape:** default remains an isolated sandbox container. `ide`
  bind-mounts a host path on the **gateway machine** (`ide_path` /
  `RETINUE_IDE_ROOT`). Loud UI confirm. Only IDE-marked rooms get the
  mount. Not SSHFS, not a remote-IDE protocol.
- **Discuss first:** path-allow list, confirm copy, and what "full
  operator envelope" means for a public install.

### 13. Workspace screen take-over (noVNC)

- **Labels:** `enhancement`, `linux`, `ui/ux`
- **Why:** `GET /workspace` already reports the shared container and
  an attach command. A browser view of that computer is the next
  increment (`plugins/platforms/retinue_rooms/workspace.py`).
- **Shape:** opt-in, same isolation story as the workspace computer,
  no extra daemon on the host if a container-side noVNC is enough.
- **Discuss first:** port publish, auth, and whether this waits on
  IDE-attached rooms.
