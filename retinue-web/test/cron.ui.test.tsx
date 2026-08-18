import assert from "node:assert/strict";
import test, { beforeEach } from "node:test";

import { api } from "../src/api";
import {
  actionsForJob,
  blankCronForm,
  CronJobModal,
  fmtRunAt,
  formFromRow,
  SaveRoutineModal,
  ScheduledSection,
  scheduleFromForm,
  submitCronJob,
  submitSaveRoutine,
} from "../src/cron";
import type { CronJobRow } from "../src/api";

let calls: Array<{ path: string; method: string; body?: Record<string, unknown> }> = [];
let alerts: string[] = [];
let confirmAnswer = true;
let nextResponse: unknown = {};
let nextOk = true;
let nextStatus = 200;

Object.assign(globalThis, {
  localStorage: { getItem: () => "", setItem: () => undefined },
  window: { confirm: () => confirmAnswer },
  alert: (message: unknown) => alerts.push(String(message)),
  fetch: async (path: string, init: RequestInit) => {
    calls.push({
      path,
      method: String(init.method),
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    });
    return { ok: nextOk, status: nextStatus, json: async () => nextResponse };
  },
});

beforeEach(() => {
  calls = [];
  alerts = [];
  confirmAnswer = true;
  nextResponse = {};
  nextOk = true;
  nextStatus = 200;
});

type Node = { type?: unknown; props?: Record<string, unknown> };

function nodesFrom(value: unknown, output: Node[] = []): Node[] {
  if (value === null || value === undefined || typeof value === "boolean") return output;
  if (Array.isArray(value)) {
    value.forEach((item) => nodesFrom(item, output));
    return output;
  }
  if (typeof value !== "object") return output;
  const node = value as Node;
  if (typeof node.type === "function" && node.props) {
    nodesFrom((node.type as (props: unknown) => unknown)(node.props), output);
    return output;
  }
  if (node.props) {
    output.push(node);
    nodesFrom(node.props.children, output);
  }
  return output;
}

function render(component: (props: never) => unknown, props: unknown): Node[] {
  return nodesFrom(component(props as never));
}

function allTestId(nodes: Node[], id: string): Node[] {
  return nodes.filter((node) => node.props?.["data-testid"] === id);
}

function byTestId(nodes: Node[], id: string): Node {
  const found = allTestId(nodes, id)[0];
  assert.ok(found, `missing data-testid=${id}`);
  return found;
}

function textOf(value: unknown): string {
  if (value === null || value === undefined || typeof value === "boolean") return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(textOf).join("");
  const node = value as Node;
  return textOf(node.props?.children);
}

function job(overrides: Partial<CronJobRow> = {}): CronJobRow {
  return {
    id: "job-1",
    owner: "sally",
    name: "Daily brief",
    prompt: "Prepare it",
    skill: null,
    kind: "reminder",
    routine_slug: null,
    schedule: { kind: "interval", minutes: 120 },
    schedule_display: "every 120m",
    schedule_input: "every 120m",
    next_run_at: "2026-08-19T09:00:00Z",
    last_run_at: null,
    last_status: null,
    last_error: null,
    last_delivery_error: null,
    registration_error: null,
    state: "scheduled",
    enabled: true,
    deliver: "origin",
    room: "room-a",
    room_name: "Room A",
    repeat: {},
    timezone: "UTC",
    ...overrides,
  };
}

const sectionProps = (jobs: CronJobRow[]) => ({
  jobs,
  owners: ["sally", "editor"],
  rooms: [{ id: "room-a", name: "Room A" }, { id: "room-b", name: "Room B" }],
  timezone: "UTC",
  filterOwner: "",
  filterRoom: "",
  onFilterOwner: () => undefined,
  onFilterRoom: () => undefined,
  onEdit: () => undefined,
  onChanged: () => undefined,
});

test("renders one row per job with next and last run", () => {
  const second = job({ id: "job-2", owner: "editor", last_run_at: "2026-08-18T09:00:00Z" });
  const nodes = render(ScheduledSection as never, sectionProps([job(), second]));
  assert.equal(allTestId(nodes, "cron-row").length, 2);
  assert.equal(textOf(allTestId(nodes, "cron-last-run")[0]), "—");
  assert.equal(textOf(allTestId(nodes, "cron-next-run")[0]), fmtRunAt(job().next_run_at));
});

