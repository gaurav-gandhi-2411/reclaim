// Regression tests for SIMPLE mode's live-scan-status and results rendering
// (src/reclaim/api/static/app.js -- feat/simple-advanced-mode). Mirrors xss.test.mjs's harness
// pattern (a real JSDOM + the production app.js module, not a reimplementation): SIMPLE mode's
// scan-status polling surfaces `current_drive`, a raw OS drive path, so it gets the exact same
// "attacker-controlled-looking input renders as inert text, never markup" regression coverage
// this codebase already holds itself to for renderClusterTable/renderAISuggestionCard.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  '<!doctype html><html><body><div id="simple-view-content"></div></body></html>'
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const {
  formatEtaSeconds,
  renderSimpleScanning,
  renderSimpleGroups,
  renderSimpleEmpty,
  buildQuickCleanGroupCard,
  renderSimpleIdle,
  renderSimpleOneClickResult,
  renderSimpleOneClickProgress,
} = await import("../../src/reclaim/api/static/app.js");

function container() {
  return document.getElementById("simple-view-content");
}

// --- formatEtaSeconds -----------------------------------------------------------------------

test("formatEtaSeconds: null/undefined never renders raw 'null' -- shows a checking message", () => {
  assert.equal(formatEtaSeconds(null), "Checking…");
  assert.equal(formatEtaSeconds(undefined), "Checking…");
});

test("formatEtaSeconds: small values read as 'almost done', not a jittery few-second countdown", () => {
  assert.equal(formatEtaSeconds(3), "Almost done");
  assert.equal(formatEtaSeconds(0), "Almost done");
});

test("formatEtaSeconds: sub-minute values", () => {
  assert.equal(formatEtaSeconds(45), "Less than a minute remaining");
});

test("formatEtaSeconds: minute-scale values pluralize correctly", () => {
  assert.equal(formatEtaSeconds(65), "About 1 minute remaining");
  assert.equal(formatEtaSeconds(150), "About 3 minutes remaining");
});

// --- renderSimpleScanning --------------------------------------------------------------------

test("renderSimpleScanning: an attacker-controlled-looking current_drive renders as inert text", () => {
  const payload = '<img src=x onerror="window.__simpleXssFired = true">';
  renderSimpleScanning({
    phase: "scanning",
    entries_processed: 100,
    entries_estimated_total: 400,
    eta_seconds: 30,
    current_drive: payload,
    drives_total: 2,
    drives_done: 0,
  });

  const el = container();
  assert.equal(el.querySelectorAll("img").length, 0, "payload must not parse into an <img>");
  assert.equal(globalThis.window.__simpleXssFired, undefined, "onerror must never execute");
  assert.ok(el.textContent.includes(payload), "the raw payload must survive as literal text");
});

test("renderSimpleScanning: eta_seconds=null during 'estimating' never shows literal 'null'", () => {
  renderSimpleScanning({
    phase: "estimating",
    entries_processed: 12400,
    entries_estimated_total: null,
    eta_seconds: null,
    current_drive: "C:/",
    drives_total: 1,
    drives_done: 0,
  });
  const el = container();
  assert.equal(el.textContent.includes("null"), false);
  assert.ok(el.textContent.includes("12,400"), "counted-so-far must be shown, formatted");
});

test("renderSimpleScanning: phase=null on the very first tick renders the estimating copy, not a crash", () => {
  renderSimpleScanning({
    phase: null,
    entries_processed: 0,
    entries_estimated_total: null,
    eta_seconds: null,
    current_drive: null,
    drives_total: 1,
    drives_done: 0,
  });
  const el = container();
  assert.ok(el.textContent.includes("Checking your computer"));
});

test("renderSimpleScanning: single-drive scan never shows a 'Drive N of M' line", () => {
  renderSimpleScanning({
    phase: "scanning",
    entries_processed: 10,
    entries_estimated_total: 100,
    eta_seconds: 20,
    current_drive: "C:/",
    drives_total: 1,
    drives_done: 0,
  });
  const el = container();
  assert.equal(el.textContent.includes("Drive"), false);
});

test("renderSimpleScanning: multi-drive scan shows a plain-language 'Drive N of M' line", () => {
  renderSimpleScanning({
    phase: "scanning",
    entries_processed: 10,
    entries_estimated_total: 0,
    eta_seconds: null,
    current_drive: "D:/",
    drives_total: 3,
    drives_done: 1,
  });
  const el = container();
  assert.ok(el.textContent.includes("Drive 2 of 3"));
});

// --- buildQuickCleanGroupCard / renderSimpleGroups --------------------------------------------

