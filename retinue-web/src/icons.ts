/** Named retainers get a hand-drawn icon; everyone else hashes into the pool. */

const NAMED = new Set(["admin", "editor", "envoy", "janitor", "scout", "scribe"]);
const POOL = 3;

export const LOGO_SRC = "icons/logo.png";
export const YOU_SRC = "icons/you.png";

function hashSlug(slug: string): number {
  let h = 0;
  for (const c of slug) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h;
}

export function agentIcon(slug: string): string {
  const s = (slug || "").toLowerCase();
  if (NAMED.has(s)) return `icons/agents/${s}.png`;
  return `icons/pool/${hashSlug(s) % POOL}.png`;
}

export function speakerIcon(speaker: string, userName: string): string {
  if (!speaker || speaker === userName || speaker === "You") return YOU_SRC;
  return agentIcon(speaker);
}