test("owner and room filters filter rows", () => {
  const rows = [job(), job({ id: "job-2", owner: "editor", room: "room-b" })];
  let nodes = render(ScheduledSection as never, { ...sectionProps(rows), filterOwner: "sally" });
  assert.equal(allTestId(nodes, "cron-row").length, 1);
  nodes = render(ScheduledSection as never, { ...sectionProps(rows), filterRoom: "room-b" });
  assert.equal(allTestId(nodes, "cron-row")[0].props?.["data-job-id"], "job-2");
});

test("filters are wired to callbacks", () => {
  const values: string[] = [];
  const nodes = render(ScheduledSection as never, {
    ...sectionProps([job()]), onFilterOwner: (value: string) => values.push(value),
    onFilterRoom: (value: string) => values.push(value),
  });
  (byTestId(nodes, "cron-filter-owner").props?.onChange as Function)({ target: { value: "sally" } });
  (byTestId(nodes, "cron-filter-room").props?.onChange as Function)({ target: { value: "room-a" } });
  assert.deepEqual(values, ["sally", "room-a"]);
});

test("pause dispatches and refreshes", async () => {
  let changed = 0;
  const nodes = render(ScheduledSection as never, { ...sectionProps([job()]), onChanged: () => changed++ });
  await (byTestId(nodes, "cron-action-pause").props?.onClick as Function)();
  assert.deepEqual(calls[0], { path: "/cron/jobs/job-1/pause", method: "POST", body: {} });
  assert.equal(changed, 1);
});

test("resume run delete dispatch the right route and confirm cancel", async () => {
  let nodes = render(ScheduledSection as never, sectionProps([job({ state: "paused" })]));
  await (byTestId(nodes, "cron-action-resume").props?.onClick as Function)();
  await (byTestId(nodes, "cron-action-run").props?.onClick as Function)();
  confirmAnswer = false;
  await (byTestId(nodes, "cron-action-delete").props?.onClick as Function)();
  assert.deepEqual(calls.map((call) => [call.method, call.path]), [
    ["POST", "/cron/jobs/job-1/resume"], ["POST", "/cron/jobs/job-1/run"],
  ]);
  confirmAnswer = true;
  await (byTestId(nodes, "cron-action-delete").props?.onClick as Function)();
  assert.equal(calls[2].method, "DELETE");
});

test("paused row offers resume not pause", () => {
  assert.deepEqual(actionsForJob(job({ state: "paused" })).map((action) => action.key), ["resume", "run", "delete"]);
  assert.deepEqual(actionsForJob(job()).map((action) => action.key), ["pause", "run", "delete"]);
});

test("row shows registration error and timezone", () => {
  const nodes = render(ScheduledSection as never, sectionProps([job({ registration_error: "retry me" })]));
  assert.equal(textOf(byTestId(nodes, "cron-error")), "retry me");
  assert.match(textOf(byTestId(nodes, "cron-timezone")), /UTC/);
});

test("new and edit buttons pass their values", () => {
  const values: unknown[] = [];
  const row = job();
  const nodes = render(ScheduledSection as never, { ...sectionProps([row]), onEdit: (value: unknown) => values.push(value) });
  (byTestId(nodes, "cron-new").props?.onClick as Function)();
  (byTestId(nodes, "cron-edit").props?.onClick as Function)();
  assert.deepEqual(values, [null, row]);
});

test("schedule round trips through form", () => {
  const rows = [
    job({ schedule: { kind: "once", run_at: "2026-08-20T10:00:00" }, schedule_input: "2026-08-20T10:00:00" }),
    job(),
    job({ schedule: { kind: "cron", expr: "0 9 * * *" }, schedule_input: "0 9 * * *" }),
  ];
  rows.forEach((row) => assert.equal(scheduleFromForm(formFromRow(row, "default")), row.schedule_input));
});

function modalProps(form = blankCronForm("sally")) {
  return {
    form,
    owners: ["sally"],
    rooms: [{ id: "room-a", name: "Room A" }],
    timezone: "UTC",
    onChange: () => undefined,
    onClose: () => undefined,
    onSaved: () => undefined,
  };
}

test("modal create submits owner room and schedule", async () => {
  const form = { ...blankCronForm("sally"), room: "room-a", prompt: "go", every: "2", unit: "h" as const };
  const nodes = render(CronJobModal as never, modalProps(form));
  await (byTestId(nodes, "cron-form").props?.onSubmit as Function)({ preventDefault() {} });
  assert.deepEqual(calls[0], {
    path: "/cron/jobs", method: "POST",
    body: { name: "", skill: "", prompt: "go", schedule: "every 2h", enabled: true, room: "room-a", owner: "sally" },
  });
});

