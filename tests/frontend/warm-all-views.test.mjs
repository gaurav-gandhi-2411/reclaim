// fix/warm-check-all-views: EVERY view that reads the candidate cache (Overview, Quick Clean,
// treemap, review queue, SIMPLE-mode results) runs the same warm-status check through
// `readCandidateCache`. A "stale" status (mode switch / category toggle / new scan / scope
// change) must trigger exactly one re-warm per detection, show the cause as inert text while
// warming, and never read -- or render -- the cache before it is warm again.
//
// Harness: a real JSDOM + the real app.js, with a stateful fake of the server. Like the real
// server, a candidate-cache READ against a cold/stale cache answers 409 `candidates_not_warm`;
// `coldReads` counts such attempts, so a view that skips the check shows `coldReads > 0`.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const view = (name) =>
  `<div id="${name}-state"></div><div id="${name}-content" hidden></div>`;
const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<button id="mode-badge" data-mode="power"></button>' +
    '<button class="rc-tab" data-view="overview" aria-selected="true"></button>' +
    view("overview") +
    '<div id="stat-row"></div>' +
    view("quick-clean") +
    view("treemap") +
    '<span id="treemap-root-label"></span><svg id="treemap-svg"></svg>' +
    '<div id="treemap-tooltip"></div><div id="treemap-legend"></div>' +
    view("review") +
    '<select id="review-tier-filter"><option value="both" selected>both</option></select>' +
    '<select id="review-category-filter"><option value="" selected>all</option></select>' +
    view("duplicate-review") +
    '<div id="simple-view-content"></div>' +
    '<div id="quick-clean-dialog" hidden><ul id="quick-clean-dialog-groups"></ul>' +
    '<span id="quick-clean-dialog-total"></span><p id="quick-clean-dialog-warning"></p></div>' +
    '<div id="power-mode-dialog"><input id="power-mode-input"><p id="power-mode-error"></p></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const READS = new Set([
  "/api/summary",
  "/api/treemap",
  "/api/candidates?tier=both",
  "/api/clean/one-click-summary",
]);

function makeServer() {
  const server = {
    key: "scan1|power|cfg1|home",
    cachedKey: "scan1|power|cfg1|home",
    reason: null,
    computing: false,
    pollsLeft: 0,
    calls: [],
    coldReads: 0,
    served: [], // key the cache was warm under for each successful read
    racy409Once: false, // status says ready but the first read still 409s (stale in the gap)
  };
  const reply = (body, status = 200) => ({
    ok: status < 400,
    status,
    statusText: "",
    headers: { get: () => "application/json" },
    json: async () => body,
  });
  const warmStatus = () => {
    if (server.computing) return { status: "computing", elapsed_seconds: 0, stale_reason: null };
    if (server.cachedKey === server.key) return { status: "ready", stale_reason: null };
    return { status: "stale", stale_reason: server.reason };
  };
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    server.calls.push(`${method} ${path}`);
    if (path === "/api/candidates/warm-status") {
      if (server.computing && server.pollsLeft-- <= 0) {
        server.computing = false;
        server.cachedKey = server.key;
      }
      return reply(warmStatus());
    }
    if (path === "/api/candidates/warm" && method === "POST") {
      server.computing = true;
      server.pollsLeft = 0;
      return reply({ status: "computing", elapsed_seconds: 0 }, 202);
    }
    if (path === "/api/duplicate-clusters/review") return reply({ has_scan: false, clusters: [] });
    if (READS.has(path)) {
      if (server.racy409Once) {
        server.racy409Once = false;
        server.cachedKey = null;
        server.computing = true; // the 409 route starts the courtesy warm-up
        server.pollsLeft = 0;
        server.coldReads += 1;
        return reply({ detail: "not warm", code: "candidates_not_warm", stale_reason: "mode" }, 409);
      }
      if (server.cachedKey !== server.key) {
        server.coldReads += 1;
        return reply(
          { detail: "not warm", code: "candidates_not_warm", stale_reason: server.reason },
          409
        );
      }
      server.served.push(server.key);
      if (path === "/api/treemap") {
        return reply({ has_scan: true, root: `ROOT[${server.key}]`, nodes: [], total_bytes_human: "0 B" });
      }
      if (path === "/api/clean/one-click-summary") {
        return reply({ has_scan: true, groups: [], total_file_count: 0, total_bytes_human: "0 B" });
      }
      if (path === "/api/candidates?tier=both") {
        return reply({ has_scan: true, candidates: [], count: 0 });
      }
      return reply({ has_scan: false });
    }
    throw new Error(`unexpected request ${method} ${path}`);
  };
  return server;
}

const app = await import("../../src/reclaim/api/static/app.js");

const warmPosts = (server) => server.calls.filter((c) => c === "POST /api/candidates/warm");
const stateText = (id) => document.getElementById(id).textContent;

// view name -> [loader, element id its loading/stale text is shown in, element id of final state]
const VIEWS = {
  overview: [app.loadOverview, "overview-state", "overview-state"],
  "quick clean": [app.loadQuickClean, "quick-clean-state", "quick-clean-state"],
  treemap: [app.loadTreemapView, "treemap-state", "treemap-state"],
  "review queue": [app.loadReviewQueue, "review-state", "review-state"],
  "simple results": [app.loadSimpleResults, "simple-view-content", "simple-view-content"],
};

