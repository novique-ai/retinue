# Voice interaction — alternatives (2026-08-13 look)

A room is a **shared transcript**. Voice for Retinue has to produce that
transcript and speak from it. Speech-to-speech that bypasses the room bus is a
different product.

This look is **not** a current-hardware audit. Capture can be a browser mic, a
conference puck, a phone, or a room array — that is a later input-device choice.
The decision is the pipeline.

Operator picked **A for testing, then B for testing** (2026-08-13).
Shipped plugin-shaped: `GET /voice`, `POST /rooms/{id}/audio`, `POST /tts`,
hold-to-talk in `retinue-web` (`6f4b79b62`). Live default on c-desktop is
**Track A** (xAI, `ready: true`).

**Flip to Track B** (claymore-1 sidecar, Glimmer left on `:8080`):

```bash
# on c-desktop
mkdir -p ~/.config/systemd/user/retinue-gateway.service.d
cat > ~/.config/systemd/user/retinue-gateway.service.d/voice.conf <<'EOF'
[Service]
Environment=RETINUE_VOICE_BACKEND=openai
Environment=RETINUE_VOICE_BASE_URL=http://10.44.0.13:8104/v1
EOF
systemctl --user daemon-reload
systemctl --user restart retinue-gateway.service
```

Remove `voice.conf` and restart to return to A. Sidecar is a test `nohup`
on claymore-1 (`python3 /srv/evo-data/local-llm/voice/voice_sidecar.py
--host 10.44.0.13 --port 8104`). whisper-cli loads per request (VRAM stays
~23 GiB on Glimmer). PID file: `/srv/evo-data/local-llm/voice/sidecar.pid`.

## What already exists (reference only)

| Surface | What it is | Why it is not the Retinue path |
|---|---|---|
| Hermes `/voice` + Discord live voice | Upstream CLI/TUI + Discord channel listen/speak | Different home (`~/.hermes`), 1:1 or Discord, not rooms |
| Hermes Desktop voice | c-desktop composer STT/TTS | Jeeves/Desktop, not `HERMES_HOME=~/.retinue` |
| clay-blade `local-ai` | Kokoro `:8102` (CPU) + Speaches `faster-whisper-base` `:8103` (CPU) + Qwen2-VL `:8101` | Wired for Desktop; whisper-**base** quality; 2070 is 8 GB and already holds vision |
| Hermes STT providers | `local`, `openai`, `groq`, `mistral`, **`xai`**, `elevenlabs` | Built-in. Rooms adapter does not call them. |
| Hermes streaming TTS | `elevenlabs`, `gemini`, `openai`, **`xai`** WebSocket | Gateway seam exists (`supports_streaming_tts`). Rooms adapter does not opt in. |
| `retinue-web/` | Text composer + SSE transcript | **Zero** voice code (`MediaRecorder` / `getUserMedia` / playback). |

Verified live 2026-08-13: clay-blade `local-ai.service` still serving Kokoro + whisper-base. Last Kokoro generation in the journal is 2026-08-11 (Jeeves/Desktop), not Retinue.

## Live capacity on claymore-1

Verified 2026-08-13 on the box, not from docs:

- Glimmer 30B Vulkan llama-server on `:8080` (`muse-glimmer-30B-kquant-dynamic` + DFlash + mmproj, ctx 65536). Router aliases `local/auto` etc. still point here.
- Unified GPU pool: **96.0 GiB** total, **23.1 GiB** used → **~73 GiB free**.
- Linux `MemAvailable` ~17 GiB is the leftover *system* slice after the GPU claim — do not read that as "the box is full."
- Do **not** evict `local/auto` to free space. There is room for a **second** process.

`whisper.cpp` with Vulkan is known to run on gfx1151 (Strix Halo). A speech sidecar does not have to fight Glimmer for the same llama-server.

## Product shape (locked for any track)

```
mic ──▶ STT ──▶ POST /rooms/{id}/messages {text} ──▶ existing turn engine
                                                      │
member final (or streamed sentence) ──▶ TTS ──▶ speaker / browser playback
```

- User speech becomes a normal room user line. `@mentions`, lead routing, budgets, SSE stay as they are.
- Each retainer can have its own voice. Parallel waves need a playback queue (mention order), not simultaneous shouting.
- Local members still get the 1800s budget; cloud stays 300s. Voice I/O does not change those.
- Plugin-shaped only: `retinue-web/` + `plugins/platforms/retinue_rooms/`. No upstream-core edits. Configure `~/.retinue` `stt:` / `tts:` — do not patch `tools/transcription_tools.py`.

## Tracks

### A — xAI Voice as the mouth/ear (recommended first slice)

Use the APIs already in the Hermes tree and already authenticated for this workspace (`xai-oauth`).

| Piece | Endpoint | Official price (docs 2026-07-28) |
|---|---|---|
| STT | `POST /v1/stt` or streaming WS | $0.10 / hour batch, $0.20 / hour streaming |
| TTS | `POST /v1/tts` or `wss://api.x.ai/v1/tts` | $15.00 / 1M chars |
| Custom voices | `POST /v1/custom-voices` (≤120s clip) | one `voice_id` per retainer |
| Built-in voices | `eve`, `leo`, `rex`, `rigel`, `ursa`, … | enough to distinguish 6 staff without cloning |

