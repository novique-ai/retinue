import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { RoomMeta } from "./api";
import {
  attentionBadge,
  attentionLabel,
  clearedByPrincipalPost,
  isHeldNotice,
  roomAttention,
} from "./attention.ts";

const base: RoomMeta = { id: "r-1", name: "Ops", members: ["scout"], lead: "scout", max_agent_turns: 8 };

describe("roomAttention (#256)", () => {
  it("is idle with neither flag, or on an old payload without answered_user", () => {
    assert.equal(roomAttention(base), "idle");
    assert.equal(roomAttention({ ...base, needs_user: false }), "idle");
    assert.equal(attentionBadge("idle"), null);
    assert.equal(attentionLabel("Ops", "idle"), "Ops");
  });

  it("is waiting (blocking) when a retainer asked for the principal", () => {
    assert.equal(roomAttention({ ...base, needs_user: true }), "waiting");
    const badge = attentionBadge("waiting");
    assert.ok(badge);
    assert.match(badge.label, /paused/);
    assert.equal(badge.className, "needs-you-badge");
  });

  it("is answered (non-blocking) for an FYI answer", () => {
    const state = roomAttention({ ...base, answered_user: true });
    assert.equal(state, "answered");
    const badge = attentionBadge(state);
    assert.ok(badge);
    assert.doesNotMatch(badge.label, /paused/);
    assert.equal(badge.className, "answered-badge");
    assert.equal(attentionLabel("Ops", state), "Ops — answer for you");
  });

  it("prefers waiting when both flags are somehow set", () => {
    assert.equal(roomAttention({ ...base, needs_user: true, answered_user: true }), "waiting");
  });

  it("a principal post clears both states locally", () => {
    const cleared = clearedByPrincipalPost({ ...base, needs_user: true, answered_user: true });
    assert.equal(roomAttention(cleared), "idle");
  });
});

describe("isHeldNotice", () => {
  it("recognises the server's held-post line only", () => {
    assert.equal(
      isHeldNotice("Held: Ops Session's message #7 started no turn — the room is paused, waiting on Ada."),
      true,
    );
    assert.equal(isHeldNotice("scout is on it."), false);
    assert.equal(isHeldNotice(""), false);
  });
});
