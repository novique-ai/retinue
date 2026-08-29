/** Speak Replies queue: skip backlog, speak later agent lines (#234). */

export type SpeakLine = {
  seq: number;
  kind: string;
};

export type SpeakGate = {
  /** Highest seq treated as already-heard. null = not primed yet. */
  skipThrough: number | null;
};

export function maxSeq(messages: ReadonlyArray<{ seq: number }>): number {
  let max = 0;
  for (const msg of messages) {
    if (msg.seq > max) max = msg.seq;
  }
  return max;
}

export function primeSpeakGate(
  messages: ReadonlyArray<{ seq: number }>,
): SpeakGate {
  return { skipThrough: maxSeq(messages) };
}

export function selectSpeakSeqs(
  messages: ReadonlyArray<SpeakLine>,
  spoken: ReadonlySet<number>,
  gate: SpeakGate,
): number[] {
  if (gate.skipThrough === null) return [];
  const seqs: number[] = [];
  for (const msg of messages) {
    if (msg.kind !== "agent") continue;
    if (spoken.has(msg.seq)) continue;
    if (msg.seq <= gate.skipThrough) continue;
    seqs.push(msg.seq);
  }
  return seqs;
}

/**
 * One step of the Speak Replies gate.
 *
 * - Toggle off: drop the watermark so the next enable re-primes.
 * - Rising edge (checkbox on): current non-empty transcript is backlog.
 *   An empty snapshot does not prime (SSE catch-up may still be in flight).
 * - Speak already on, gate unprimed: first non-empty snapshot is catch-up
 *   (opening a room with the toggle already on). An empty snapshot waits
 *   so a later history batch is not treated as live.
 * - Otherwise: agent lines after the watermark, not yet spoken.
 */
export function nextSpeakSeqs(opts: {
  enabled: boolean;
  risingEdge: boolean;
  messages: ReadonlyArray<SpeakLine>;
  spoken: ReadonlySet<number>;
  gate: SpeakGate;
}): { gate: SpeakGate; seqs: number[]; markSpoken: number[] } {
  if (!opts.enabled) {
    return { gate: { skipThrough: null }, seqs: [], markSpoken: [] };
  }
  // Empty transcript is ambiguous: empty room, or SSE catch-up not yet
  // landed. Do not prime skipThrough=0 — that would speak the whole
  // history when the first snapshot arrives.
  if (opts.risingEdge) {
    if (opts.messages.length === 0) {
      return { gate: { skipThrough: null }, seqs: [], markSpoken: [] };
    }
    return { gate: primeSpeakGate(opts.messages), seqs: [], markSpoken: [] };
  }
  if (opts.gate.skipThrough === null) {
    if (opts.messages.length === 0) {
      return { gate: opts.gate, seqs: [], markSpoken: [] };
    }
    return { gate: primeSpeakGate(opts.messages), seqs: [], markSpoken: [] };
  }
  const seqs = selectSpeakSeqs(opts.messages, opts.spoken, opts.gate);
  return { gate: opts.gate, seqs, markSpoken: seqs };
}

/** Voice-bar copy: never dump HTTP JSON. Short provider prose can stay. */
export function formatSpeakError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err ?? "");
  const cleaned = raw.replace(/\s+/g, " ").trim();
  if (
    /\{[\s\S]*\}/.test(raw) ||
    /\bHTTP\s+\d{3}\b/i.test(raw) ||
    /playback failed/i.test(raw) ||
    !cleaned
  ) {
    return "Couldn't speak this reply. Later replies will still play.";
  }
  return cleaned.length > 80 ? `${cleaned.slice(0, 77)}…` : cleaned;
}
