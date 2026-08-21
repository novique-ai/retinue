import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { includeBusyThinkers, isWorkingIn } from "./thinking.ts";

const ROOM_A = "room-alpha";
const ROOM_B = "room-beta";

describe("isWorkingIn", () => {
  it("is false for an idle agent", () => {
    assert.equal(isWorkingIn({ busy: false, busy_rooms: [] }, ROOM_A), false);
  });

  it("is true in the room the turn is actually in", () => {
    assert.equal(isWorkingIn({ busy: true, busy_rooms: [ROOM_A] }, ROOM_A), true);
  });

  it("is false in a room the agent is merely a member of (#168)", () => {
    // The whole bug: a turn in room A rendered a thinking bubble in room B.
    assert.equal(isWorkingIn({ busy: true, busy_rooms: [ROOM_A] }, ROOM_B), false);
  });

  it("handles an agent busy in several rooms at once", () => {
    const agent = { busy: true, busy_rooms: [ROOM_A, ROOM_B] };
    assert.equal(isWorkingIn(agent, ROOM_A), true);
    assert.equal(isWorkingIn(agent, ROOM_B), true);
    assert.equal(isWorkingIn(agent, "room-gamma"), false);
  });

  it("is false for an unknown member", () => {
    assert.equal(isWorkingIn(undefined, ROOM_A), false);
  });

  it("falls back to the global flag when the gateway predates busy_rooms", () => {
    // A stale tab against an older gateway gets the old (wrong-but-familiar)
    // behaviour rather than losing every bubble.
    assert.equal(isWorkingIn({ busy: true }, ROOM_A), true);
  });

  it("does not treat an empty busy_rooms as a missing field", () => {
    assert.equal(isWorkingIn({ busy: true, busy_rooms: [] }, ROOM_A), false);
  });
});

describe("includeBusyThinkers", () => {
  const agents = {
    mangus: { busy: true, busy_rooms: [ROOM_A] },
    scout: { busy: false, busy_rooms: [] },
  };

  it("adds a member whose turn is in this room", () => {
    assert.deepEqual(
      includeBusyThinkers([], ["mangus", "scout"], agents, ROOM_A),
      ["mangus"],
    );
  });

  it("adds nobody when the only busy member is busy elsewhere (#168)", () => {
    assert.deepEqual(
      includeBusyThinkers([], ["mangus", "scout"], agents, ROOM_B),
      [],
    );
  });

  it("keeps an explicit waiter even when the busy flag is elsewhere", () => {
    // `waiting` comes from this room's own turn state and always wins.
    assert.deepEqual(
      includeBusyThinkers(["mangus"], ["mangus"], agents, ROOM_B),
      ["mangus"],
    );
  });

  it("does not duplicate a member already waiting", () => {
    assert.deepEqual(
      includeBusyThinkers(["mangus"], ["mangus"], agents, ROOM_A),
      ["mangus"],
    );
  });

  it("ignores members with no agent record", () => {
    assert.deepEqual(includeBusyThinkers([], ["ghost"], agents, ROOM_A), []);
  });
});
