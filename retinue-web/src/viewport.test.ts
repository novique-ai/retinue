import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  KEYBOARD_INSET_MIN_PX,
  NARROW_BREAKPOINT,
  NARROW_MEDIA,
  computeKeyboardInset,
  isNarrowViewport,
  shellClassName,
} from "./viewport.ts";

describe("isNarrowViewport — overlay vs docked sidebar", () => {
  it("treats a 375px phone as overlay-sidebar, not a docked 292px column", () => {
    assert.equal(isNarrowViewport(375), true);
    assert.equal(isNarrowViewport(360), true);
    assert.equal(isNarrowViewport(412), true);
  });

  it("includes the breakpoint itself (max-width: 720px)", () => {
    assert.equal(isNarrowViewport(NARROW_BREAKPOINT), true);
    assert.equal(isNarrowViewport(NARROW_BREAKPOINT + 1), false);
  });

  it("leaves desktop 1440×900 on the docked three-pane layout", () => {
    assert.equal(isNarrowViewport(1440), false);
    assert.equal(isNarrowViewport(900), false);
    assert.equal(isNarrowViewport(721), false);
  });

  it("rejects non-finite widths", () => {
    assert.equal(isNarrowViewport(Number.NaN), false);
    assert.equal(isNarrowViewport(Number.POSITIVE_INFINITY), false);
  });
});

describe("shellClassName contract", () => {
  it("is a plain .shell on desktop even if a drawer flag is stale", () => {
    assert.equal(shellClassName({ narrow: false, drawerOpen: false }), "shell");
    assert.equal(shellClassName({ narrow: false, drawerOpen: true }), "shell");
  });

  it("adds .narrow on a phone so CSS can un-dock the sidebar", () => {
    const cls = shellClassName({ narrow: true, drawerOpen: false });
    assert.equal(cls, "shell narrow");
    assert.match(cls, /\bnarrow\b/);
    assert.doesNotMatch(cls, /drawer-open/);
  });

  it("adds .drawer-open only while the overlay is showing on a narrow viewport", () => {
    assert.equal(
      shellClassName({ narrow: true, drawerOpen: true }),
      "shell narrow drawer-open",
    );
  });
});

describe("NARROW_MEDIA", () => {
  it("matches the CSS max-width used in styles.css (grep NARROW_BREAKPOINT)", () => {
    assert.equal(NARROW_MEDIA, `(max-width: ${NARROW_BREAKPOINT}px)`);
    assert.equal(NARROW_BREAKPOINT, 720);
  });
});

describe("computeKeyboardInset", () => {
  it("returns 0 when visualViewport is missing", () => {
    assert.equal(computeKeyboardInset(null, 812), 0);
    assert.equal(computeKeyboardInset(undefined, 812), 0);
  });

  it("returns 0 for URL-bar jitter below the minimum", () => {
    assert.equal(
      computeKeyboardInset({ height: 812 - (KEYBOARD_INSET_MIN_PX - 1), offsetTop: 0 }, 812),
      0,
    );
  });

  it("reports the obscured region for a real soft keyboard", () => {
    // 812 layout, 480 visual, no offset → 332px keyboard
    assert.equal(computeKeyboardInset({ height: 480, offsetTop: 0 }, 812), 332);
  });

  it("subtracts visualViewport.offsetTop (iOS nudge)", () => {
    assert.equal(computeKeyboardInset({ height: 480, offsetTop: 40 }, 812), 292);
  });
});
