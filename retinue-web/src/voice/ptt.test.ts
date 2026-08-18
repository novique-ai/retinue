import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import {
  DEFAULT_PTT_KEY,
  PushToTalkController,
  isPrimaryPointerDown,
  isTypingTarget,
  matchesPttKey,
  type PttReleaseInfo,
} from "./ptt.ts";

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function flush(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

type Harness = {
  controller: PushToTalkController;
  starts: number;
  stops: PttReleaseInfo[];
  errors: unknown[];
  active: boolean[];
  gate: ReturnType<typeof deferred> | null;
  failStart: Error | null;
  failStop: Error | null;
  busy: boolean;
};

function harness(): Harness {
  const h: Harness = {
    controller: null as unknown as PushToTalkController,
    starts: 0,
    stops: [],
    errors: [],
    active: [],
    gate: null,
    failStart: null,
    failStop: null,
    busy: false,
  };
  h.controller = new PushToTalkController({
    start: async () => {
      h.starts += 1;
      if (h.gate) await h.gate.promise;
      if (h.failStart) throw h.failStart;
    },
    stop: async (info) => {
      h.stops.push(info);
      if (h.failStop) throw h.failStop;
    },
    isBusy: () => h.busy,
    onActiveChange: (active) => {
      h.active.push(active);
    },
    onError: (error) => {
      h.errors.push(error);
    },
  });
  return h;
}

describe("isPrimaryPointerDown", () => {
  it("accepts the primary / left button", () => {
    assert.equal(isPrimaryPointerDown({ pointerId: 1, button: 0, pointerType: "mouse" }), true);
    assert.equal(isPrimaryPointerDown({ pointerId: 2, button: 0, pointerType: "touch" }), true);
  });

  it("rejects right and middle mouse buttons", () => {
    assert.equal(isPrimaryPointerDown({ pointerId: 1, button: 2, pointerType: "mouse" }), false);
    assert.equal(isPrimaryPointerDown({ pointerId: 1, button: 1, pointerType: "mouse" }), false);
  });
});

describe("isTypingTarget", () => {
  it("treats input, textarea, and select as typing fields", () => {
    assert.equal(isTypingTarget({ tagName: "INPUT" }), true);
    assert.equal(isTypingTarget({ tagName: "TEXTAREA" }), true);
    assert.equal(isTypingTarget({ tagName: "SELECT" }), true);
    assert.equal(isTypingTarget({ tagName: "BUTTON" }), false);
    assert.equal(isTypingTarget(null), false);
  });

  it("treats contenteditable as a typing field", () => {
    assert.equal(isTypingTarget({ isContentEditable: true, tagName: "DIV" }), true);
  });

  it("uses closest() when present", () => {
    const inside = {
      closest(sel: string) {
        return sel.includes("textarea") ? this : null;
      },
    };
    const outside = { closest() { return null; } };
    assert.equal(isTypingTarget(inside), true);
    assert.equal(isTypingTarget(outside), false);
  });
});

describe("matchesPttKey", () => {
  it("matches the configured code", () => {
    assert.equal(matchesPttKey({ code: "Space", repeat: false }, DEFAULT_PTT_KEY), true);
    assert.equal(matchesPttKey({ code: "KeyV", repeat: false }, DEFAULT_PTT_KEY), false);
    assert.equal(matchesPttKey({ code: "KeyV", repeat: false }, { code: "KeyV" }), true);
  });
});

describe("PushToTalkController", () => {
  afterEach(async () => {
    // let any leftover start/stop promises settle between tests
    await flush();
  });

  it("pointerdown starts and pointerup stops", async () => {
    const h = harness();
    assert.equal(h.controller.handlePointerDown({ pointerId: 7, button: 0 }), "start");
    await flush();
    assert.equal(h.starts, 1);
    assert.equal(h.controller.isActive(), true);
    assert.equal(h.controller.handlePointerUp({ pointerId: 7, button: 0 }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
    assert.equal(h.controller.isActive(), false);
  });

  it("pointercancel stops", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    assert.equal(h.controller.handlePointerCancel({ pointerId: 1, button: 0 }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("lostpointercapture stops", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    assert.equal(h.controller.handleLostPointerCapture({ pointerId: 1, button: 0 }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("ignores a second start while already active", async () => {
    const h = harness();
    assert.equal(h.controller.handlePointerDown({ pointerId: 1, button: 0 }), "start");
    await flush();
    assert.equal(h.controller.handlePointerDown({ pointerId: 2, button: 0 }), "ignore");
    assert.equal(h.controller.engageFrom("keyboard"), "consume");
    assert.equal(h.starts, 1);
  });

  it("ignores a stop while already inactive", () => {
    const h = harness();
    assert.equal(h.controller.handlePointerUp({ pointerId: 1, button: 0 }), "ignore");
    assert.equal(h.controller.abort(), "ignore");
    assert.equal(h.stops.length, 0);
  });

  it("ignores right mouse button", () => {
    const h = harness();
    assert.equal(
      h.controller.handlePointerDown({ pointerId: 1, button: 2, pointerType: "mouse" }),
      "ignore",
    );
    assert.equal(h.starts, 0);
  });

  it("ignores middle mouse button", () => {
    const h = harness();
    assert.equal(
      h.controller.handlePointerDown({ pointerId: 1, button: 1, pointerType: "mouse" }),
      "ignore",
    );
    assert.equal(h.starts, 0);
  });

  it("ignores a second simultaneous touch point", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0, pointerType: "touch" });
    assert.equal(
      h.controller.handlePointerDown({ pointerId: 2, button: 0, pointerType: "touch" }),
      "ignore",
    );
    await flush();
    assert.equal(h.starts, 1);
    // releasing the ignored finger must not stop the first
    assert.equal(h.controller.handlePointerUp({ pointerId: 2, button: 0 }), "ignore");
    assert.equal(h.controller.isActive(), true);
  });

  it("keyboard Space starts and Space release stops", async () => {
    const h = harness();
    assert.equal(h.controller.handleKeyDown({ code: "Space", repeat: false }), "start");
    await flush();
    assert.equal(h.starts, 1);
    assert.equal(h.controller.handleKeyUp({ code: "Space", repeat: false }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("ignores keyboard auto-repeat", async () => {
    const h = harness();
    h.controller.handleKeyDown({ code: "Space", repeat: false });
    await flush();
    assert.equal(h.controller.handleKeyDown({ code: "Space", repeat: true }), "consume");
    assert.equal(h.starts, 1);
  });

  it("ignores Space while a typing field is focused", () => {
    const h = harness();
    const textarea = { tagName: "TEXTAREA" };
    assert.equal(
      h.controller.handleKeyDown({ code: "Space", repeat: false, target: textarea }),
      "ignore",
    );
    const input = { tagName: "INPUT" };
    assert.equal(
      h.controller.handleKeyDown({ code: "Space", repeat: false, target: input }),
      "ignore",
    );
    assert.equal(h.starts, 0);
  });

  it("visibility hidden stops", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    assert.equal(h.controller.handleVisibilityHidden(), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("window blur stops", async () => {
    const h = harness();
    h.controller.handleKeyDown({ code: "Space", repeat: false });
    await flush();
    assert.equal(h.controller.handleBlur(), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("discards an in-flight start when the gesture ends early", async () => {
    const h = harness();
    h.gate = deferred();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    assert.equal(h.controller.isActive(), true);
    assert.equal(h.controller.handlePointerUp({ pointerId: 1, button: 0 }), "stop");
    h.gate.resolve();
    await flush();
    await flush();
    assert.deepEqual(h.stops, [{ committed: false }]);
    assert.equal(h.controller.isActive(), false);
  });

  it("reports a start failure and does not stay active", async () => {
    const h = harness();
    h.failStart = new Error("mic denied");
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    assert.equal(h.errors.length, 1);
    assert.equal(h.controller.isActive(), false);
    assert.equal(h.active.at(-1), false);
    assert.equal(h.stops.length, 0);
  });

  it("stays inactive after a stop failure", async () => {
    const h = harness();
    h.failStop = new Error("upload failed");
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    h.controller.handlePointerUp({ pointerId: 1, button: 0 });
    await flush();
    assert.equal(h.errors.length, 1);
    assert.equal(h.controller.isActive(), false);
  });

  it("refuses to start while busy", () => {
    const h = harness();
    h.busy = true;
    assert.equal(h.controller.handlePointerDown({ pointerId: 1, button: 0 }), "ignore");
    assert.equal(h.controller.handleKeyDown({ code: "Space", repeat: false }), "ignore");
    assert.equal(h.starts, 0);
  });

  it("honours a custom key binding", async () => {
    const h = harness();
    h.controller.setKeyBinding({ code: "KeyV" });
    assert.equal(h.controller.handleKeyDown({ code: "Space", repeat: false }), "ignore");
    assert.equal(h.controller.handleKeyDown({ code: "KeyV", repeat: false }), "start");
    await flush();
    assert.equal(h.starts, 1);
    assert.equal(h.controller.handleKeyUp({ code: "KeyV", repeat: false }), "stop");
  });

  it("keeps the session while a second source is still held", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    h.controller.handleKeyDown({ code: "Space", repeat: false });
    assert.equal(h.controller.handlePointerUp({ pointerId: 1, button: 0 }), "consume");
    assert.equal(h.controller.isActive(), true);
    assert.equal(h.stops.length, 0);
    assert.equal(h.controller.handleKeyUp({ code: "Space", repeat: false }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });

  it("abort while a second source is held still stops", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    h.controller.handleKeyDown({ code: "Space", repeat: false });
    await flush();
    assert.equal(h.controller.handlePointerCancel({ pointerId: 1, button: 0 }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
    assert.equal(h.controller.isActive(), false);
  });

  it("dispose stops an active session (unmount / navigation)", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 1, button: 0 });
    await flush();
    h.controller.dispose();
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
    assert.equal(h.controller.isActive(), false);
  });

  it("releases after the pointer slides off the button (same pointerId)", async () => {
    const h = harness();
    h.controller.handlePointerDown({ pointerId: 9, button: 0 });
    await flush();
    // capture means we still see this pointerup even outside the hit target
    assert.equal(h.controller.handlePointerUp({ pointerId: 9, button: 0 }), "stop");
    await flush();
    assert.deepEqual(h.stops, [{ committed: true }]);
  });
});
