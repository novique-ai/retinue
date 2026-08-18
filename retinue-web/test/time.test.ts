import assert from "node:assert/strict";
import { test } from "node:test";
import { formatMessageTime, messageTimeMs } from "../src/time";

const NOW = Date.parse("2026-08-12T20:14:00-05:00");

function localDay(daysAgo: number, hour: number, minute: number): number {
  const now = new Date(NOW);
  return (
    new Date(now.getFullYear(), now.getMonth(), now.getDate() - daysAgo, hour, minute, 0).getTime() /
    1000
  );
}

test("messageTimeMs treats unix seconds and ms, hides empty", () => {
  assert.equal(messageTimeMs(0), null);
  assert.equal(messageTimeMs(-1), null);
  assert.equal(messageTimeMs(Number.NaN), null);
  assert.equal(messageTimeMs(1_776_000_000), 1_776_000_000_000);
  assert.equal(messageTimeMs(1_776_000_000_000), 1_776_000_000_000);
});

test("today / yesterday / weekday / older", () => {
  assert.match(formatMessageTime(localDay(0, 15, 14), NOW), /^Today at /);
  assert.match(formatMessageTime(localDay(1, 23, 8), NOW), /^Yesterday at /);
  const twoAgo = new Date(localDay(2, 9, 2) * 1000);
  const weekday = twoAgo.toLocaleDateString(undefined, { weekday: "long" });
  assert.equal(formatMessageTime(localDay(2, 9, 2), NOW), `${weekday} at ${twoAgo.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`);
  const older = formatMessageTime(localDay(40, 9, 2), NOW);
  assert.match(older, /at /);
  assert.ok(!older.startsWith("Today"));
  assert.ok(!older.startsWith("Yesterday"));
  const lastYear = formatMessageTime(localDay(400, 9, 2), NOW);
  assert.match(lastYear, /2025/);
});

test("missing ts is blank", () => {
  assert.equal(formatMessageTime(0, NOW), "");
});