test("buildQuickCleanGroupCard renders plain_label/safety_reason/total_bytes_human", () => {
  const card = buildQuickCleanGroupCard({
    category_group: "package_caches",
    plain_label: "Package manager caches",
    safety_reason: "Safe — re-downloaded automatically when needed.",
    file_count: 42,
    total_bytes: 1024,
    total_bytes_human: "1.0 KB",
  });
  assert.ok(card.innerHTML.includes("Package manager caches"));
  assert.ok(card.innerHTML.includes("1.0 KB"));
});

test("renderSimpleEmpty shows a friendly empty state with a way back to idle", () => {
  renderSimpleEmpty();
  const el = container();
  const panel = el.querySelector('.rc-state-panel[data-kind="empty"]');
  assert.ok(panel, "must reuse the existing .rc-state-panel empty pattern");
  const btn = panel.querySelector("button");
  assert.ok(btn, "must offer a way back to the idle screen");
});

// --- renderSimpleIdle: P0 fix (2026-08-22 real-disk finding) -------------------------------
//
// "Clean My Computer" must default to a user-scoped scan (POST /api/scan/my-files) -- a real
// smoke-test scan found the previous "whole computer"/full-drive default reached other local
// accounts' profile directories on a real multi-project dev machine. These assertions pin the
// user-facing copy so a future edit can't silently reintroduce "whole computer" language for the
// default action, and prove the whole-drive opt-in is present but visually/textually distinct.

test("renderSimpleIdle: intro copy no longer claims to scan the whole computer by default", () => {
  renderSimpleIdle();
  const el = container();
  assert.ok(el.textContent.includes("caches"));
  assert.equal(
    el.textContent.includes("Scans your whole computer"),
    false,
    "the default action's copy must not claim whole-computer scope"
  );
});

test("renderSimpleIdle: exactly one primary 'Clean My Computer' button", () => {
  renderSimpleIdle();
  const el = container();
  const primaryButtons = [...el.querySelectorAll("button")].filter(
    (b) => b.textContent === "Clean My Computer"
  );
  assert.equal(primaryButtons.length, 1);
});

test("renderSimpleIdle: whole-drive scan is offered only as a distinct, secondary, explicitly-labeled action", () => {
  renderSimpleIdle();
  const el = container();
  const advancedBtn = el.querySelector(".rc-simple-advanced-btn");
  assert.ok(advancedBtn, "a secondary whole-drive-scan control must be present");
  assert.ok(advancedBtn.textContent.toLowerCase().includes("whole drive"));
  assert.ok(advancedBtn.textContent.toLowerCase().includes("advanced"));
  assert.notEqual(
    advancedBtn.className,
    el.querySelector(".rc-simple-primary-btn").className,
    "the whole-drive opt-in must not share the primary action's visual weight"
  );
});

test("renderSimpleGroups renders exactly one 'Clean now' button and every group's plain_label", () => {
  renderSimpleGroups({
    has_scan: true,
    groups: [
      {
        category_group: "temp_and_browser_caches",
        plain_label: "Temporary & browser cache files",
        safety_reason: "Safe — recreated automatically as you browse.",
        file_count: 10,
        total_bytes: 2048,
        total_bytes_human: "2.0 KB",
        paths: ["C:/Temp/a.tmp"],
      },
    ],
    total_bytes: 2048,
    total_bytes_human: "2.0 KB",
    total_file_count: 10,
  });
  const el = container();
  assert.ok(el.textContent.includes("Temporary & browser cache files"));
  const buttons = [...el.querySelectorAll("button")].filter((b) => b.textContent === "Clean now");
  assert.equal(buttons.length, 1, "exactly one 'Clean now' button");
});

// --- renderSimpleOneClickResult (ADR-0034) ----------------------------------------------------

function oneClickReport(overrides = {}) {
  return {
    run_id: "abc123",
    apply: true,
    items: [
      {
        kind: "native_command",
        key: "uv",
        label: "uv package cache",
        status: "cleaned",
        bytes_removed: 3221225472,
        bytes_removed_human: "3.0 GB",
        files_removed: 0,
        files_skipped_in_use: 0,
        detail: "`uv cache prune` exit 0",
        skipped_paths: [],
      },
      {
        kind: "browser_cache",
        key: "chrome",
        label: "Google Chrome cache",
        status: "skipped_browser_running",
        bytes_removed: 0,
        bytes_removed_human: "0 B",
        files_removed: 0,
        files_skipped_in_use: 0,
        detail: "Google Chrome is running; its cache is left alone",
        skipped_paths: [],
      },
      {
        kind: "browser_cache",
        key: "firefox",
        label: "Mozilla Firefox cache",
        status: "skipped_not_present",
        bytes_removed: 0,
        bytes_removed_human: "0 B",
        files_removed: 0,
        files_skipped_in_use: 0,
        detail: "",
        skipped_paths: [],
      },
    ],
    bytes_removed: 3221225472,
    bytes_removed_human: "3.0 GB",
    files_skipped_in_use: 4,
    disk_free_before_bytes: 1000,
    disk_free_after_bytes: 3221226472,
    disk_free_delta_bytes: 3221225472,
    percent_used_after: 94.4,
    duration_seconds: 12.5,
    ...overrides,
  };
}

