// Agent-runtime presentation helpers (#218 / #236). Pure — App.tsx renders
// from these so the selection rules are testable without mounting the panel.
import type { AgentMeta, RuntimeInfo } from "./api";

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

const GROK_BUILD_PICKER_PREFIX = "grok-build:";
const HERMES_PICKER_PREFIX = "hermes:";

export function parsePickerValue(value: string): { runtime: string; model: string } {
  const raw = (value || "").trim();
  if (raw.startsWith(GROK_BUILD_PICKER_PREFIX)) {
    return { runtime: GROK_BUILD_RUNTIME, model: raw.slice(GROK_BUILD_PICKER_PREFIX.length) };
  }
  if (raw.startsWith(HERMES_PICKER_PREFIX)) {
    return { runtime: HERMES_RUNTIME, model: raw.slice(HERMES_PICKER_PREFIX.length) };
  }
  return { runtime: HERMES_RUNTIME, model: raw };
}

export function formatPickerValue(runtime: string | undefined, model: string): string {
  const id = (model || "").trim();
  if (runtime === GROK_BUILD_RUNTIME) return `${GROK_BUILD_PICKER_PREFIX}${id}`;
  return id;
}

export function agentPickerValue(agent: Pick<AgentMeta, "runtime" | "model_preset" | "runtime_model">): string {
  if (isGrokBuild(agent.runtime)) {
    return formatPickerValue(GROK_BUILD_RUNTIME, agent.runtime_model || "");
  }
  return agent.model_preset || "";
}

export function isCrossRuntime(currentRuntime: string | undefined, nextValue: string): boolean {
  const current = isGrokBuild(currentRuntime) ? GROK_BUILD_RUNTIME : HERMES_RUNTIME;
  return current !== parsePickerValue(nextValue).runtime;
}

export const CROSS_RUNTIME_CONFIRM =
  "This switches the retainer to a different agent runtime. Sessions reset. Grok Build runs tools on this machine in the room's project tree, not in the room container, and uses a longer turn budget. Continue?";
