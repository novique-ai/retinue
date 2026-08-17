/**
 * Named retainers get a commissioned portrait. Everyone else hashes into
 * the 3-image clay pool so the sidebar never falls back to a letter.
 *
 * The identity PR (#78 / c2929aae2) dropped that pool so unnamed agents
 * became initials. That was a visible regression: the live UI had been
 * showing an icon for every retainer. Restore the pool.
 */
const NAMED = new Set(["admin", "editor", "envoy", "janitor", "scout", "scribe"]);
const POOL = 3;

export const LOGO_SRC = "icons/logo.png";
/** The human's illustrated portrait (glasses and vest) in the same clay
 * character family as the named-agent artwork. See `UserAvatar` in App.tsx. */
export const YOU_SRC = "icons/you.png";

function hashSlug(slug: string): number {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
}

/** PNG path for a slug. Named retainers get a unique file; everyone else
 * a stable pool slot. Never returns null for a non-empty slug. */
export function agentIcon(slug: string): string {
  const s = (slug || "").toLowerCase();
  if (NAMED.has(s)) return `icons/agents/${s}.png`;
  return `icons/pool/${hashSlug(s) % POOL}.png`;
}