test("renderSimpleOneClickResult: leads with 'Freed X' and the new C: usage", () => {
  renderSimpleOneClickResult(oneClickReport());
  const text = container().textContent;
  assert.ok(text.includes("Freed 3.0 GB"));
  assert.ok(text.includes("C: is now 94% used"));
  assert.ok(text.includes("+3.0 GB"), "the measured free-space delta is shown beside the total");
});

test("renderSimpleOneClickResult: skipped items are shown, not hidden; absent ones are dropped", () => {
  renderSimpleOneClickResult(oneClickReport());
  const text = container().textContent;
  assert.ok(text.includes("Google Chrome cache: Skipped — browser is open"));
  assert.equal(text.includes("Mozilla Firefox"), false, "a tool/browser that isn't here is noise");
  assert.ok(text.includes("4 file(s) were in use and left alone"));
});

test("renderSimpleOneClickResult: offers 'scan for more' and 'Done'", () => {
  renderSimpleOneClickResult(oneClickReport());
  const labels = [...container().querySelectorAll("button")].map((b) => b.textContent);
  assert.ok(labels.includes("Scan my files for more to review"));
  assert.ok(labels.includes("Done"));
});

test("renderSimpleOneClickResult: disk-derived text renders as inert text, never markup", () => {
  const payload = '<img src=x onerror="window.__oneClickXss = true">';
  renderSimpleOneClickResult(
    oneClickReport({
      items: [
        {
          ...oneClickReport().items[0],
          label: payload,
          detail: payload,
        },
      ],
    })
  );
  assert.equal(container().querySelectorAll("img").length, 0);
  assert.equal(globalThis.window.__oneClickXss, undefined);
  assert.ok(container().textContent.includes(payload));
});

test("renderSimpleOneClickResult: null measurements never render literal 'null'", () => {
  renderSimpleOneClickResult(
    oneClickReport({ percent_used_after: null, disk_free_delta_bytes: null })
  );
  assert.equal(container().textContent.includes("null"), false);
});

// --- renderSimpleOneClickProgress (live view while the background clean runs) ------------------

test("renderSimpleOneClickProgress: shows finished items live and the waiting uv item", () => {
  const first = oneClickReport().items[0];
  renderSimpleOneClickProgress({
    status: "running",
    items: [{ ...first, label: "Old temp files (C:/t)", key: "temp0" }],
    current_item: "uv package cache: waiting for its cache lock if another process is using it… 12 min",
    elapsed_seconds: 745,
  });
  const text = container().textContent;
  assert.ok(text.includes("waiting for its cache lock"));
  assert.ok(text.includes("12 min"));
  assert.ok(text.includes("Old temp files (C:/t): Cleaned"), "completed items are listed live");
  assert.ok(text.includes("Running for 12 min 25 s"));
  assert.equal(container().querySelector('[data-kind="loading"]') !== null, true);
});

test("renderSimpleOneClickProgress: no items and no current item never renders 'null'", () => {
  renderSimpleOneClickProgress({
    status: "running",
    items: [],
    current_item: null,
    elapsed_seconds: 0,
  });
  const text = container().textContent;
  assert.equal(text.includes("null"), false);
  assert.ok(text.includes("Finishing up"));
  assert.equal(container().querySelectorAll("li").length, 0);
});

test("renderSimpleOneClickProgress: server-supplied text renders as inert text, never markup", () => {
  const payload = '<img src=x onerror="window.__oneClickProgressXss = true">';
  renderSimpleOneClickProgress({
    status: "running",
    items: [{ ...oneClickReport().items[0], label: payload, detail: payload }],
    current_item: payload,
    elapsed_seconds: 5,
  });
  assert.equal(container().querySelectorAll("img").length, 0);
  assert.equal(globalThis.window.__oneClickProgressXss, undefined);
  assert.ok(container().textContent.includes(payload));
});
