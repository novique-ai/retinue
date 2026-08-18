# Voice interaction

> Voice backends here are interoperable APIs (including xAI STT/TTS).
> Retinue is not affiliated with or endorsed by xAI.

A room is a **shared transcript**. Voice for Retinue has to produce that
transcript and speak from it. Speech-to-speech that bypasses the room bus is a
different product.

Capture can be a browser mic, a conference puck, a phone, or a room array —
that is an input-device choice. The decision is the pipeline.

## What shipped

Plugin-shaped voice is live in `retinue-web/` and
`plugins/platforms/retinue_rooms/`. No upstream-core edits.

| Piece | Where | Behavior |
|---|---|---|
| Hold-to-talk | `retinue-web/src/voice/` | `getUserMedia` + ScriptProcessor → WAV blob. Pointer Events + capture on the button (not the page): finger/left-button down records, up uploads. Space holds the same session when focus is not in a typing field. Android long-press / context-menu is suppressed on this control only — see the comment at the top of `ptt.ts`. |
| Speak replies | same | Sequential `POST /tts` + `Audio` playback queue when “Speak replies” is on. Stop (room chrome / Esc) or unchecking the toggle cuts the current clip and drops the queue. |
| Backend status | `GET /voice` | `{ backend, ready, detail, voices }` from `voice.status()` |
| Audio in | `POST /rooms/{id}/audio` | Raw body + query `from` / `filename` / optional `draft` → STT → `draft + speech` → leading spoken vocative (`at Claude`) rewritten to `@Claude` when the line has no live @mention → normal user-message cycle → **202** `{ seq, planned, text }` |
| TTS out | `POST /tts` | JSON `{ text, speaker }` (or `voice`) → `audio/wav` or `audio/mpeg` bytes |

Web client: `api.voiceStatus()`, `api.sendAudio()`, `api.speak()`, `api.stop()` in
`retinue-web/src/api.ts`. Room UI shows a “Hold to talk” button (pointer
or Space; see `retinue-web/src/voice/`), a backend label (`xai` /
`openai`, plus “not ready” when applicable), and a short “Heard: …” note
after transcription. **Stop** (header + Escape) cuts Speak Replies
immediately and aborts this room’s turn cycle so the next line is a
redirect. A composer draft (typically `@Patty` from the mention bar) is
sent as `draft` and prefixed onto the spoken line so the mention engine
routes it. Empty draft still goes to the lead unless the spoken line
starts with a vocative (`at Claude`, `hey Ellie`, `Hi, Patty`,
`Claude,`) — that is rewritten to a live `@Handle` on the transcript. A
tap still wins: if the draft already has an `@`, the spoken “at …” is
left as words.

Default backend is **Track A** (`RETINUE_VOICE_BACKEND` unset → `xai`).
`ready` is true when xAI credentials resolve, or when the OpenAI-compatible
base URL is set for Track B.

## Product shape (locked)

```
mic ──▶ STT ──▶ room user line (same path as typed text) ──▶ turn engine
                                                              │
member final text ──▶ TTS ──▶ browser playback (queued)
```

- User speech becomes a normal room user line. `@mentions`, lead routing,
  budgets, and SSE stay as they are.
- Each retainer can have its own **narrator** id (see `STAFF_VOICES` /
  `RETINUE_VOICE_MAP` / `AVAILABLE_VOICES` in `voice.py`). Hire and Edit
  bind to `GET /voice.available`, not the roster map — a staff slug is
  not a voice id. The web client queues playback so a later speaker does
  not talk over an earlier one.
- Voice I/O does not change turn budgets.
- Configure Retinue voice via process env (below). Do not patch
  `tools/transcription_tools.py` for rooms.

**Not shipped (still true):** the rooms adapter does **not** implement the
gateway streaming-TTS seam (`supports_streaming_tts` / `write_streaming_tts`).
Playback is whole-utterance `POST /tts` after the agent message is on the
transcript.

## Backends (only these)

Implemented in `plugins/platforms/retinue_rooms/voice.py`.
`RETINUE_VOICE_BACKEND` selects one of:

| Value | Role | Credentials / URL |
|---|---|---|
| `xai` (default) | Track A — xAI `POST {base}/stt` and `POST {base}/tts` | `XAI_API_KEY` if set, else Hermes xAI OAuth (`tools.xai_http.resolve_xai_http_credentials`). Optional `XAI_BASE_URL` (default `https://api.x.ai/v1`). |
| `openai` | Track B — OpenAI-compatible local/remote | Requires `RETINUE_VOICE_BASE_URL` (e.g. `http://127.0.0.1:8104/v1`). Optional `RETINUE_VOICE_API_KEY` (default `not-needed`). |

Aliases that map to `openai`: `local`, `sidecar` (case-insensitive).

Optional knobs (all read from env in `voice.py`):

| Env | Purpose |
|---|---|
| `RETINUE_VOICE_MAP` | Comma list `slug:voice_id` overrides (e.g. `scout:helix,admin:eve`) |
| `RETINUE_VOICE_STT_MODEL` | OpenAI-compatible STT model name (default `whisper-1`) |
| `RETINUE_VOICE_TTS_MODEL` | OpenAI-compatible TTS model name (default `tts-1`) |

Built-in staff → voice map (overridable): `admin`→`eve`, `envoy`→`rigel`,
`janitor`→`lux`, `scout`→`ursa`, `editor`→`leo`, `scribe`→`celeste`.
A stored or env value that is not a narrator id (`eve`, `leo`, `rex`,
`rigel`, `ursa`, `celeste`, `lux`, `iris`, plus `helix` for env/API) is
ignored and the next precedence step applies. Hire/patch of a non-narrator
id is a 400.

