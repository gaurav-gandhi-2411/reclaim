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
