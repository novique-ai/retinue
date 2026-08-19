# Retinue fork policy

Upstream (`NousResearch/hermes-agent`) is a very large (~685K LOC), very fast-moving codebase. A fork that edits upstream core files will rot within weeks. Retinue therefore follows one rule:

**The Retinue delta stays plugin-shaped. Upstream core files are not edited.**

## Where Retinue code lives

| Delta | Location | Mechanism |
|---|---|---|
| Rooms (shared multi-agent transcript + turn-taking) | `plugins/platforms/retinue_rooms/` | New platform adapter, per `gateway/platforms/ADDING_A_PLATFORM.md`, consuming the typed event stream (`gateway/stream_events.py`) |
| Podman execution | upstream `tools/environments/docker.py` as-is | No new backend needed: upstream's `find_docker()` already falls back to podman on PATH (`HERMES_DOCKER_BINARY` forces it). The workspace-computer sharing is the carried patch below. |
| Web UI | `retinue-web/` | Standalone client of the sessions/SSE API (`gateway/platforms/api_server.py`); no PTY embedding |
| Product docs | `README.md`, `retinue/`, `docs/` | See exception below |
| Community health | `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `NOTICE`, `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/SECURITY.md` | Owned; `merge=ours` so upstream templates do not clobber them |
| Contributor scripts / CI | `scripts/retinue-*.sh`, `.github/workflows/retinue.yml` | New files; no upstream counterpart |

## Carried patches

Narrow, cherry-pickable fixes for upstream bugs that break Retinue in practice. Each must
reference an upstream issue and be dropped when upstream fixes it. Current list:

| File | Patch | Upstream issue |
|---|---|---|
| `tools/environments/docker.py` | `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` — opt-in workspace key replacing the per-profile container identity, so every room member attaches to one shared "workspace computer" container | [NousResearch/hermes-agent#84671](https://github.com/NousResearch/hermes-agent/issues/84671) |
| `tools/terminal_tool.py` | Key the `_active_environments` cache by `TERMINAL_DOCKER_SHARED_CONTAINER_KEY` instead of collapsing to `"default"`. Identity already uses that key at creation, so a single `"default"` cache entry handed the first workspace's container to every later turn — a sandbox room's container served an IDE room ([#16](https://github.com/novique-ai/retinue/issues/16)) | [NousResearch/hermes-agent#84671](https://github.com/NousResearch/hermes-agent/issues/84671) |
| `tools/terminal_tool.py`, `tools/environments/docker.py`, `gateway/platforms/base.py` | Read the workspace key and volume list through `tools/workspace_context.py` (a **new** file, not an upstream edit) instead of `os.getenv`. The values are per-room; carrying them in process env forced the rooms adapter to serialize every cycle behind one lock, so one turn blocked every room for up to the local-model timeout ([#67](https://github.com/novique-ai/retinue/issues/67)). With no overlay bound these read plain `os.environ`, so non-room callers are unchanged | [NousResearch/hermes-agent#84671](https://github.com/NousResearch/hermes-agent/issues/84671) |
| `tools/image_generation_tool.py` | `xai/grok-imagine-image/v2.0/text-to-image` catalog entry: `upscale` default flipped to `False`. Upstream's own `test_upscale_defaults_are_all_off` (added in their opt-in-only sweep `f06c41522`) forbids default-on upscaling, but the entry merged after the sweep with `True`, leaving upstream main red on its own suite. That upstream test doubles as this patch's drift guard: a sync that clobbers the flip turns CI red. Drop when upstream fixes it. | [NousResearch/hermes-agent#90013](https://github.com/NousResearch/hermes-agent/issues/90013) |
| `cron/scheduler.py` | `_deliver_result`: a resolved live transport with no platform config block means "live adapter, no config", not "disabled". Upstream applied that rule to relay transports only, so `deliver=origin` jobs to a plugin platform under a profile with no `platforms:` block failed with `platform '<name>' not configured/enabled` ([#112](https://github.com/novique-ai/retinue/issues/112)) | [NousResearch/hermes-agent#89302](https://github.com/NousResearch/hermes-agent/issues/89302) |

**Upstream report:** Filed as [NousResearch/hermes-agent#89302](https://github.com/NousResearch/hermes-agent/issues/89302).
`resolve_delivery_transport` returns a live native transport when its config block is
absent, but `_deliver_result` then rejected `not pconfig`. Removing
`and transport.is_relay` from the immediately preceding branch applies the existing
`PlatformConfig(enabled=True)` normalization to both resolved transport kinds.

### Retired patches

| File | Patch | Retired |
|---|---|---|
| `agent/prompt_builder.py` | `SKILLS_GUIDANCE` reword for the Anthropic content filter ([#82154](https://github.com/NousResearch/hermes-agent/issues/82154)) | 2026-08-16 sync — **upstream adopted the same reword** and documented it with a NOTE citing the issue. The regression guard stays (`test_skills_guidance_avoids_the_content_filter_wording`), because the failure it prevents is a remote, billing-shaped HTTP 400 that misleads users into buying quota. It now asserts on the imported `SKILLS_GUIDANCE` constant rather than scanning the file: upstream's explanatory comment quotes the bad phrasing to say what NOT to use, and a guard that fires on a comment is a guard that gets muted. |

Each patch above has a drift guard in
`plugins/platforms/retinue_rooms/test_carried_patches.py`; an upstream sync that
reverts one turns the suite red instead of failing silently in production.

## Owned upstream paths

`README.md` is the public face of the fork. Contributor-health files
that GitHub reads from well-known names (`CONTRIBUTING.md`, issue/PR
templates, …) are also owned, because leaving the Hermes copies in
place sends contributors to NousResearch. Those paths are listed in
`.gitattributes` with `merge=ours` so an upstream sync does not clobber
them.

`.github/workflows/review-labels.yml` is owned for a different reason than the
contributor-health files: it is upstream *governance*, not upstream *branding*. It
requires a maintainer-applied `ci-reviewed` label whenever CI-sensitive files change,
which an upstream sync does by definition — so every sync PR would block on a label
only we could apply to our own merge. That is ceremony, not review. Our replacement
keeps the call interface byte-compatible so `ci.yaml` (renamed from `ci.yml` upstream in
the 13ce0c5c6 sync) stays pristine and keeps receiving
upstream improvements, and it still logs which areas tripped the gate.

The sibling `contributor-check.yml` is deliberately NOT owned. Its data is cheap, it
catches something real, and it does not fire on every sync — the 00c12dac6 sync needed
exactly one new mapping file across 1399 commits.

Do not expand that list casually — every `merge=ours` file stops
receiving upstream edits. Product logic still does not belong in
upstream core files.

`LICENSE` and the root `SECURITY.md` trust model stay unmodified
upstream text. Retinue reporting instructions live in
`.github/SECURITY.md`.

## Upstream sync procedure

Syncs are deliberate, not automatic (upstream bumps config schema versions and requires a web rebuild):

```bash
git fetch upstream main
git merge <upstream-sha-or-tag>       # owned files auto-resolve ours
# smoke: hermes doctor, gateway start, web build if web/ changed
git tag retinue-base-<short-sha>
git push origin main --tags
```

The current base is recorded by the most recent `retinue-base-*` tag.

## Licensing

Upstream is MIT © 2025 Nous Research; the LICENSE file is retained unmodified. Retinue additions are MIT © 2026 Novique.
