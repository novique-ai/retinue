import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  formatSpeakError,
  nextSpeakSeqs,
  type SpeakLine,
} from "./speakQueue.ts";

function msg(
  seq: number,
  kind: SpeakLine["kind"] = "agent",
): SpeakLine {
  return { seq, kind };
}

const HISTORY: SpeakLine[] = [
  msg(1, "user"),
  msg(2),
  msg(3),
  msg(4),
  msg(5),
];

describe("nextSpeakSeqs — enable mid-transcript (#234)", () => {
  it("does not queue the backlog when Speak Replies is turned on", () => {
    const step = nextSpeakSeqs({
      enabled: true,
      risingEdge: true,
      messages: HISTORY,
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    assert.equal(step.gate.skipThrough, 5);
    assert.deepEqual(step.seqs, []);
    assert.deepEqual(step.markSpoken, [2, 3, 4, 5]);
  });

  it("queues only agent lines after the watermark", () => {
    const spoken = new Set([2, 3, 4, 5]);
    const step = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: [...HISTORY, msg(6), msg(7, "user"), msg(8)],
      spoken,
      gate: { skipThrough: 5 },
    });
    assert.deepEqual(step.seqs, [6, 8]);
  });

  it("skips backlog lines that have no ts (seq still <= watermark)", () => {
    // The old openedAtRef filter skipped the time check when ts was missing,
    // so history without ts replayed from the top of the room.
    const step = nextSpeakSeqs({
      enabled: true,
      risingEdge: true,
      messages: [msg(1), msg(2), msg(3)],
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    assert.deepEqual(step.seqs, []);
    assert.equal(step.gate.skipThrough, 3);
  });

  it("speaks the next reply after enabling in an empty room", () => {
    const primed = nextSpeakSeqs({
      enabled: true,
      risingEdge: true,
      messages: [],
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    assert.equal(primed.gate.skipThrough, 0);
    const live = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: [msg(1, "user"), msg(2)],
      spoken: new Set(),
      gate: primed.gate,
    });
    assert.deepEqual(live.seqs, [2]);
  });

  it("does not replay when the toggle is turned off then on again", () => {
    const off = nextSpeakSeqs({
      enabled: false,
      risingEdge: false,
      messages: HISTORY,
      spoken: new Set([2, 3, 4, 5]),
      gate: { skipThrough: 5 },
    });
    assert.equal(off.gate.skipThrough, null);
    const on = nextSpeakSeqs({
      enabled: true,
      risingEdge: true,
      messages: HISTORY,
      spoken: new Set(),
      gate: off.gate,
    });
    assert.deepEqual(on.seqs, []);
    assert.equal(on.gate.skipThrough, 5);
  });
});

describe("nextSpeakSeqs — room open with Speak already on", () => {
  it("waits on an empty snapshot so later history is catch-up, not live", () => {
    const empty = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: [],
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    assert.equal(empty.gate.skipThrough, null);
    assert.deepEqual(empty.seqs, []);
  });

  it("treats the first non-empty snapshot as backlog", () => {
    const catchup = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: HISTORY,
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    assert.equal(catchup.gate.skipThrough, 5);
    assert.deepEqual(catchup.seqs, []);
    const live = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: [...HISTORY, msg(6)],
      spoken: new Set(catchup.markSpoken),
      gate: catchup.gate,
    });
    assert.deepEqual(live.seqs, [6]);
  });

  it("does not speak tool or system lines", () => {
    const primed = nextSpeakSeqs({
      enabled: true,
      risingEdge: true,
      messages: [msg(1, "user")],
      spoken: new Set(),
      gate: { skipThrough: null },
    });
    const live = nextSpeakSeqs({
      enabled: true,
      risingEdge: false,
      messages: [
        msg(1, "user"),
        msg(2, "system"),
        msg(3, "tool"),
        msg(4),
      ],
      spoken: new Set(),
      gate: primed.gate,
    });
    assert.deepEqual(live.seqs, [4]);
  });
});

describe("formatSpeakError", () => {
  it("does not dump local TTS HTTP JSON in the voice bar", () => {
    const err = new Error(
      'local TTS HTTP 500: {"detail":"500: TTS engine failed to synthesize audio for chunk 5."}',
    );
    const note = formatSpeakError(err);
    assert.equal(
      note,
      "Couldn't speak this reply. Later replies will still play.",
    );
    assert.ok(!note.includes("chunk"));
    assert.ok(!note.includes("{"));
    assert.ok(!note.includes("500"));
  });

  it("keeps a short non-provider message", () => {
    assert.equal(formatSpeakError(new Error("mic permission denied")), "mic permission denied");
  });

  it("maps playback failure to the same isolated note", () => {
    assert.equal(
      formatSpeakError(new Error("playback failed")),
      "Couldn't speak this reply. Later replies will still play.",
    );
  });
});
