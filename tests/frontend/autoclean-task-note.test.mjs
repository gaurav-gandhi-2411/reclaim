// The weekly auto-clean note renders ISO-8601 times from the API through the viewer's locale
// and only ever as text (the caller assigns textContent).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { describeAutoCleanTask } = await import("../../src/reclaim/api/static/app.js");

const base = {
  enabled: true,
  task_registered: true,
  task_name: "Reclaim Weekly Auto-Clean (x)",
  task_state: "Ready",
  last_run_time: null,
  last_result: 267011,
  next_run_time: "2026-10-04T10:00:00.0000000+05:30",
};

test("ISO next-run time is shown as a locale-formatted parsed Date", () => {
  const text = describeAutoCleanTask(base);
  const expected = new Date(base.next_run_time).toLocaleString();
  assert.ok(text.includes(`next run ${expected}`), text);
  assert.ok(text.includes("has not run yet"), text);
  assert.ok(!text.includes("T10:00:00"), "raw ISO must not leak into the UI");
});

test("a non-date string degrades to the raw text instead of 'Invalid Date'", () => {
  const text = describeAutoCleanTask({ ...base, next_run_time: "soon" });
  assert.ok(text.includes("next run soon"), text);
});
