/**
 * Bespoke hand-drawn/3D-rendered artwork exists for six named retainer
 * slugs — commissioned, on-brand character illustrations, not placeholders.
 * Everyone else falls back to the identity system's initial-over-colour
 * badge (see `AgentAvatar` in App.tsx), not a shared placeholder pool: a
 * pool of 3 images reused across every unnamed agent is exactly the "they
 * all look the same" problem #78 exists to fix.
 */
const NAMED = new Set(["admin", "editor", "envoy", "janitor", "scout", "scribe"]);

export const LOGO_SRC = "icons/logo.png";
/** The human's illustrated portrait (glasses and vest) in the same clay
 * character family as the named-agent artwork. See `UserAvatar` in App.tsx. */
export const YOU_SRC = "icons/you.png";

/** PNG path for one of the six named slugs, or null — never a shared pool
 * fallback. Callers fall back to the identity badge when this is null. */
export function agentIcon(slug: string): string | null {
  const s = (slug || "").toLowerCase();
  return NAMED.has(s) ? `icons/agents/${s}.png` : null;
}
