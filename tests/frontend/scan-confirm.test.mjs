// Regression tests for the scan-target confirmation dialog (src/reclaim/api/static/app.js) --
// added after a development-time incident where a quick-scan shortcut resolved to a real,
// unintended path with no visible pause before the scan started (see PLAN.md). Mirrors
// xss.test.mjs/simple-mode.test.mjs's harness pattern (a real JSDOM + the production app.js
// module, not a reimplementation).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  '<!doctype html><html><body>' +
    '<div id="scan-confirm-dialog" hidden><code id="scan-confirm-path"></code></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { openScanConfirmDialog, closeScanConfirmDialog } = await import(
  "../../src/reclaim/api/static/app.js"
);

test("openScanConfirmDialog shows the dialog and renders the exact path", () => {
  openScanConfirmDialog("C:/Users/gaura/Downloads");
  const dialog = document.getElementById("scan-confirm-dialog");
  assert.equal(dialog.hidden, false, "dialog must become visible");
  assert.equal(document.getElementById("scan-confirm-path").textContent, "C:/Users/gaura/Downloads");
});

test("openScanConfirmDialog: an attacker-controlled-looking path renders as inert text", () => {
  const payload = '<img src=x onerror="window.__scanConfirmXssFired = true">';
  openScanConfirmDialog(payload);
  const pathEl = document.getElementById("scan-confirm-path");
  assert.equal(pathEl.querySelectorAll("img").length, 0, "payload must not parse into an <img>");
  assert.equal(globalThis.window.__scanConfirmXssFired, undefined, "onerror must never execute");
  assert.equal(pathEl.textContent, payload, "the raw payload must survive as literal text");
});

test("closeScanConfirmDialog hides the dialog", () => {
  openScanConfirmDialog("C:/Users/gaura/Downloads");
  closeScanConfirmDialog();
  const dialog = document.getElementById("scan-confirm-dialog");
  assert.equal(dialog.hidden, true, "dialog must be hidden again after close");
});
