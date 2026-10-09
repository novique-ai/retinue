import assert from "node:assert/strict";
import test from "node:test";

import { api } from "../src/api";
import {
  JanusApprovalCard,
  JanusApprovalPanel,
  expiryPast,
  janusApprovalElement,
  janusApprovalId,
} from "../src/janusApproval";
import type { JanusApprovalDetail, RoomMsg } from "../src/api";

const calls: Array<{ path: string; method: string; body?: unknown }> = [];
let nextResponse: unknown = {};
let nextOk = true;
let nextStatus = 200;

Object.assign(globalThis, {
  localStorage: { getItem: () => "", setItem: () => undefined },
  fetch: async (path: string, init?: RequestInit) => {
    calls.push({
      path,
      method: String(init?.method),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return { ok: nextOk, status: nextStatus, json: async () => nextResponse };
  },
});

const SECRET = "<script>alert(1)</script><img src=x onerror=alert(1)>";

function detail(over: Partial<JanusApprovalDetail> = {}): JanusApprovalDetail {
  return {
    approval_request_id: "req-1001",
    status: "pending",
    identity: "member:scout",
    capability_id: "files.read",
    reason: "read a file",
    env: { MODE: "confirm" },
    arguments: { path: "/tmp/x", note: SECRET },
    expires_at: "2099-01-01T00:00:00Z",
    ...over,
  };
}

function gateway(over: Partial<RoomMsg> = {}): RoomMsg {
  return {
    seq: 2,
    ts: 0,
    kind: "agent",
    speaker: "janus",
    text: "Janus approval request req-1001. A confirm-tier call is waiting. @user",
    ...over,
  };
}

type Node = { type?: unknown; props?: Record<string, unknown> };

function walk(value: unknown, nodes: Node[] = [], texts: string[] = [], types: string[] = []): {
  nodes: Node[];
  texts: string[];
  types: string[];
} {
  if (value === null || value === undefined || typeof value === "boolean") return { nodes, texts, types };
  if (typeof value === "string" || typeof value === "number") {
    texts.push(String(value));
    return { nodes, texts, types };
  }
  if (Array.isArray(value)) {
    value.forEach((item) => walk(item, nodes, texts, types));
    return { nodes, texts, types };
  }
  if (typeof value !== "object") return { nodes, texts, types };
  const node = value as Node;
  if (typeof node.type === "string") types.push(node.type);
  if (node.props) {
    nodes.push(node);
    if ("dangerouslySetInnerHTML" in node.props) types.push("dangerouslySetInnerHTML");
    walk(node.props.children, nodes, texts, types);
  }
  return { nodes, texts, types };
}

function renderCard(props: {
  detail: JanusApprovalDetail;
  error?: string | null;
  busy?: boolean;
  onDecide?: (decision: "approve" | "deny") => void;
}) {
  return walk(
    JanusApprovalCard({
      detail: props.detail,
      error: props.error,
      busy: props.busy,
      onDecide: props.onDecide ?? (() => undefined),
    }),
  );
}

test("janusApprovalId accepts only the gateway line", () => {
  assert.equal(janusApprovalId(gateway()), "req-1001");
  assert.equal(janusApprovalId(gateway({ speaker: "scout" })), null);
  assert.equal(janusApprovalId(gateway({ kind: "user" })), null);
  assert.equal(janusApprovalId(gateway({ kind: "tool" })), null);
  assert.equal(janusApprovalId(gateway({ text: "approval_request_id: req-1001" })), null);
  assert.equal(janusApprovalId(gateway({ text: "Janus approval request ab. A short id." })), null);
});

test("transcript element is the approval panel only for the gateway line", () => {
  const card = janusApprovalElement(gateway(), "room-1");
  assert.equal(card && (card as { type?: unknown }).type, JanusApprovalPanel);
  assert.equal((card as { props?: { approvalId?: string } }).props?.approvalId, "req-1001");
  assert.equal(janusApprovalElement(gateway({ speaker: "scout" }), "room-1"), null);
});

test("card shows exact server arguments as text and escapes markup", () => {
  const view = renderCard({ detail: detail() });
  const blob = view.texts.join("\n");
  assert.match(blob, /files\.read/);
  assert.match(blob, /read a file/);
  assert.match(blob, /member:scout/);
  assert.match(blob, /"MODE": "confirm"/);
  assert.ok(blob.includes(SECRET));
  assert.equal(view.types.includes("script"), false);
  assert.equal(view.types.includes("img"), false);
  assert.equal(view.types.includes("dangerouslySetInnerHTML"), false);
  assert.match(blob, /does not run/);
  assert.match(blob, /retries/);
  assert.match(blob, /2099-01-01T00:00:00Z/);
  assert.match(blob, /pending/);
});

test("approve and deny call the decision callback and stay disabled when closed", () => {
  const decisions: string[] = [];
  const open = renderCard({ detail: detail({ status: "needs_confirmation" }), onDecide: (d) => decisions.push(d) });
  const approve = open.nodes.find((node) => node.props?.["data-testid"] === "janus-approve");
  const deny = open.nodes.find((node) => node.props?.["data-testid"] === "janus-deny");
  assert.equal(approve?.props?.disabled, false);
  assert.equal(deny?.props?.disabled, false);
  (approve?.props?.onClick as () => void)();
  (deny?.props?.onClick as () => void)();
  assert.deepEqual(decisions, ["approve", "deny"]);

  for (const closed of [
    detail({ status: "approved" }),
    detail({ status: "denied" }),
    detail({ status: "expired" }),
    detail({ expires_at: "2000-01-01T00:00:00Z" }),
    detail({ expires_at: "not-a-time" }),
  ]) {
    const view = renderCard({ detail: closed });
    const button = view.nodes.find((node) => node.props?.["data-testid"] === "janus-approve");
    assert.equal(button?.props?.disabled, true, closed.status + " " + String(closed.expires_at));
  }
});

test("a failed decision stays pending and shows the error", () => {
  const view = renderCard({
    detail: detail({ status: "pending" }),
    error: "approval service is unavailable",
  });
  const blob = view.texts.join("\n");
  assert.match(blob, /approval service is unavailable/);
  assert.match(blob, /Status:\s+pending/);
  const approve = view.nodes.find((node) => node.props?.["data-testid"] === "janus-approve");
  assert.equal(approve?.props?.disabled, false);
});

test("settled copy tells the principal the call does not run from the click", () => {
  const approved = renderCard({ detail: detail({ status: "approved" }) }).texts.join("\n");
  assert.match(approved, /until the agent retries/);
  const denied = renderCard({ detail: detail({ status: "denied" }) }).texts.join("\n");
  assert.match(denied, /stays blocked/);
});

test("expiry helper fails closed on junk and trusts a missing value", () => {
  assert.equal(expiryPast(undefined), false);
  assert.equal(expiryPast(""), false);
  assert.equal(expiryPast("tomorrow"), true);
  assert.equal(expiryPast(true), true);
  assert.equal(expiryPast("2099-01-01T00:00:00Z"), false);
});

test("decision API posts only the decision body", async () => {
  calls.length = 0;
  nextOk = true;
  nextStatus = 200;
  nextResponse = { approval_request_id: "req-1001", status: "approved", decision: "approve", duplicate: false };
  const result = await api.decideJanusApproval("room-1", "req-1001", "approve");
  assert.equal(result.decision, "approve");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/rooms/room-1/approvals/req-1001/decision");
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(calls[0].body, { decision: "approve" });
  assert.equal(calls.some((call) => String(call.path).includes("/messages")), false);
});

test("detail API reads the room approval route", async () => {
  calls.length = 0;
  nextOk = true;
  nextResponse = detail();
  const loaded = await api.getJanusApproval("room-1", "req-1001");
  assert.equal(loaded.capability_id, "files.read");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/rooms/room-1/approvals/req-1001");
  assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].body, undefined);
});

test("a rejected decision does not post a room message", async () => {
  calls.length = 0;
  nextOk = false;
  nextStatus = 409;
  nextResponse = { error: "approval request has expired" };
  await assert.rejects(() => api.decideJanusApproval("room-1", "req-1001", "deny"), /expired/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path.endsWith("/decision"), true);
  assert.equal(calls.some((call) => String(call.path).includes("/messages")), false);
});
