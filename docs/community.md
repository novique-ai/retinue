# Community surfaces

How Retinue uses GitHub. Issues, Discussions, labels, and a `main`
ruleset were enabled 2026-08-13. **GitHub is the public record of
product work.** Local IDE beads may still track private host/cutover
tasks; they are not a substitute for an Issue or PR on this repo.

## Discussions vs Issues

| Use | Where |
|---|---|
| Ideas, architecture, "how do I…", feature exploration | **Discussions** |
| Actionable bugs and agreed work | **Issues** |
| Code | **Pull requests** against `main` |
| Vulnerabilities and conduct reports | **Private security advisory** ([.github/SECURITY.md](../.github/SECURITY.md)) |

An accepted Discussion becomes an Issue (or is linked from a PR). Please
do not open both for the same un-agreed idea.

### Suggested Discussion categories

Discussions are on. Keep the default set small:

| Category | For |
|---|---|
| **Announcements** (maintainers only) | Releases, policy, calls for help |
| **Ideas** | Feature exploration before it is an Issue |
| **Q&A** | Setup and "how do I" (close as answered; promote recurring ones into docs) |
| **Show and tell** | What people built with rooms |
| **Development** | Fork policy, carried patches, CI, architecture |

## Label taxonomy

Keep this list short. GitHub will auto-create a label named in an issue
template the first time it is applied; the rest should be created once
(see the commands in the readiness report, or
`scripts/retinue-apply-github-labels.sh` if present).

| Label | Color (suggested) | Use |
|---|---|---|
| `bug` | `#d73a4a` | Broken behavior |
| `enhancement` | `#a2eeef` | Agreed new behavior |
| `documentation` | `#0075ca` | Docs only |
| `question` | `#d876e3` | Setup help / needs a conversation |
| `good first issue` | `#7057ff` | New contributor, one sitting |
| `help wanted` | `#008672` | Maintainer wants a volunteer |
| `ui/ux` | `#fbca04` | `retinue-web/` or rooms UX |
| `linux` | `#0e8a16` | Linux-specific |
| `wayland` | `#1d76db` | Wayland / compositor |
| `x11` | `#5319e7` | X11-specific |
| `kde` | `#3e4eb8` | KDE / Plasma |
| `gnome` | `#4a86cf` | GNOME |
| `packaging` | `#e99695` | Install, distro, prefix, pip UI path |
| `ai-provider` | `#b60205` | Model preset / provider |
| `rooms` | `#5319e7` | Turn-taking / adapter |
| `voice` | `#d4c5f9` | STT/TTS |
| `upstream` | `#c5def5` | Belongs in or came from Hermes |
| `priority: p1` | `#b60205` | Should land soon |
| `priority: p2` | `#fbca04` | Normal |
| `duplicate` / `invalid` / `wontfix` | GitHub defaults | Close reasons |

Do not add a label for every Hermes subsystem. If a bug is inherited
Hermes behavior, tag `upstream` and link the Nous issue.

## Branch protection (intent)

`main` should only move through pull requests. Require the
**Retinue delta** check. The inherited Hermes `CI / All required checks pass`
job is large; whether to require it on day one is a maintainer
tradeoff (see the readiness report).

Do not force-push or delete `main`.
