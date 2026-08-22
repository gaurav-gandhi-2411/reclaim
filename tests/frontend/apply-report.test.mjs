// Regression test for renderApplyReport's bytes-outcome wording
// (src/reclaim/api/static/app.js). Advanced mode's Review Queue apply flow lets the user pick
// the quarantine method via the #apply-method dropdown (see templates/index.html), so — same as
// Simple mode's renderQuickCleanResult — the summary must never claim bytes were "freed" when
// they were only moved to the Recycle Bin or the vault (both recoverable); only direct_delete
// really frees the space immediately. See the house-rule comment above applyReportBytesPhrase.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM('<!doctype html><html><body><div id="apply-result"></div></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { renderApplyReport } = await import("../../src/reclaim/api/static/app.js");

function container() {
  return document.getElementById("apply-result");
}

function baseReport(overrides) {
  return {
    batch_id: "batch-123",
    apply: true,
    files_succeeded: 3,
    files_processed: 3,
    files_failed: 0,
    bytes_freed: 2048,
    bytes_freed_human: "2.0 KB",
    category_breakdown: [],
    disk_free_delta_bytes: null,
    disk_free_before_bytes: null,
    disk_free_after_bytes: null,
    items: [],
    method: "direct_delete",
    ...overrides,
  };
}

test("renderApplyReport: recycle_bin apply never says 'freed' -- says moved, with the empty-bin hint", () => {
  renderApplyReport(container(), baseReport({ method: "recycle_bin", apply: true }));
  const text = container().textContent;
  assert.ok(
    text.includes("moved to the Recycle Bin — empty the Recycle Bin to free the space."),
    `expected recycle_bin apply wording, got: ${text}`
  );
  assert.equal(text.includes("freed"), false, "recycle_bin must never claim bytes were freed");
});

test("renderApplyReport: recycle_bin dry-run says 'would be moved', not 'would be freed'", () => {
  renderApplyReport(container(), baseReport({ method: "recycle_bin", apply: false }));
  const text = container().textContent;
  assert.ok(text.includes("would be moved to the Recycle Bin."), `got: ${text}`);
  assert.equal(text.includes("freed"), false, "recycle_bin dry-run must never claim bytes were freed");
});

test("renderApplyReport: vault apply says moved to the vault, restorable, never 'freed'", () => {
  renderApplyReport(container(), baseReport({ method: "vault", apply: true }));
  const text = container().textContent;
  assert.ok(
    text.includes("moved to the Reclaim vault") && text.includes("restorable"),
    `expected vault apply wording, got: ${text}`
  );
  assert.equal(text.includes("freed"), false, "vault must never claim bytes were freed");
});

test("renderApplyReport: vault dry-run says 'would be moved', not 'would be freed'", () => {
  renderApplyReport(container(), baseReport({ method: "vault", apply: false }));
  const text = container().textContent;
  assert.ok(text.includes("would be moved to the Reclaim vault."), `got: ${text}`);
  assert.equal(text.includes("freed"), false, "vault dry-run must never claim bytes were freed");
});

test("renderApplyReport: direct_delete apply correctly says 'permanently freed'", () => {
  renderApplyReport(container(), baseReport({ method: "direct_delete", apply: true }));
  const text = container().textContent;
  assert.ok(text.includes("permanently freed."), `got: ${text}`);
});

test("renderApplyReport: direct_delete dry-run says 'would be permanently freed'", () => {
  renderApplyReport(container(), baseReport({ method: "direct_delete", apply: false }));
  const text = container().textContent;
  assert.ok(text.includes("would be permanently freed."), `got: ${text}`);
});
