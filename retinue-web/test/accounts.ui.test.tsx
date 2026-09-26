// Settings → Claude account: subscription first, API key never the default (#252).
import assert from "node:assert/strict";
import test from "node:test";

import { ClaudeAccountControls, claudeLoginLabel } from "../src/accounts";
import type { AccountRow } from "../src/accounts";

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

function textOf(value: unknown): string {
  if (value === null || value === undefined || typeof value === "boolean") return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(textOf).join("");
  return textOf((value as Node).props?.children);
}

function render(acct: AccountRow, onSave: () => void = () => undefined): Node[] {
  return nodesFrom(
    ClaudeAccountControls({
      acct,
      apiKey: "",
      onApiKeyChange: () => undefined,
      onSaveApiKey: onSave,
    }),
  );
}

const inputs = (nodes: Node[]) => nodes.filter((n) => n.type === "input");
const byTestId = (nodes: Node[], id: string) =>
  nodes.find((n) => n.props?.["data-testid"] === id);

test("subscription login shows signed-in state and no API-key input", () => {
  const acct: AccountRow = { id: "anthropic", status: "ok", login: "subscription" };
  const nodes = render(acct);
  assert.equal(inputs(nodes).length, 0);
  assert.equal(byTestId(nodes, "claude-api-key"), undefined);
  assert.match(textOf(byTestId(nodes, "claude-login")), /Signed in via Claude subscription/);
});

test("API-key login is labelled as API billing and offers no key input", () => {
  const acct: AccountRow = { id: "anthropic", status: "ok", login: "api_key" };
  const nodes = render(acct);
  assert.equal(inputs(nodes).length, 0);
  assert.match(claudeLoginLabel(acct), /API billing/);
});

test("missing: subscription instructions first, API key secondary and labelled billing", () => {
  let saved = 0;
  const acct: AccountRow = { id: "anthropic", status: "missing", login: null };
  const nodes = render(acct, () => {
    saved += 1;
  });
  const help = byTestId(nodes, "claude-subscription-help");
  const keyBlock = byTestId(nodes, "claude-api-key");
  assert.ok(help && keyBlock);
  assert.ok(nodes.indexOf(help) < nodes.indexOf(keyBlock), "subscription help comes first");
  assert.match(textOf(help), /Claude subscription/);
  assert.equal(keyBlock.type, "details", "API key path is collapsed by default");
  assert.equal(keyBlock.props?.open, undefined);
  assert.match(textOf(keyBlock), /API billing/);
  assert.equal(inputs(nodes).length, 1);
  (byTestId(nodes, "claude-api-key-save")?.props?.onClick as () => void)();
  assert.equal(saved, 1);
});

test("expired subscription asks to sign in again", () => {
  const acct: AccountRow = { id: "anthropic", status: "relogin_required", login: "subscription" };
  const nodes = render(acct);
  assert.match(textOf(byTestId(nodes, "claude-subscription-help")), /Sign in again/);
  assert.match(claudeLoginLabel(acct), /expired/);
});
