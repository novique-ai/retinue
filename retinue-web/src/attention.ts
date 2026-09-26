import type { RoomMeta } from "./api";

/**
 * What a room wants from the principal (#256).
 *
 * - `waiting`: a retainer @mentioned them. The room is paused: no agent
 *   turn starts until they post.
 * - `answered`: a retainer answered them without asking anything. Shown so
 *   they can read it; the room keeps working.
 * - `idle`: nothing for them.
 */
export type RoomAttention = "waiting" | "answered" | "idle";

export function roomAttention(
  room: Pick<RoomMeta, "needs_user" | "answered_user">,
): RoomAttention {
  if (room.needs_user) return "waiting";
  if (room.answered_user) return "answered";
  return "idle";
}

export interface AttentionBadge {
  label: string;
  title: string;
  className: string;
}

const BADGES: Record<Exclude<RoomAttention, "idle">, AttentionBadge> = {
  waiting: {
    label: "paused — needs you",
    title: "A retainer asked for you. The room is paused until you reply.",
    className: "needs-you-badge",
  },
  answered: {
    label: "answer for you",
    title: "A retainer answered you. Nothing is waiting; the room keeps going.",
    className: "answered-badge",
  },
};

export function attentionBadge(state: RoomAttention): AttentionBadge | null {
  return state === "idle" ? null : BADGES[state];
}

/** Rail / nav accessible label for a room in the given state. */
export function attentionLabel(name: string, state: RoomAttention): string {
  const badge = attentionBadge(state);
  return badge ? `${name} — ${badge.label}` : name;
}

/** The principal just posted: the server clears both flags (see engine). */
export function clearedByPrincipalPost<T extends RoomMeta>(room: T): T {
  return { ...room, needs_user: false, answered_user: false };
}

/** System line the server posts for a post the barrier held (engine.HELD_POST_PREFIX). */
export const HELD_POST_PREFIX = "Held:";

export function isHeldNotice(text: string): boolean {
  return (text || "").startsWith(HELD_POST_PREFIX);
}
