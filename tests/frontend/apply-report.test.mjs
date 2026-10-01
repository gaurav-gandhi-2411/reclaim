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

// B7: recycle_bin/vault reports carry the 2048 bytes in `bytes_moved` (bytes_freed is 0 --
// nothing is freed until the bin is emptied / vault purged); direct_delete is the reverse.
function baseReport(overrides) {
  const method = overrides.method ?? "direct_delete";
  const moved = method === "recycle_bin" || method === "vault";
  return {
    batch_id: "batch-123",
    apply: true,
    files_succeeded: 3,
    files_processed: 3,
    files_failed: 0,
    bytes_freed: moved ? 0 : 2048,
    bytes_freed_human: moved ? "0 B" : "2.0 KB",
    bytes_moved: moved ? 2048 : 0,
    bytes_moved_human: moved ? "2.0 KB" : "0 B",
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

// P0 residual-gap close (Z5): before this, ANY method value the code didn't explicitly
// recognize -- not just the real direct_delete -- fell through to the same "permanently freed"
// wording, an unverified claim for a method the frontend has never seen before. These two tests
// prove a genuinely unrecognized method value is now handled safely: never "freed" (an
// affirmative claim about an outcome nothing here actually confirmed), and the method name
// itself surfaces in the message so a real occurrence is diagnosable, not silently mislabeled.

test("renderApplyReport: an unrecognized method never says 'freed' -- apply", () => {
  renderApplyReport(container(), baseReport({ method: "future_unknown_method", apply: true }));
  const text = container().textContent;
  assert.equal(text.includes("freed"), false, "unrecognized method must never claim bytes were freed");
  assert.ok(text.includes('unrecognized method "future_unknown_method"'), `got: ${text}`);
});

test("renderApplyReport: an unrecognized method never says 'freed' -- dry-run", () => {
  renderApplyReport(container(), baseReport({ method: "future_unknown_method", apply: false }));
  const text = container().textContent;
  assert.equal(text.includes("freed"), false, "unrecognized method must never claim bytes were freed");
  assert.ok(text.includes('unrecognized method "future_unknown_method"'), `got: ${text}`);
});

// B7 teeth: the phrase is driven by the report's own bytes_freed/bytes_moved fields, not by
// report.method. A recycle_bin report that (wrongly) carried its bytes in bytes_freed would be
// worded "permanently freed" here, so a backend regression that counts a move as freed surfaces.
test("renderApplyReport: mixed batch words moved and freed bytes separately", () => {
  renderApplyReport(
    container(),
    baseReport({
      method: "vault",
      apply: true,
      bytes_freed: 1024,
      bytes_freed_human: "1.0 KB",
      bytes_moved: 2048,
      bytes_moved_human: "2.0 KB",
    })
  );
  const text = container().textContent;
  assert.ok(text.includes("2.0 KB (2,048 bytes) moved to the Reclaim vault"), `got: ${text}`);
  assert.ok(text.includes("1.0 KB (1,024 bytes) permanently freed."), `got: ${text}`);
});

test("renderApplyReport: category breakdown labels moved bytes as not yet freed", () => {
  renderApplyReport(
    container(),
    baseReport({
      method: "recycle_bin",
      category_breakdown: [
        {
          category_label: "Browser cache",
          count: 3,
          bytes_freed: 0,
          bytes_freed_human: "0 B",
          bytes_moved: 2048,
          bytes_moved_human: "2.0 KB",
        },
      ],
    })
  );
  const li = container().querySelector("li").textContent;
  assert.ok(li.includes("2.0 KB moved (not yet freed)"), `got: ${li}`);
  assert.equal(li.includes("0 B freed"), false, `got: ${li}`);
});