### Flip to Track B (generic example)

Point the rooms process at any OpenAI-compatible STT/TTS base URL:

```bash
export RETINUE_VOICE_BACKEND=openai
export RETINUE_VOICE_BASE_URL=http://127.0.0.1:8104/v1
# restart the process that serves the rooms HTTP API
```

Unset `RETINUE_VOICE_BACKEND` / `RETINUE_VOICE_BASE_URL` (or set backend back
to `xai`) to return to Track A.

### Optional local sidecar (Track B helper)

`plugins/platforms/retinue_rooms/voice_sidecar.py` is a **test** HTTP server
that exposes OpenAI-shaped routes for a local install:

- `POST /v1/audio/transcriptions` (multipart `file=`) — `whisper-cli`
- `POST /v1/audio/speech` — Piper if configured, else `espeak-ng` / `espeak`
- `GET /health` (and `/v1/health`)

Run example (paths and binaries are yours; defaults bind loopback):

```bash
export RETINUE_VOICE_WHISPER=/path/to/whisper-cli
export RETINUE_VOICE_WHISPER_MODEL=/path/to/ggml-large-v3-turbo.bin
# optional: RETINUE_VOICE_PIPER + RETINUE_VOICE_PIPER_MODEL
python3 plugins/platforms/retinue_rooms/voice_sidecar.py --host 127.0.0.1 --port 8104
```

Sidecar env defaults: `RETINUE_VOICE_SIDECAR_HOST=127.0.0.1`,
`RETINUE_VOICE_SIDECAR_PORT=8104`. Then set
`RETINUE_VOICE_BACKEND=openai` and
`RETINUE_VOICE_BASE_URL=http://127.0.0.1:8104/v1` on the rooms process.

## Related surfaces (not the Retinue rooms path)

| Surface | Why it is not rooms voice |
|---|---|
| Hermes CLI `/voice`, Discord live voice | Different home and session model; not room transcript |
| Hermes Desktop STT/TTS | Desktop composer, not `retinue-web` rooms |
| Hermes STT providers (`local`, `openai`, `groq`, `mistral`, `xai`, …) | Built-in; rooms adapter uses `voice.py`, not those tools |
| Hermes streaming TTS (`elevenlabs`, `gemini`, `openai`, `xai` WS) | Gateway seam exists; rooms adapter does not opt in |

## Tracks (design choices)

### A — xAI STT/TTS (default shipped path)

Use xAI batch STT/TTS with credentials already usable for Hermes xAI.
Per-member voice ids from `STAFF_VOICES` / `RETINUE_VOICE_MAP`.
Hold-to-talk and speak-replies are implemented as above.

**Why first:** evaluates voice-in-rooms UX without standing up local speech
hardware. Cost is usage-based on the provider (check current xAI pricing;
do not treat historical numbers in old notes as authoritative).

### B — OpenAI-compatible sidecar (local / offline)

A **second process**, not a swap of the chat model server. Expose
`/v1/audio/transcriptions` + `/v1/audio/speech` (or the same paths under a
base that already includes `/v1`). Point
`RETINUE_VOICE_BACKEND=openai` + `RETINUE_VOICE_BASE_URL=…` at it.

Typical local stack for the shipped sidecar: `whisper-cli` + Piper or
espeak. Larger or streaming ASR/TTS stacks are operator choice as long as
the HTTP shape matches.

**Do not** put speech on the same llama-server process that serves chat
completion for local members if that process is already contended.

### C — Shared desktop-class local speech stack

Reuse an existing OpenAI-compatible Speaches/Kokoro-style stack on a
workstation GPU/CPU host, upgraded from tiny Whisper if quality is thin.
Same Track B env pointing at that base URL. Incremental ops path, not a
separate rooms protocol.

### D — Cloud speech-to-speech as the brain (reject for v1)

Vendor realtime speech-to-speech APIs want to **be** the agent. Retinue’s
agents are hired staff with their own models, SOULs, tools, and long local
turns. Putting a vendor S2S model in front as the thinker collapses the
roster into one voice agent.

Allowed later as a **thin I/O shim** (audio in/out only, text still hits
the room bus) — Track A already covers that shape with less magic. Do not
start here.

### E — Discord live voice / Desktop-only / phone

Fine for 1:1 Hermes. Wrong face for the public room UI. Phone/SIP products
are a different surface.

## Capture layer

- **Shipped:** browser mic + hold-to-talk in `retinue-web` (no new hardware).
- **Later:** always-on + VAD / wake word; dedicated array / puck / phone-as-mic
  pointing at the same STT path.

Always-on is a UX increment on top of PTT, not a reason to pick D.

## Recommendation

1. **Keep the product shape** (transcript-preserving PTT).
2. **Use Track A** for the first room-voice experience when xAI credentials
   are available.
3. **Stand Track B up** when local/offline speech is required — OpenAI-compatible
   sidecar + the env vars above.
4. **Leave C** as a quality/ops choice for shared local stacks.
5. **Do not do D or E** for v1 rooms product.

## Out of scope

- Streaming TTS into the rooms adapter (gateway seam exists; rooms still
  uses request/response `POST /tts`).
- Replacing the local chat model server, or co-loading large speech models
  onto a fully loaded inference process without spare capacity.
- Speech-to-speech “brain” products that bypass the room transcript.
