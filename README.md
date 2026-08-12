# Retinue

> **Self-hosted AI teammates that work together.**

Retinue lets you build a staff of named AI agents — each with its own persona, job, memory, and model — that talk with you **and with each other** in shared rooms, and do real work on **your** machine or in podman containers, not on someone else's cloud.

Retinue is a thin, plugin-layer fork of [hermes-agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com) (MIT), maintained by [Novique](https://novique.ai).

## The idea

Commercial products like xAI's Grok Bot have proven the UX: agents you hire with a three-field brief (a name, one job, how it should work), group chats where agents hand work to each other, persistent per-agent memory, and a shared computer the whole team works on. But they run on managed cloud VMs at $120–300/month, with your credentials living on machines you don't control.

Retinue brings that experience home:

- **Local-first** — your agents' computer is your computer, or a podman container you own.
- **Model-agnostic** — every agent picks its own brain: Anthropic, OpenAI, xAI, Gemini, or fully local llama.cpp / vLLM / Ollama endpoints (34 provider plugins inherited from Hermes).
- **Open source** — MIT, same as upstream.

## What Retinue adds to Hermes

| Piece | What it is | Status |
|---|---|---|
| **Rooms** | A shared transcript where N agents and you converse, with turn-taking — built as a Hermes platform adapter | In development |
| **Web UI** | Native chat interface plus a three-field "hire an agent" flow that templates a persona, model, and toolset per agent | In development |
| **Podman execution** | A long-lived workspace container as the team's shared computer, or stricter per-agent isolation — built as a Hermes execution-environment backend | In development |
| **Routines & take-over view** | Learn-by-demonstration task replay and a watch/take-over screen | Planned |

Everything Hermes already does — the agent loop, tools (terminal, browser, files, computer use, MCP), skills, memory, messaging-platform gateways — is inherited, not reimplemented.

## Status

Early development. The fork policy, architecture notes, and roadmap live in [`retinue/`](retinue/):

- [`retinue/ROADMAP.md`](retinue/ROADMAP.md) — build phases
- [`retinue/FORK-POLICY.md`](retinue/FORK-POLICY.md) — how this fork tracks upstream (short version: our delta stays plugin-shaped; upstream core files are not edited)

## Upstream

This repository tracks [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) and syncs deliberately. Hermes' own documentation applies to everything not listed in the table above. If you want the upstream product, use upstream — it's excellent.

## License

MIT. Upstream code © 2025 Nous Research; Retinue additions © 2026 Novique. See [LICENSE](LICENSE).
