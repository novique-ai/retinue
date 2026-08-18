/** Discord-style room timestamps from RoomMsg.ts (unix seconds, or ms). */

const DAY_MS = 86_400_000;

export function messageTimeMs(ts: number): number | null {
  if (!Number.isFinite(ts) || ts <= 0) return null;
  const ms = ts < 1e12 ? ts * 1000 : ts;
  return Number.isFinite(ms) ? ms : null;
}

function startOfLocalDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function clock(then: Date): string {
  return then.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** Tiny label: "Today at 3:14 PM", "Yesterday at 11:08 PM", "Monday at 9:02 AM". */
export function formatMessageTime(ts: number, nowMs: number = Date.now()): string {
  const ms = messageTimeMs(ts);
  if (ms === null) return "";
  const then = new Date(ms);
  if (Number.isNaN(then.getTime())) return "";
  const now = new Date(nowMs);
  const time = clock(then);
  const dayDiff = Math.round((startOfLocalDay(now) - startOfLocalDay(then)) / DAY_MS);
  if (dayDiff <= 0) return `Today at ${time}`;
  if (dayDiff === 1) return `Yesterday at ${time}`;
  if (dayDiff < 7) {
    const weekday = then.toLocaleDateString(undefined, { weekday: "long" });
    return `${weekday} at ${time}`;
  }
  const date = then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(then.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  return `${date} at ${time}`;
}
