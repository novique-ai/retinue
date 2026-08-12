# Retinue fork policy

Upstream (`NousResearch/hermes-agent`) is a very large (~685K LOC), very fast-moving codebase. A fork that edits upstream core files will rot within weeks. Retinue therefore follows one rule:

**The Retinue delta stays plugin-shaped. Upstream core files are not edited.**

## Where Retinue code lives

| Delta | Location | Mechanism |
|---|---|---|
| Rooms (shared multi-agent transcript + turn-taking) | `plugins/platforms/retinue_rooms/` | New platform adapter, per `gateway/platforms/ADDING_A_PLATFORM.md`, consuming the typed event stream (`gateway/stream_events.py`) |
| Podman execution backend | `tools/environments/podman.py` | New `BaseEnvironment` subclass (additive file, registered like the seven existing backends) |
| Web UI | `apps/retinue-web/` | Standalone client of the sessions/SSE API (`gateway/platforms/api_server.py`); no PTY embedding |
| Product docs | `README.md`, `retinue/` | See exception below |

## Carried patches

Narrow, cherry-pickable fixes for upstream bugs that break Retinue in practice. Each must
reference an upstream issue and be dropped when upstream fixes it. Current list:

| File | Patch | Upstream issue |
|---|---|---|
| `agent/prompt_builder.py` | Reword `SKILLS_GUIDANCE` sentence 1 — the stock wording trips an Anthropic content filter for subscription-OAuth tokens, surfacing as a billing-shaped 400 ("out of extra usage") | [NousResearch/hermes-agent#82154](https://github.com/NousResearch/hermes-agent/issues/82154) |

## The one owned upstream path

`README.md` is the single upstream file Retinue replaces (a public repo needs its own face). It is protected with a `merge=ours` gitattribute so upstream syncs never clobber it. Everything else from upstream merges clean because we don't touch it.

## Upstream sync procedure

Syncs are deliberate, not automatic (upstream bumps config schema versions and requires a web rebuild):

```bash
git fetch upstream main
git merge <upstream-sha-or-tag>       # README.md auto-resolves ours
# smoke: hermes doctor, gateway start, web build if web/ changed
git tag retinue-base-<short-sha>
git push origin main --tags
```

The current base is recorded by the most recent `retinue-base-*` tag.

## Licensing

Upstream is MIT © 2025 Nous Research; the LICENSE file is retained unmodified. Retinue additions are MIT © 2026 Novique.
