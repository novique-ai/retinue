# Security policy (Retinue)

This is the **reporting** policy for the Retinue fork
([novique-ai/retinue](https://github.com/novique-ai/retinue)).

The **trust model** — what is and is not a vulnerability in this tree — is
inherited from Hermes Agent and lives in the repository-root
[SECURITY.md](../SECURITY.md). Read that before filing. In particular:

- The only security boundary against an adversarial LLM is the operating
  system / container, not in-process filters.
- Prompt injection by itself is not a vulnerability.
- Bypasses of approval-gate regexes, redaction, or Skills Guard are out of
  scope as advisories (they are welcome as ordinary issues or PRs).

## Where to report

**Do not open a public issue for a vulnerability.**

1. **Retinue-specific** (rooms adapter, `retinue-web/`, hire/sidebar/voice,
   carried patches, this fork's docs or CI): report privately via
   [GitHub Security Advisories](https://github.com/novique-ai/retinue/security/advisories/new)
   on **this** repository.
2. **Inherited Hermes Agent core** that is not caused by the Retinue delta:
   prefer the upstream channel in the root [SECURITY.md](../SECURITY.md)
   ([NousResearch/hermes-agent advisories](https://github.com/NousResearch/hermes-agent/security/advisories/new)
   or `security@nousresearch.com`). You may also notify us here so we can
   track the fork impact.

Retinue does not operate a bug bounty.

## What to include

- A concise description and your severity assessment.
- Affected files and line ranges where you can name them.
- Environment: OS / distro, Python version, `git rev-parse HEAD`, whether
  you used `HERMES_HOME=~/.retinue` or another home.
- A reproduction against `main`.
- Which trust-model clause in the root [SECURITY.md](../SECURITY.md) you
  believe is crossed.

## Disclosure

- Coordinated disclosure window: 90 days from report, or until a fix is
  released, whichever comes first.
- Credit: reporters are named in [CHANGELOG.md](../CHANGELOG.md) unless
  they ask to stay anonymous.

## Deployment notes specific to Retinue

- The rooms HTTP API binds **localhost only** unless `RETINUE_ROOMS_API_KEY`
  is set. Do not expose `:8643` on a non-loopback interface without that
  key (and preferably without a VPN / Tailscale in front).
- Rooms inherit Hermes' isolation posture. A shared workspace container
  (`TERMINAL_DOCKER_SHARED_CONTAINER_KEY`) is one computer for every
  member — treat it that way.
- Opt-in IDE mounts (when shipped) bind a **host path** into that
  computer. Only rooms marked for it should get the mount.