test("modal edit submits patch without owner", async () => {
  const form = formFromRow(job(), "default");
  await submitCronJob(form);
  assert.equal(calls[0].method, "PATCH");
  assert.ok(!("owner" in (calls[0].body ?? {})));
});

test("edit a roomless job omits room from patch body", async () => {
  const form = { ...formFromRow(job({ room: null, room_name: "" }), "sally"), name: "Changed" };
  await submitCronJob(form);
  assert.ok(!("room" in (calls[0].body ?? {})));
  assert.equal(calls[0].body?.name, "Changed");
});

test("edit a roomed job keeps sending its room", async () => {
  await submitCronJob(formFromRow(job(), "sally"));
  assert.equal(calls[0].body?.room, "room-a");
});

test("modal submit failure keeps the modal open", async () => {
  let saved = 0;
  let closed = 0;
  nextOk = false;
  nextStatus = 400;
  nextResponse = { error: "bad schedule" };
  const form = { ...blankCronForm("sally"), room: "room-a", prompt: "go" };
  const nodes = render(CronJobModal as never, { ...modalProps(form), onSaved: () => saved++, onClose: () => closed++ });
  await (byTestId(nodes, "cron-form").props?.onSubmit as Function)({ preventDefault() {} });
  assert.equal(saved, 0);
  assert.equal(closed, 0);
  assert.match(alerts[0], /bad schedule/);
});

test("save routine submits owner and optional schedule", async () => {
  const base = { name: "Demo", room: "room-a", owner: "sally", scheduled: true, mode: "every" as const, at: "", every: "2", unit: "h" as const, expr: "" };
  await submitSaveRoutine(base);
  assert.equal(calls[0].body?.schedule, "every 2h");
  calls = [];
  await submitSaveRoutine({ ...base, scheduled: false });
  assert.ok(!("schedule" in (calls[0].body ?? {})));
});

test("save routine modal submits through its form", async () => {
  const form = { name: "Demo", room: "room-a", owner: "sally", scheduled: false, mode: "every" as const, at: "", every: "1", unit: "d" as const, expr: "" };
  const nodes = render(SaveRoutineModal as never, { form, owners: ["sally"], timezone: "UTC", onChange: () => {}, onClose: () => {}, onSaved: () => {} });
  await (byTestId(nodes, "save-routine-form").props?.onSubmit as Function)({ preventDefault() {} });
  assert.equal(calls[0].path, "/routines");
});

test("edit clearing prompt sends explicit empty prompt", async () => {
  await submitCronJob({ ...formFromRow(job(), "sally"), prompt: "" });
  assert.ok("prompt" in (calls[0].body ?? {}));
  assert.equal(calls[0].body?.prompt, "");
});

test("edit clearing skill sends explicit empty skill", async () => {
  await submitCronJob({ ...formFromRow(job({ skill: "brief" }), "sally"), skill: "" });
  assert.ok("skill" in (calls[0].body ?? {}));
  assert.equal(calls[0].body?.skill, "");
});

test("edit a promptless roomless job is submittable", async () => {
  const form = { ...formFromRow(job({ prompt: "", skill: null, room: null }), "sally"), name: "Changed" };
  const nodes = render(CronJobModal as never, modalProps(form));
  assert.equal(byTestId(nodes, "cron-form-submit").props?.disabled, false);
  await (byTestId(nodes, "cron-form").props?.onSubmit as Function)({ preventDefault() {} });
  assert.equal(calls[0].body?.prompt, "");
  assert.equal(calls[0].body?.skill, "");
  assert.ok(!("room" in (calls[0].body ?? {})));
});

test("create requires a room and a prompt or skill", () => {
  let nodes = render(CronJobModal as never, modalProps());
  assert.equal(byTestId(nodes, "cron-form-submit").props?.disabled, true);
  nodes = render(CronJobModal as never, modalProps({ ...blankCronForm("sally"), room: "room-a" }));
  assert.equal(byTestId(nodes, "cron-form-submit").props?.disabled, true);
  nodes = render(CronJobModal as never, modalProps({ ...blankCronForm("sally"), room: "room-a", prompt: "go" }));
  assert.equal(byTestId(nodes, "cron-form-submit").props?.disabled, false);
});
