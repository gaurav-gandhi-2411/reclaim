// DOM-level tests for the "Move to another drive" page (relocate; ADR-0044). Folder paths and
// reasons come from the disk (untrusted text), so they must render inert, and Confirm must send
// exactly the plan the dialog showed (source, target and its confirm token) and nothing else.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<div id="relocate-state"></div><div id="relocate-result"></div>' +
    '<div id="relocate-content"><p id="relocate-drive"></p><div id="relocate-candidates"></div>' +
    '<section id="relocate-moves-section" hidden><div id="relocate-moves"></div></section></div>' +
    '<div id="relocate-dialog" hidden><code id="relocate-dialog-source"></code>' +
    '<code id="relocate-dialog-target"></code><span id="relocate-dialog-size"></span>' +
    '<p id="relocate-dialog-kept"></p><ul id="relocate-dialog-reasons"></ul>' +
    '<button id="relocate-dialog-cancel"></button><button id="relocate-dialog-confirm"></button></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const calls = [];
globalThis.fetch = async (path, options = {}) => {
  calls.push({ path, options });
  const body = path === "/api/relocate/status" ? { status: "done", message: "moved", kept: [] } : { candidates: [], moves: [] };
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const { renderRelocateCandidates, renderRelocateMoves, openRelocateDialog, confirmRelocate } =
  await import("../../src/reclaim/api/static/app.js");

const hostile = 'C:\\Users\\x\\<img src=x onerror="globalThis.pwned=1">';

test("candidate paths and reasons render as inert text", () => {
  renderRelocateCandidates([
    { path: hostile, verdict: "movable", bytes: 2048, files: 3, reasons: [], link_target: null },
    { path: "C:\\b", verdict: "must_stay", bytes: 0, files: 0, reasons: [hostile], link_target: null },
  ]);
  const list = document.getElementById("relocate-candidates");
  assert.equal(list.querySelector("img"), null);
  assert.ok(list.textContent.includes("<img"));
  assert.equal(globalThis.pwned, undefined);
  const cards = list.querySelectorAll(".rc-relocate-card");
  assert.equal(cards.length, 2);
  assert.ok(cards[0].querySelector("button"), "a movable folder offers the check");
  assert.equal(cards[1].querySelector("button"), null, "a must-stay folder offers nothing");
});

test("a refused plan cannot be confirmed", () => {
  openRelocateDialog({
    source: hostile,
    target: "D:\\t",
    moved: "C:\\m",
    ok: false,
    reasons: ["it is in use"],
    files: 1,
    bytes: 1,
    target_free: 10,
    confirm_token: null,
  });
  assert.equal(document.getElementById("relocate-dialog-confirm").disabled, true);
  assert.equal(document.getElementById("relocate-dialog").querySelector("img"), null);
});

test("confirm sends exactly the plan the dialog showed", async () => {
  calls.length = 0;
  openRelocateDialog({
    source: "C:\\src",
    target: "D:\\reclaim-relocated\\C\\src",
    moved: "C:\\src.reclaim-moved",
    ok: true,
    reasons: [],
    files: 2,
    bytes: 4096,
    target_free: 10 ** 12,
    confirm_token: "tok123",
  });
  await confirmRelocate();
  const move = calls.find((c) => c.path === "/api/relocate/move");
  assert.ok(move, "the move was requested");
  assert.equal(move.options.method, "POST");
  assert.deepEqual(JSON.parse(move.options.body), {
    source: "C:\\src",
    target: "D:\\reclaim-relocated\\C\\src",
    confirm_token: "tok123",
  });
  assert.equal(document.getElementById("relocate-dialog").hidden, true);
});

test("moves list offers undo for done moves only", () => {
  renderRelocateMoves([
    { id: "a1", source: "C:\\a", target: "D:\\a", moved: "C:\\a.m", stage: "done", bytes: 1, delete_old_allowed_at: 0 },
    { id: "b2", source: "C:\\b", target: "D:\\b", moved: "C:\\b.m", stage: "rolled_back", bytes: 1, delete_old_allowed_at: null },
  ]);
  const cards = document.querySelectorAll("#relocate-moves .rc-relocate-card");
  assert.equal(cards[0].querySelectorAll("button").length, 2);
  assert.equal(cards[1].querySelectorAll("button").length, 0);
});
