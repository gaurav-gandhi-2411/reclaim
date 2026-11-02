// "Disk growth alert" Settings panel (ADR-0046 addendum 2026-11-02): an opt-in toggle with the
// same load / save / rollback behaviour as the low-disk-space toggle. Real JSDOM + the real app.js
// module, fetch faked on globalThis.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<input id="growth-alert-enabled-checkbox" type="checkbox" disabled />' +
    '<span id="growth-alert-enabled-text">Off</span>' +
    '<p id="growth-alert-collection-note"></p>' +
    '<p id="growth-alert-status" class="rc-settings-card-status"></p>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { loadGrowthAlertSettings } = await import("../../src/reclaim/api/static/app.js");

const reply = (body, status = 200) => ({
  ok: status < 400,
  status,
  statusText: status < 400 ? "" : "Server Error",
  headers: { get: () => "application/json" },
  json: async () => body,
});

const box = () => document.getElementById("growth-alert-enabled-checkbox");
const text = () => document.getElementById("growth-alert-enabled-text").textContent;
const status = () => document.getElementById("growth-alert-status");
const note = () => document.getElementById("growth-alert-collection-note").textContent;
const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

test("default off: checkbox unchecked, enabled for input, no collection warning", async () => {
  globalThis.fetch = async () => reply({ enabled: false, collection_enabled: true });
  await loadGrowthAlertSettings();
  assert.equal(box().checked, false);
  assert.equal(box().disabled, false);
  assert.equal(text(), "Off");
  assert.equal(note(), "");
});

test("collection off: the panel says the alert cannot fire", async () => {
  globalThis.fetch = async () => reply({ enabled: true, collection_enabled: false });
  await loadGrowthAlertSettings();
  assert.equal(box().checked, true);
  assert.equal(text(), "On");
  assert.match(note(), /cannot fire/);
});

test("toggling on POSTs {enabled:true} to the growth-alert endpoint", async () => {
  const calls = [];
  globalThis.fetch = async (path, options = {}) => {
    calls.push([path, options.method ?? "GET", options.body]);
    return reply({ enabled: options.method === "POST", collection_enabled: true });
  };
  await loadGrowthAlertSettings();
  box().checked = true;
  box().dispatchEvent(new dom.window.Event("change"));
  await settle();
  assert.deepEqual(calls.at(-1), [
    "/api/settings/growth-alert",
    "POST",
    JSON.stringify({ enabled: true }),
  ]);
  assert.equal(text(), "On");
  assert.equal(box().disabled, false);
  assert.equal(status().textContent, "");
});

test("a failed save rolls the checkbox back and shows an error", async () => {
  globalThis.fetch = async (path, options = {}) =>
    options.method === "POST"
      ? reply({ detail: "disk full" }, 500)
      : reply({ enabled: false, collection_enabled: true });
  await loadGrowthAlertSettings(); // the change listener is attached once per element
  box().checked = true;
  box().dispatchEvent(new dom.window.Event("change"));
  await settle();
  assert.equal(box().checked, false);
  assert.equal(text(), "Off");
  assert.match(status().textContent, /Could not save: disk full/);
  assert.ok(status().classList.contains("rc-form-error"));
  assert.equal(box().disabled, false);
});

test("a failed load shows an error and leaves the toggle disabled", async () => {
  box().disabled = true;
  globalThis.fetch = async () => reply({ detail: "nope" }, 500);
  await loadGrowthAlertSettings();
  assert.match(status().textContent, /Could not load: nope/);
  assert.equal(box().disabled, true);
});
