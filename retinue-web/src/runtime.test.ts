// Agent-runtime selection rules + API wiring (#218).
import assert from "node:assert/strict";
import test, { beforeEach } from "node:test";

import type { RuntimeInfo } from "./api";
import { api } from "./api";
import {
  GROK_BUILD_RUNTIME,
  HERMES_RUNTIME,
  agentPickerValue,
  formatPickerValue,
  isCrossRuntime,
  isGrokBuild,
  parsePickerValue,
  runtimeHealthLabel,
  runtimeOptionLabel,
  runtimeOptionTitle,
  runtimeSelectable,
} from "./runtime";

let calls: Array<{ path: string; method: string; body?: Record<string, unknown> }> = [];
let nextResponse: unknown = {};

Object.assign(globalThis, {
  localStorage: { getItem: () => "", setItem: () => undefined },
  fetch: async (path: string, init: RequestInit) => {
    calls.push({
      path,
      method: String(init.method),
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    });
    return { ok: true, status: 200, json: async () => nextResponse };
  },
});

beforeEach(() => {
  calls = [];
  nextResponse = {};
});

function rt(overrides: Partial<RuntimeInfo> = {}): RuntimeInfo {
  return {
    id: GROK_BUILD_RUNTIME,
    label: "Grok Build",
    description: "xAI's native agent harness.",
    capabilities: {},
    health: { status: "available" },
    ...overrides,
  };
}

test("available runtimes are selectable with their plain label", () => {
  const r = rt();
  assert.equal(runtimeSelectable(r, HERMES_RUNTIME), true);
  assert.equal(runtimeOptionLabel(r), "Grok Build");
  assert.equal(runtimeOptionTitle(r), "xAI's native agent harness.");
});

test("unavailable runtimes stay visible but not selectable, with the reason", () => {
  const r = rt({ health: { status: "auth_required", detail: "run grok login" } });
  assert.equal(runtimeSelectable(r, HERMES_RUNTIME), false);
  assert.equal(runtimeOptionLabel(r), "Grok Build — login required");
  assert.equal(runtimeOptionTitle(r), "login required — run grok login");
});

test("the currently selected runtime never disables itself", () => {
  const r = rt({ health: { status: "error", detail: "boom" } });
  assert.equal(runtimeSelectable(r, GROK_BUILD_RUNTIME), true);
});

test("health labels cover every backend status", () => {
  assert.equal(runtimeHealthLabel("not_installed"), "not installed");
  assert.equal(runtimeHealthLabel("auth_required"), "login required");
  assert.equal(runtimeHealthLabel("error"), "runtime error");
  assert.equal(runtimeHealthLabel("available"), "available");
});

test("isGrokBuild only matches the grok-build id", () => {
  assert.equal(isGrokBuild("grok-build"), true);
  assert.equal(isGrokBuild("hermes"), false);
  assert.equal(isGrokBuild(undefined), false);
});

test("listRuntimes hits GET /runtimes", async () => {
  nextResponse = { runtimes: [] };
  await api.listRuntimes();
  assert.deepEqual(calls, [{ path: "/runtimes", method: "GET", body: undefined }]);
});

test("hire posts the runtime and grok catalog model for grok-build", async () => {
  nextResponse = { slug: "gizmo" };
  await api.hire("Gizmo", "build", "well", "grok-4.5", undefined, GROK_BUILD_RUNTIME);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/agents");
  assert.equal(calls[0].body?.runtime, GROK_BUILD_RUNTIME);
  assert.equal(calls[0].body?.model, "grok-4.5");
});

test("hire omits an empty model so the gateway can default it", async () => {
  nextResponse = { slug: "gizmo" };
  await api.hire("Gizmo", "build", "well", "", undefined, GROK_BUILD_RUNTIME);
  assert.equal("model" in (calls[0].body ?? {}), false);
});

test("picker values are qualified for Grok Build and bare for Hermes", () => {
  assert.deepEqual(parsePickerValue("grok-build:grok-4.5"), {
    runtime: GROK_BUILD_RUNTIME,
    model: "grok-4.5",
  });
  assert.deepEqual(parsePickerValue("grok-4.5"), { runtime: HERMES_RUNTIME, model: "grok-4.5" });
  assert.equal(formatPickerValue(GROK_BUILD_RUNTIME, "grok-4.6"), "grok-build:grok-4.6");
  assert.equal(formatPickerValue(HERMES_RUNTIME, "grok-4.5"), "grok-4.5");
  assert.equal(
    agentPickerValue({ runtime: GROK_BUILD_RUNTIME, runtime_model: "grok-4.5", model_preset: null }),
    "grok-build:grok-4.5",
  );
  assert.equal(isCrossRuntime("hermes", "grok-build:grok-4.5"), true);
  assert.equal(isCrossRuntime("grok-build", "grok-build:grok-4.5"), false);
  assert.equal(isCrossRuntime("grok-build", "grok-4.5"), true);
});

test("hire omits the runtime field for a hermes hire", async () => {
  nextResponse = { slug: "plain" };
  await api.hire("Plain", "chat", "kindly", "claude-sonnet-5");
  assert.equal(calls[0].body?.model, "claude-sonnet-5");
  assert.equal("runtime" in (calls[0].body ?? {}), false);
});
