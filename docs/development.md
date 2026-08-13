# Development setup

Goal: a stranger can go from clone to a running Retinue room without
sharing state with a stock Hermes install.

```
clone → ./scripts/retinue-dev-setup.sh → configure one provider → hermes gateway
```

Then open http://127.0.0.1:8643.

## Requirements

| Tool | Why |
|---|---|
| Git | Clone, and `git-lfs` if you hit LFS pointers |
| Python 3.11–3.13 | Hermes / rooms runtime. 3.14 is out of range upstream. |
| [uv](https://docs.astral.sh/uv/) | Installs the editable package and the test extras |
| Node.js 20+ | Builds `retinue-web/` (the UI is static files after that) |
| podman or docker | Optional. Only needed for the shared workspace computer |

Linux is the supported development OS. macOS / WSL reports are welcome
but not a claimed path.

## Bootstrap

```bash
git clone https://github.com/novique-ai/retinue.git
cd retinue
./scripts/retinue-dev-setup.sh
```

The script:

1. Creates a virtualenv **outside** the clone when it can
   (`${HERMES_HOME:-$HOME/.retinue}/venvs/retinue-dev`), falling back
   to `.venv` in-tree if that already exists.
2. Runs `uv pip install -e ".[dev]"`.
3. Runs `npm ci` and `npm run build` in `retinue-web/`.
4. Prints the env vars and commands for the next step.

A venv inside the clone can be deleted by an agent terminal command
(`rm -rf .venv`). Prefer the home outside the tree.

## First run

Use a dedicated Hermes home so Retinue does not read or write
`~/.hermes`:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.retinue}"
export RETINUE_ROOMS_ENABLED=1
export GATEWAY_MULTIPLEX_PROFILES=true

mkdir -p "$HERMES_HOME"
# If you do not already have a config in that home:
#   cp cli-config.yaml.example "$HERMES_HOME/config.yaml"
# then put a provider key in "$HERMES_HOME/.env", or run:
#   hermes auth login

hermes doctor
hermes gateway
```

Rooms stay dark unless **both** of these are true:

- `RETINUE_ROOMS_ENABLED=1` (or `RETINUE_ROOMS_API_KEY` is set)
- The gateway is multiplexing profiles
  (`GATEWAY_MULTIPLEX_PROFILES=true`, or
  `hermes config set gateway.multiplex_profiles true` in that home)

If the web UI 404s, `retinue-web/dist/` is missing — rerun
`npm run build` in `retinue-web/`. The adapter only serves a built SPA.

### Isolated from a stock Hermes install

| | Stock Hermes | Retinue (recommended) |
|---|---|---|
| Home | `~/.hermes` | `~/.retinue` |
| Binary | whatever `hermes` is on `PATH` | the venv the setup script created |
| Rooms | off | `RETINUE_ROOMS_ENABLED=1` |

Point `PATH` at the setup venv (`$HERMES_HOME/venvs/retinue-dev/bin`)
or run `./hermes` from the clone after that venv is active.

## Day-to-day commands

```bash
# Rooms + web checks (what CI runs on the delta)
./scripts/retinue-check.sh

# Rooms tests only
pytest plugins/platforms/retinue_rooms

# Rebuild the UI after a retinue-web/ edit, then refresh the browser
# (the adapter serves dist/ — no separate frontend dev server is required)
cd retinue-web && npm run build

# Optional: Vite dev server if you want HMR. You still need the gateway
# for the API; CORS is not a concern when the adapter serves the built UI.
cd retinue-web && npm run dev

# Reference CLI against a running gateway
python -m plugins.platforms.retinue_rooms.cli --help
```

`pytest` with no path will **not** collect the rooms tests. Upstream
`testpaths` is `tests/`. Always pass `plugins/platforms/retinue_rooms`
or use `scripts/retinue-check.sh`.

The full Hermes suite is `scripts/run_tests.sh`. You do not need it for
Retinue-delta work.

## Workspace computer (optional)

To give every room member one shared container:

```bash
export TERMINAL_ENV=docker
export TERMINAL_DOCKER_SHARED_CONTAINER_KEY=retinue-dev
# optional: HERMES_DOCKER_BINARY=podman
```

See [retinue/ROOMS.md](../retinue/ROOMS.md) for the attach/status API
and the isolation caveats (terminal only, not browser/file/MCP).

## Configuration reference (rooms)

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | **Set this to `~/.retinue`** for a clean Retinue home |
| `RETINUE_ROOMS_ENABLED` | unset | `1`/`true` enables the platform |
| `RETINUE_ROOMS_API_KEY` | unset | Bearer token; unset ⇒ localhost-only bind |
| `RETINUE_ROOMS_HOST` / `_PORT` | `127.0.0.1` / `8643` | Bind address |
| `RETINUE_ROOMS_TURN_TIMEOUT` | `300` | Seconds for one cloud-provider turn |
| `RETINUE_ROOMS_LOCAL_TURN_TIMEOUT` | `1800` | Seconds for one local-LLM turn |
| `GATEWAY_MULTIPLEX_PROFILES` | unset | `true` to host every hired profile in one process |
| `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` | unset | Shared workspace-computer identity |

## Common friction

| Symptom | Likely cause |
|---|---|
| `GET /health` connection refused | Gateway not running, or rooms not enabled |
| UI is a JSON 404 | `retinue-web/dist/` not built |
| Hire works, agents never reply | No provider credentials in `$HERMES_HOME`; or multiplex is off so only `default` is served |
| "I hired someone and nothing changed" | Old gateway without hot-hire; restart `hermes gateway` |
| Tests you expected to run did not | You invoked bare `pytest` — pass the plugin path |
| `~/.hermes` changed when you were working on Retinue | `HERMES_HOME` was unset |

## Manual clone fallback

If you do not want the setup script:

```bash
uv venv "${HERMES_HOME:-$HOME/.retinue}/venvs/retinue-dev" --python 3.12
export VIRTUAL_ENV="${HERMES_HOME:-$HOME/.retinue}/venvs/retinue-dev"
export PATH="$VIRTUAL_ENV/bin:$PATH"
uv pip install -e ".[dev]"
cd retinue-web && npm ci && npm run build
```

Do not install with a random system `pip` and then run a different
`hermes` from `PATH`. The entry point you start must come from the
venv you just installed.
