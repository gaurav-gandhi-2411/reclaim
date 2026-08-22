// Regression tests for the whole-drive-scan confirmation dialog
// (src/reclaim/api/static/app.js) -- P0 fix, 2026-08-22 real-disk finding. A real smoke-test scan
// found SIMPLE mode's previous default ("Clean My Computer" -> POST /api/scan/full-drive, a
// volume-root traversal) reached other local accounts' profile directories on a real
// multi-project dev machine. "Scan the whole drive" now requires this explicit, separately-
// surfaced opt-in dialog before POST /api/scan/full-drive can ever be reached -- see
// service.user_scan_roots's docstring for the full incident. Mirrors scan-confirm.test.mjs's
// harness pattern (a real JSDOM + the production app.js module, not a reimplementation).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  '<!doctype html><html><body>' + '<div id="full-drive-confirm-dialog" hidden></div>' + "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { openFullDriveConfirmDialog, closeFullDriveConfirmDialog } = await import(
  "../../src/reclaim/api/static/app.js"
);

test("openFullDriveConfirmDialog shows the dialog", () => {
  openFullDriveConfirmDialog();
  const dialog = document.getElementById("full-drive-confirm-dialog");
  assert.equal(dialog.hidden, false, "dialog must become visible");
});

test("closeFullDriveConfirmDialog hides the dialog", () => {
  openFullDriveConfirmDialog();
  closeFullDriveConfirmDialog();
  const dialog = document.getElementById("full-drive-confirm-dialog");
  assert.equal(dialog.hidden, true, "dialog must be hidden again after close");
});
