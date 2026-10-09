// DOM-level tests for the assistant (MCP) approval card (docs/specs/assistant-mcp.md section 5).
// Same harness as xss.test.mjs: a real JSDOM + the real app.js module. The card is built from
// server data that includes FILE NAMES (untrusted text), so it must render them inert, and the
// Approve / Decline buttons must call exactly the decide endpoints and nothing else.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<button id="mode-badge" data-mode="power"></button>' +
    '<section id="mcp-approvals" hidden><div id="mcp-approvals-list"></div></section>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.fetch = async () => {
  throw new Error("unexpected fetch");
};

const { renderMcpApprovalCard } = await import("../../src/reclaim/api/static/app.js");

const approval = (over = {}) => ({
  id: "abc123",
  status: "pending",
  client_id: "Claude Desktop",
  rule_id_or_category: "dev_artifact_node_modules",
  item_count: 7,
  bytes_total: 5 * 1024 * 1024,
  sample_paths: ["C:/Users/x/proj/node_modules"],
  protected_names: ["fr-en-transformer", "intent-router"],
  method: "vault",
  reversible_until_days: 30,
  expires_at: Date.now() / 1000 + 600,
  ...over,
});

test("card states what, how many, how big, how it is undone, and who is protected", () => {
  const card = renderMcpApprovalCard(approval(), async () => {});
  const text = card.textContent;
  assert.match(text, /Claude Desktop wants to clean 7 item\(s\), 5\.0 MB/);
  assert.match(text, /dev_artifact_node_modules/);
  assert.match(text, /Nothing is permanently deleted/);
  assert.match(text, /at least 30 day\(s\)/);
  assert.match(text, /fr-en-transformer, intent-router/);
  assert.deepEqual(
    [...card.querySelectorAll("button")].map((b) => b.textContent),
    ["Approve", "Decline"]
  );
});

test("recycle-bin method and empty protected list are stated honestly", () => {
  const card = renderMcpApprovalCard(
    approval({ method: "recycle_bin", reversible_until_days: null, protected_names: [] }),
    async () => {}
  );
  assert.match(card.textContent, /Recycle Bin/);
  assert.match(card.textContent, /protected list is empty/);
});

test("hostile file names and client ids render as inert text", () => {
  const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
  const card = renderMcpApprovalCard(
    approval({ client_id: evil, sample_paths: [evil], rule_id_or_category: evil }),
    async () => {}
  );
  document.body.append(card);
  assert.equal(card.querySelectorAll("img, script").length, 0);
  assert.equal(window.__pwned, undefined);
  assert.ok(card.textContent.includes(evil));
});

test("long selections are summarised, not dumped", () => {
  const card = renderMcpApprovalCard(
    approval({ item_count: 40, sample_paths: Array.from({ length: 20 }, (_, i) => `C:/p${i}`) }),
    async () => {}
  );
  assert.equal(card.querySelectorAll(".rc-approval-paths li").length, 6); // 5 paths + "and 35 more"
  assert.match(card.textContent, /and 35 more/);
});

test("Approve and Decline call decide with the right action; buttons lock while sending", async () => {
  const calls = [];
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const card = renderMcpApprovalCard(approval(), async (id, action) => {
    calls.push([id, action]);
    await gate;
  });
  const [approve, decline] = card.querySelectorAll("button");
  approve.click();
  assert.deepEqual(calls, [["abc123", "approve"]]);
  assert.equal(approve.disabled && decline.disabled, true, "double-click protection");
  assert.equal(approve.textContent, "Approving...");
  release();
  await new Promise((r) => setTimeout(r, 0));
});

test("a failed answer re-enables the buttons and shows the error", async () => {
  const card = renderMcpApprovalCard(approval(), async () => {
    throw new Error("This request was already answered or has expired.");
  });
  const [approve, decline] = card.querySelectorAll("button");
  decline.click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(approve.disabled || decline.disabled, false);
  const error = card.querySelector(".rc-form-error");
  assert.equal(error.hidden, false);
  assert.match(error.textContent, /already answered or has expired/);
  assert.equal(decline.textContent, "Decline");
});
