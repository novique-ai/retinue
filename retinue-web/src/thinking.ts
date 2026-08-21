/** Drop the in-room thinking bubble when a turn ends without an agent line.

Keep the system-notice prefixes in lockstep with
`plugins/platforms/retinue_rooms/engine.py`.
*/

export type TranscriptMsg = {
  kind: string;
  speaker: string;
  text: string;
};

const CYCLE_INTERNAL_ERROR_PREFIX = "internal error running the turn cycle";
const CYCLE_BUDGET_PREFIX = "turn budget";
const CYCLE_STOPPED_PREFIX = "Stopped.";
const CYCLE_ROUND_BUDGET_PREFIX = "⚠️ round budget reached";
const DID_NOT_REPLY_INFIX = " did not reply (";

export function isCycleAbortNotice(text: string): boolean {
  return (
    text.startsWith(CYCLE_INTERNAL_ERROR_PREFIX) ||
    text.startsWith(CYCLE_BUDGET_PREFIX) ||
    text.startsWith(CYCLE_STOPPED_PREFIX) ||
    text.startsWith(CYCLE_ROUND_BUDGET_PREFIX)
  );
}

export function turnConcludesWaiter(msg: TranscriptMsg, waiter: string): boolean {
  if (msg.kind === "agent" && msg.speaker === waiter) return true;
  if (msg.kind !== "system") return false;
  const text = msg.text || "";
  if (isCycleAbortNotice(text)) return true;
  return text.startsWith(waiter + DID_NOT_REPLY_INFIX);
}

export function remainingThinkers(
  waiting: string[],
  fresh: TranscriptMsg[],
): string[] {
  if (!waiting.length || !fresh.length) return waiting;
  if (fresh.some((m) => m.kind === "system" && isCycleAbortNotice(m.text || ""))) {
    return [];
  }
  return waiting.filter((w) => !fresh.some((m) => turnConcludesWaiter(m, w)));
}

/** Self-heal: only messages after the latest user line can end this turn. */
export function remainingThinkersAfter(
  waiting: string[],
  messages: TranscriptMsg[],
): string[] {
  if (!waiting.length) return waiting;
  let lastUser = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].kind === "user") {
      lastUser = i;
      break;
    }
  }
  if (lastUser < 0) return waiting;
  return remainingThinkers(waiting, messages.slice(lastUser + 1));
}

export type BusyAgent = { busy?: boolean; busy_rooms?: string[] };

/** Is this agent mid-turn *in this room*?

`busy` is gateway-global — true while the agent has a turn in any room — so
reading it inside a room view painted a thinking bubble on every other room the
agent belongs to, which reads as a hung turn. `busy_rooms` carries the rooms
those turns are actually in.

A gateway that predates `busy_rooms` omits the field entirely; fall back to the
global flag there rather than silently dropping every bubble. An empty array is
not that case — it means busy in no room, and must not fall back.
*/
export function isWorkingIn(
  agent: BusyAgent | undefined,
  roomId: string,
): boolean {
  if (!agent?.busy) return false;
  if (agent.busy_rooms === undefined) return true;
  return agent.busy_rooms.includes(roomId);
}

export function includeBusyThinkers(
  waiting: string[],
  members: string[],
  agentsBySlug: Record<string, BusyAgent | undefined>,
  roomId: string,
): string[] {
  const merged = [...waiting];
  const seen = new Set(waiting);
  for (const member of members) {
    if (seen.has(member) || !isWorkingIn(agentsBySlug[member], roomId)) continue;
    seen.add(member);
    merged.push(member);
  }
  return merged;
}
