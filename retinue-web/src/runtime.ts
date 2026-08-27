// Agent-runtime presentation helpers (#218). Pure — App.tsx renders from
// these so the selection rules are testable without mounting the panel.
import type { RuntimeInfo } from "./api";

export const HERMES_RUNTIME = "hermes";
export const GROK_BUILD_RUNTIME = "grok-build";

export function isGrokBuild(runtime?: string | null): boolean {
  return runtime === GROK_BUILD_RUNTIME;
}

export function runtimeHealthLabel(status: string): string {
  switch (status) {
    case "not_installed":
      return "not installed";
    case "auth_required":
      return "login required";
    case "error":
      return "runtime error";
    default:
      return status;
  }
}

/** Button caption: the label, plus why it cannot be picked when it cannot. */
export function runtimeOptionLabel(r: RuntimeInfo): string {
  return r.health.status === "available"
    ? r.label
    : `${r.label} — ${runtimeHealthLabel(r.health.status)}`;
}

/** An unavailable runtime stays visible but not selectable — the user should
 * see that Grok Build exists and what it needs, not wonder where it went. */
export function runtimeSelectable(r: RuntimeInfo, current: string): boolean {
  return r.health.status === "available" || r.id === current;
}

/** Hover text: the description when usable, the blocking reason when not. */
export function runtimeOptionTitle(r: RuntimeInfo): string {
  if (r.health.status === "available") return r.description;
  const label = runtimeHealthLabel(r.health.status);
  return r.health.detail ? `${label} — ${r.health.detail}` : label;
}
