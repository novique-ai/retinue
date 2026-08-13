# Contributing to Retinue

Thank you for wanting to help. Retinue is a young project and a small maintainer team — well-scoped pull requests, clear bug reports, and documentation fixes all matter.

This guide is for **this repository** (`novique-ai/retinue`). The tree is a public fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). Most of the code you see is upstream Hermes. Retinue's own product lives in a small, plugin-shaped delta.

**Please read [retinue/FORK-POLICY.md](retinue/FORK-POLICY.md) before editing anything outside that delta.**

The inherited Hermes contributing guide (skills vs tools, memory-provider policy, the huge test runner) still applies if you are touching upstream code. Read it on GitHub rather than in this file: [NousResearch/hermes-agent CONTRIBUTING.md](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md).

---

## What to work on here vs upstream

| Work | Where it belongs |
|---|---|
| Rooms, hire flow, sidebar, voice, routines, workspace-computer sharing, `retinue-web/` | **This repo** |
| Product docs under `retinue/` and `docs/` | **This repo** |
| A bug in inherited Hermes Agent behavior that is not caused by Retinue | **[Upstream](https://github.com/NousResearch/hermes-agent/issues)** — link it from a Retinue issue if users hit it here |
| A one-line upstream fix that unblocks Retinue | A **carried patch** here, with an upstream issue link, per the fork policy. Ask first. |

If you are unsure, open a Discussion (idea) or a short Issue (actionable) before writing a large patch.

---

## Discussions vs Issues

- **[GitHub Discussions](https://github.com/novique-ai/retinue/discussions)** — broad ideas, architecture questions, "how do I…", feature exploration. No commitment that it will be built.
- **[GitHub Issues](https://github.com/novique-ai/retinue/issues)** — actionable bugs and agreed-upon work. An idea that the maintainer accepts should become an Issue (or you link the Discussion from the PR).

Please search both before opening a new thread.

---

## Development setup

### Fork and clone

```bash
# Fork novique-ai/retinue on GitHub, then:
git clone https://github.com/<you>/retinue.git
cd retinue
git remote add upstream https://github.com/novique-ai/retinue.git
```

`origin` is your fork. `upstream` here means **Retinue's** `main`, not Hermes. The Hermes remote, if you add it, should be named `hermes` or similar so the two are not confused.

### One-command bootstrap

```bash
./scripts/retinue-dev-setup.sh
```

This is the expected stranger path: install Python deps, build the web UI, print how to start the gateway. Details and the manual fallback live in [docs/development.md](docs/development.md).

Requirements: Git, Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Node.js 20+. Podman or Docker is optional (workspace computer).

### Create a branch

```bash
git checkout -b fix/short-description
# or
git checkout -b feat/short-description
```

Use a short prefix (`fix/`, `feat/`, `docs/`, `test/`, `chore/`) and a slug that will still make sense in six months.

---

## Coding and style expectations

- **Stay in the delta.** Prefer `plugins/platforms/retinue_rooms/`, `retinue-web/`, `retinue/`, and `docs/`. Do not edit upstream core files to land a Retinue feature.
- **Python:** match the surrounding plugin style. The rooms adapter is stdlib-only — do not add a Python dependency for rooms unless the Discussion says otherwise.
- **TypeScript / React:** `retinue-web/` is a small Vite app. Keep it dependency-light. Run `npm run build` in that directory (it typechecks).
- **Do not rewrite the inherited Hermes tree** to match a new style. Ruff/ty rules in `pyproject.toml` are upstream's.
- **No drive-by refactors** in the same PR as a feature or fix.
- **Commit messages:** [Conventional Commits](https://www.conventionalcommits.org/) (`fix(rooms):`, `feat(web):`, `docs:`, `test(rooms):`, `chore:`).

---

## Testing

Before you open a PR that touches the Retinue delta, run:

```bash
./scripts/retinue-check.sh
```

That is the same pair of checks GitHub Actions runs on every PR:

1. `pytest plugins/platforms/retinue_rooms` — rooms unit tests and carried-patch drift guards.
2. `npm run build` in `retinue-web/` — TypeScript + Vite production build.

Default `pytest tests/` does **not** collect the rooms suite (`testpaths` is still the upstream `tests/` tree). Use the script, or pass the plugin path explicitly.

If you change inherited Hermes code (rare; see fork policy), also run the relevant upstream slice:

```bash
scripts/run_tests.sh tests/path/you/touched
```

Do not run the full Hermes suite unless you know you need it — it is large and slow.

**New behavior needs a test.** Bug fixes should include a regression test that fails without the fix. UI-only copy changes can skip Python tests; still build the web UI if you touched `retinue-web/`.

---

## Pull requests

1. Push your branch to **your fork**.
2. Open a PR against `novique-ai/retinue` `main`.
3. Fill in the PR template.
4. Reference the issue with `Closes #123` (or `Fixes #123`) when the PR fully resolves it. Use `Refs #123` if it is only related.
5. Keep the PR focused. One problem per PR. A 20-line rooms fix should not also reformat the web UI.
6. Wait for CI. The **Retinue delta** check should finish in a few minutes. The inherited Hermes `CI / All required checks pass` workflow is large; a docs-only or `retinue-web/`-only change may still trigger parts of it. That is upstream behavior, not something you need to "fix" in the same PR.

### Scope we will review

- Small, reviewable diffs that a tired maintainer can understand in one sitting.
- Tests or a clear manual test plan.
- Docs updates when you change user-visible behavior.

### Scope we will bounce (politely)

- Unrelated drive-by refactors.
- New dependencies without a Discussion.
- Edits to upstream core files that should have been a plugin, a carried patch, or an upstream PR.
- "Please implement this large feature" PRs that were not discussed.

### Review feedback

- Assume good intent. Reply to each review comment, even if the answer is "will do in a follow-up."
- Push additional commits to the same branch. You may rebase / force-push **your feature branch** to keep history readable; do not force-push `main`.
- If you disagree, say why. A short technical argument is better than silent force-push.

---

## Documentation-only contributions

Docs PRs are first-class. Useful work includes:

- Fixing a step that failed when you followed [docs/development.md](docs/development.md).
- Clarifying rooms API behavior from [retinue/ROOMS.md](retinue/ROOMS.md).
- Screenshots or a short demo for the README.
- Typo and link fixes.

You do not need a matching Issue for a typo. For a new page, a short Issue or Discussion helps us put it in the right place.

Please do not copy operator-private host names, Tailscale IPs, or systemd unit drop-ins from internal notes into public docs.

---

## Large features

Open a Discussion first (or comment on the existing Issue) if your idea is more than about a day of work. Examples that need a conversation:

- Token streaming into the room transcript
- noVNC / screen take-over
- IDE-attached workspace mounts
- New AI providers that are not already Hermes plugins
- Packaging (Flatpak, distro packages, installers)

We would rather say "yes, and here is the shape" than review a 2,000-line surprise.

Starter-sized work we already know we want is listed in [docs/contributor-issues.md](docs/contributor-issues.md).

---

## Security

Do not file public issues for vulnerabilities. See [.github/SECURITY.md](.github/SECURITY.md).

The Hermes Agent trust model still applies to this tree — the only real containment boundary is the OS / container, not in-process filters. Read the root [SECURITY.md](SECURITY.md) before assuming a prompt-injection demo is a vulnerability.

---

## Code of Conduct

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## License

By contributing, you agree that your changes are licensed under the MIT License, the same as the rest of this repository (upstream © Nous Research; Retinue additions © Novique and contributors). See [LICENSE](LICENSE) and [NOTICE](NOTICE).