Hermes already streams xAI TTS (`docs/streaming-tts.md`). Built-in STT provider name is `xai`.

**First slice:** retinue-web hold-to-talk → `POST /rooms/{id}/audio` (or transcribe in the adapter and reuse `POST /messages`) → `stt.provider: xai` → existing cycle. Play each member's notify text with a mapped `voice_id`. Opt the rooms adapter into the gateway streaming-TTS seam so replies start speaking at the first sentence.

**Why first:** judges whether voice-in-rooms is even desirable, in days, at pocket cost, without touching claymore-1. Quality will be good enough to evaluate the UX (barge-in, who is speaking, playback vs transcript).

**Cost sanity:** an hour of talking is cents of STT. A 2k-char retainer reply is a fraction of a cent of TTS.

### B — Second model on claymore-1 (use the headroom)

A **sidecar**, not a swap. Glimmer stays on `:8080`. Speech gets its own process and port.

| Role | Candidate | Fit next to Glimmer |
|---|---|---|
| STT | `whisper.cpp` Vulkan `large-v3-turbo` (~6 GB) or `large-v3` (~10 GB) | Proven on gfx1151. Best local accuracy/latency tradeoff. |
| STT (alt) | Kyutai delayed-stream / Vox-class streaming ASR | Only if PTT feels too slow and we need partials. |
| TTS | Kyutai Pocket TTS (~100M, CPU-capable) or Kokoro-class | Tiny. Do not spend 16 GB of GPU on TTS. |
| TTS (quality) | A larger open TTS later (Qwen3-TTS / Voxtral-class) | Only after the room UX is proven. 16 GB VRAM is affordable here; still a second process. |

Expose OpenAI-compatible `/v1/audio/transcriptions` + `/v1/audio/speech` on the tailnet. Point `~/.retinue` `stt.provider: openai` / `tts.provider: openai` at that base URL (same pattern as clay-blade `:8102`/`:8103`).

**Do not** put speech on the Glimmer llama-server. Two locals in one room wave already share that server.

**Do not** start ollama for this.

### C — Upgrade the existing clay-blade `local-ai`

Same topology as Desktop: Speaches + Kokoro on clay-blade. Upgrade STT from `faster-whisper-base` to `large-v3-turbo`. Keep TTS Kokoro.

**Only if** we want one shared voice backend for Jeeves *and* Retinue. The 2070 cannot also host a 10 GB Whisper next to Qwen2-VL. Turbo on CPU is the honest upgrade; GPU Whisper means parking vision.

This is the incremental ops path, not the product path. Whisper-base is why Desktop voice still feels thin.

### D — Cloud speech-to-speech as the brain (reject for v1)

xAI `wss://api.x.ai/v1/realtime?model=grok-voice-latest` (~$0.05/min), OpenAI Realtime, Gemini Live. Full-duplex, tool use, sub-second.

These APIs want to **be** the agent. Retinue's agents are hired staff with their own models, SOULs, tools, and 1800s local turns. Putting Grok Voice in front as the thinker collapses the roster into one voice agent.

Allowed later as a **thin I/O shim** (audio in/out only, text still hits the room bus) — at that point Track A already covers it with less magic. Do not start here.

### E — Discord live voice / Hermes Desktop / phone

Discord `/voice channel` and Desktop voice already work for 1:1 Hermes. They are the wrong face: Retinue's public product is the room UI. Phone/SIP (Retell, Twilio) is AnswerCrew, not this.

## Capture layer (after a track is picked)

Not decided by current headset inventory:

- Browser `MediaRecorder` + hold-to-talk in `retinue-web` (default; no new hardware).
- Always-on + VAD / wake word in the browser or a small local daemon.
- Dedicated array / puck / phone-as-mic pointing at the same STT URL.

Always-on is a UX increment on top of PTT, not a reason to pick D.

## Recommendation

1. **Lock the product shape** (transcript-preserving PTT). 
2. **Ship Track A** as the first room-voice slice — xAI STT/TTS, per-member voices, rooms adapter opts into streaming TTS, `retinue-web` hold-to-talk + playback queue.
3. **Stand Track B up if A feels right and we want local/offline** — whisper.cpp Vulkan + a tiny TTS on claymore-1, Glimmer untouched. claymore-1 has the room.
4. **Leave C** as a Desktop quality fix, not a Retinue milestone.
5. **Do not do D or E** for v1.

## Implementation sketch (only after the operator names a track)

Plugin-shaped:

- `retinue-web/`: hold-to-talk button, `MediaRecorder`, play returned / streamed audio, per-member voice tint in the roster.
- `plugins/platforms/retinue_rooms/`: `POST /rooms/{id}/audio` → STT → existing message cycle; implement `supports_streaming_tts` / `write_streaming_tts`; map `slug → voice_id`.
- `~/.retinue` config only for `stt:` / `tts:`. No core forks.

Verify: rooms pytest still green; a live room on c-desktop `:8643` shows the spoken line in the transcript and plays the retainer reply.

## Out of scope

- `infra-dfc1` (noVNC). Still operator-gated.
- Replacing Glimmer, lowering the 1800s local budget, or putting two speech models on the one llama-server.
- Re-doing P0–P4 v1 or `infra-ivl9.1`.