for (const [name, [loader, loadingId]] of Object.entries(VIEWS)) {
  test(`${name}: ready status -> no re-warm`, async () => {
    const server = makeServer();
    await loader();
    assert.equal(warmPosts(server).length, 0);
    assert.equal(server.coldReads, 0);
  });

  for (const [cause, reason, key] of [
    ["mode switch", "mode", "scan1|safe|cfg2|home"],
    ["category toggle", "config", "scan1|power|cfg2|home"],
    ["new scan", "scan", "scan2|power|cfg1|home"],
    ["scope change", "scope", "scan1|power|cfg1|elsewhere"],
  ]) {
    test(`${name}: stale (${cause}) -> exactly one re-warm, no cold read, cause shown inertly`, async () => {
      const server = makeServer();
      server.key = key;
      server.reason = reason;
      const el = document.getElementById(loadingId);
      let seen = "";
      const realFetch = globalThis.fetch;
      globalThis.fetch = async (path, options) => {
        seen += el.textContent;
        return realFetch(path, options);
      };
      await loader();
      assert.equal(warmPosts(server).length, 1, "exactly one POST /api/candidates/warm");
      assert.equal(server.coldReads, 0, "the cache was never read while stale");
      assert.deepEqual([...new Set(server.served)], [key], "only post-change data was served");
      assert.match(
        seen,
        {
          mode: /You switched modes/,
          config: /Your settings changed/,
          scan: /A new scan finished/,
          scope: /The scan scope changed/,
        }[reason]
      );
      assert.equal(el.querySelectorAll("script, img").length, 0);
    });
  }

  test(`${name}: stale between the check and the read -> 409 handled with ONE bounded retry`, async () => {
    const server = makeServer();
    server.racy409Once = true;
    await loader();
    assert.equal(warmPosts(server).length, 0, "the 409 route already started the warm-up");
    assert.equal(server.coldReads, 1);
    assert.equal(server.served.length >= 1, true, "the retry after the warm-up was served");
    assert.doesNotMatch(stateText(loadingId), /Could not|check what/i);
  });
}

test("a persistent 409 surfaces as the view's error state, never loops", async () => {
  const server = makeServer();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, options) => {
    if (path === "/api/treemap") {
      server.calls.push(`GET ${path}`);
      return {
        ok: false,
        status: 409,
        statusText: "Conflict",
        headers: { get: () => "application/json" },
        json: async () => ({ detail: "not warm", code: "candidates_not_warm", stale_reason: "cold" }),
      };
    }
    return realFetch(path, options);
  };
  await app.loadTreemapView();
  assert.equal(server.calls.filter((c) => c === "GET /api/treemap").length, 2, "one retry only");
  assert.match(stateText("treemap-state"), /Could not load treemap/);
});

test("cross-screen: after a mode switch and a category toggle every cache view is post-switch", async () => {
  const server = makeServer();
  const allLoads = async () => {
    for (const [loader] of Object.values(VIEWS)) await loader();
  };
  await allLoads();
  assert.equal(warmPosts(server).length, 0);

  // mode switch
  server.key = "scan1|safe|cfg2|home";
  server.reason = "mode";
  await allLoads();
  assert.equal(warmPosts(server).length, 1, "one re-warm for the mode switch, not one per screen");
  assert.match(stateText("treemap-state") + document.getElementById("treemap-root-label").textContent,
    /safe/);

  // category toggle
  server.key = "scan1|safe|cfg3|home";
  server.reason = "config";
  await allLoads();
  assert.equal(warmPosts(server).length, 2, "one more re-warm for the category toggle");

  assert.equal(server.coldReads, 0, "no screen ever read the cache while it was stale");
  for (const k of server.served) assert.ok(["scan1|power|cfg1|home", "scan1|safe|cfg2|home", "scan1|safe|cfg3|home"].includes(k));
  assert.equal(server.served.at(-1), "scan1|safe|cfg3|home");
  assert.match(stateText("treemap-state"), /cfg3/);
  for (const id of ["overview-state", "quick-clean-state", "treemap-state", "review-state"]) {
    assert.doesNotMatch(stateText(id), /Could not/, id);
  }
});

test("mode switch refreshes the ACTIVE view through the shared check (Overview tab)", async () => {
  const server = makeServer();
  server.key = "scan1|safe|cfg2|home";
  server.reason = "mode";
  document.documentElement.setAttribute("data-mode", "advanced"); // ADVANCED: tab views refresh
  await app.refreshActiveView();
  document.documentElement.removeAttribute("data-mode");
  assert.equal(warmPosts(server).length, 1);
  assert.equal(server.coldReads, 0);
  assert.ok(server.calls.includes("GET /api/summary"));
});

test("mode switch refreshes SIMPLE mode's results screen when that is what is showing", async () => {
  const server = makeServer();
  await app.loadSimpleResults(); // sets the "results shown" flag
  server.key = "scan1|safe|cfg2|home";
  server.reason = "mode";
  await app.refreshActiveView();
  assert.equal(warmPosts(server).length, 1);
  assert.equal(server.coldReads, 0);
  assert.ok(server.calls.filter((c) => c === "GET /api/clean/one-click-summary").length >= 2);
});

test("Quick Clean: confirm dialog is not opened on a stale list; the list reloads instead", async () => {
  const server = makeServer();
  await app.loadQuickClean();
  server.key = "scan1|safe|cfg2|home";
  server.reason = "mode";
  await app.openQuickCleanDialogIfFresh("overview");
  assert.equal(document.getElementById("quick-clean-dialog").hidden, true, "no stale confirm");
  assert.equal(warmPosts(server).length, 1);
  assert.equal(server.coldReads, 0);
  // once fresh, the same call opens the dialog
  await app.openQuickCleanDialogIfFresh("overview");
  assert.equal(document.getElementById("quick-clean-dialog").hidden, false);
});
