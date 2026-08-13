# Retinue roadmap

Public product roadmap. Design notes and operator-facing detail live
under [`retinue/`](../retinue/) — start with
[retinue/ROADMAP.md](../retinue/ROADMAP.md) if you are implementing
something listed here.

Status words mean:

- **Shipped** — on `main`, used in the rooms UI.
- **Testable** — on `main`, needs a configured backend to try.
- **Designed** — written down, not implemented.
- **Later** — accepted direction, not scheduled.

## Shipped (v1)

| Item | Notes |
|---|---|
| Public fork + plugin-delta policy | [retinue/FORK-POLICY.md](../retinue/FORK-POLICY.md) |
| Rooms | Shared transcript, `@mention` turn-taking, turn budget, parallel waves |
| Web UI + hire flow | `retinue-web/`, three-field brief, served by the adapter |
| Workspace computer | Shared podman/docker container via carried patch |
| Routines | Save / replay a room's user prompts |
| Workspace status | `GET /workspace` + attach command (not screen take-over) |
| Hot-hire | New profile joins without a gateway restart |
| SSE transcript | `GET /rooms/{id}/stream` |
| Sidebar | Edit / archive / delete, team separators, drag-reorder |
| Model presets | Per-hire `model:` block; bundled versioned cloud presets |

## Testable

| Item | Notes |
|---|---|
| Voice (hold-to-talk) | xAI STT/TTS or an OpenAI-compatible sidecar. See [retinue/VOICE.md](../retinue/VOICE.md) |

## Designed, not shipped

| Item | Notes |
|---|---|
| noVNC / screen take-over | Next increment after workspace status. |

## Later / open

These are real product gaps, not a commitment. Prefer a Discussion
before starting one. Ready-to-file issue drafts:
[contributor-issues.md](contributor-issues.md).

- Token streaming into the room (v1 is finals only).
- Richer mention UX (display aliases; composer autocomplete beyond the chip bar).
- Media in the transcript (v1 is plain text).
- Approval UX that is more than the gateway text fallback.
- Linux packaging (distro package, Flatpak, or similar).
- Wayland/X11-specific desktop integration (only needed beyond the browser UI).
- First-run empty states and a short recorded demo.

## What will not be done in this repo

- Replacing Hermes Agent. Upstream remains the runtime.
- Editing upstream core files to land features (fork policy).
- Becoming an official xAI or official Nous Research product.
