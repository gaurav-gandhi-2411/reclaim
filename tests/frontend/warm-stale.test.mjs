// Regression tests for the candidate-cache "stale" re-warm flow (perf/path-scoped-apply-cache):
// the server recomputes the cache key on every GET /api/candidates/warm-status and reports
// "stale" (+ stale_reason) when a mode switch / category toggle / new scan / scope change made a
// previously "ready" cache cold. The dashboard must re-start the warm job exactly once and never
// call /api/summary against a cold cache. Same harness as xss.test.mjs: a real JSDOM + the real
// app.js module, with a small stateful fake of the server's warm endpoints on globalThis.fetch.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<button id="mode-badge" data-mode="power"></button>' +
    '<button class="rc-tab" data-view="overview" aria-selected="true"></button>' +
    '<section class="rc-view" id="view-overview">' +
    '<div id="overview-state"></div><div id="overview-content" hidden></div></section>' +
    '<div id="power-mode-dialog"><input id="power-mode-input"><p id="power-mode-error"></p></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

/** Stateful fake of the server side: `key` is the live cache key, `cachedKey` what was warmed. */
function makeServer() {
  const server = {
    key: "scan1|power|cfg1|home",
    cachedKey: "scan1|power|cfg1|home",
    reason: null,
    computing: false,
    calls: [],
    summaryWhileCold: 0,
  };
  const reply = (body, status = 200) => ({
    ok: status < 400,
    status,
    statusText: "",
    headers: { get: () => "application/json" },
    json: async () => body,
  });
  const statusBody = () => {
    if (server.computing) return { status: "computing", elapsed_seconds: 0, stale_reason: null };
    if (server.cachedKey === server.key) return { status: "ready", stale_reason: null };
    return { status: "stale", stale_reason: server.reason };
  };
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    server.calls.push(`${method} ${path}`);
    if (path === "/api/candidates/warm-status") {
      const body = statusBody();
      if (server.computing && server.pollsLeft-- <= 0) {
        server.computing = false;
        server.cachedKey = server.key; // the background job finished
        return reply({ status: "ready", stale_reason: null });
      }
      return reply(body);
    }
    if (path === "/api/candidates/warm" && method === "POST") {
      server.computing = true;
      server.pollsLeft = 0;
      return reply({ status: "computing", elapsed_seconds: 0 }, 202);
    }
    if (path === "/api/mode/safe" && method === "POST") {
      server.key = "scan1|safe|cfg2|home";
      server.reason = "mode";
      return reply({ mode: "safe" });
    }
    if (path === "/api/mode/power" && method === "POST") {
      server.key = "scan1|power|cfg1|home";
      server.reason = "mode";
      return reply({ mode: "power" });
    }
    if (path === "/api/summary") {
      if (server.cachedKey !== server.key) server.summaryWhileCold += 1;
      return reply({ has_scan: false });
    }
    throw new Error(`unexpected request ${method} ${path}`);
  };
  return server;
}

const { ensureCandidatesWarm, switchToSafeMode, confirmPowerMode } = await import(
  "../../src/reclaim/api/static/app.js"
);

const warmPosts = (server) => server.calls.filter((c) => c === "POST /api/candidates/warm");

test("ready status: no re-warm is started", async () => {
  const server = makeServer();
  await ensureCandidatesWarm(document.getElementById("overview-state"));
  assert.equal(warmPosts(server).length, 0);
});

test("stale status (category toggle -> config) triggers exactly one re-warm, no loop", async () => {
  const server = makeServer();
  server.key = "scan1|power|cfg2|home";
  server.reason = "config";
  const stateEl = document.getElementById("overview-state");
  await ensureCandidatesWarm(stateEl);
  assert.equal(warmPosts(server).length, 1, "exactly one POST /api/candidates/warm");
  assert.equal(server.cachedKey, server.key, "ended on a warm cache");
  const statusReads = server.calls.filter((c) => c === "GET /api/candidates/warm-status").length;
  assert.ok(statusReads <= 3, `bounded polling, saw ${statusReads} status reads`);
});

test("stale loading message names the cause as inert text", async () => {
  const server = makeServer();
  server.key = "scan2|power|cfg1|home";
  server.reason = "scan";
  const stateEl = document.getElementById("overview-state");
  let seen = "";
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    seen += stateEl.textContent;
    return realFetch(path, options);
  };
  await ensureCandidatesWarm(stateEl);
  assert.match(seen, /A new scan finished/);
  assert.equal(stateEl.querySelectorAll("script, img").length, 0);
});

test("mode switch to SAFE re-warms before /api/summary; summary never hits a cold cache", async () => {
  const server = makeServer();
  await switchToSafeMode();
  assert.equal(document.getElementById("mode-badge").dataset.mode, "safe");
  assert.equal(warmPosts(server).length, 1, "exactly one re-warm after the mode switch");
  assert.equal(server.summaryWhileCold, 0, "summary was not requested against a cold cache");
  assert.ok(server.calls.includes("GET /api/summary"), "overview reloaded after the switch");
  const order = server.calls;
  assert.ok(
    order.indexOf("POST /api/candidates/warm") < order.indexOf("GET /api/summary"),
    "warm is requested before summary"
  );
});

test("mode switch to POWER (confirmed) also re-warms exactly once", async () => {
  const server = makeServer();
  server.key = "scan1|safe|cfg2|home";
  server.cachedKey = server.key; // currently warm under SAFE
  document.getElementById("power-mode-input").value = "phrase";
  await confirmPowerMode();
  assert.equal(warmPosts(server).length, 1);
  assert.equal(server.summaryWhileCold, 0);
  assert.equal(server.cachedKey, server.key);
});
