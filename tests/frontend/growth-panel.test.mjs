// "What changed" panel (ADR-0046): renders GET /api/growth read-only. Same harness as
// warm-stale.test.mjs: a real JSDOM + the real app.js module, fetch faked on globalThis.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<button class="rc-tab" data-view="overview" aria-selected="true"></button>' +
    '<button class="rc-tab" data-view="review" aria-selected="false"></button>' +
    '<section class="rc-view" id="view-overview" data-active="true">' +
    '<div id="overview-state"></div><div id="overview-content" hidden></div>' +
    '<section id="growth-panel">' +
    '<button data-window="1d" aria-pressed="true"></button>' +
    '<button data-window="7d" aria-pressed="false"></button>' +
    '<div id="growth-state"></div><div id="growth-content" hidden>' +
    '<ul id="growth-volumes"></ul><ol id="growth-rows"></ol><p id="growth-excluded"></p>' +
    "</div></section></section>" +
    '<section class="rc-view" id="view-review" data-active="false">' +
    '<div id="duplicate-review-state"></div><div id="duplicate-review-content" hidden></div>' +
    '<div id="review-state"></div><div id="review-content" hidden></div></section>' +
    '<select id="review-tier-filter"><option value="B" selected>B</option><option value="both">both</option></select>' +
    '<select id="review-category-filter"><option value="" selected>All</option>' +
    '<option value="dev_artifacts">Dev</option><option value="temp_and_browser_caches">Temp</option></select>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { loadGrowthPanel, initGrowthPanel, reviewFilterFromLink } = await import(
  "../../src/reclaim/api/static/app.js"
);

const GIB = 1024 ** 3;
const reply = (body, status = 200) => ({
  ok: status < 400,
  status,
  statusText: "",
  headers: { get: () => "application/json" },
  json: async () => body,
});

const BODY = {
  window: "1d",
  generated_at: "2026-11-01T00:00:00+00:00",
  status: "ok",
  volumes: [
    {
      volume: "C:\\",
      change_bytes: 20 * GIB,
      attributed_bytes: 18 * GIB,
      not_attributed_bytes: 2 * GIB,
      status: "ok",
    },
  ],
  rows: [
    {
      path: "C:\\t\\claude\\proj\\sess",
      label: "proj\\sess <b>x</b>",
      kind: "scratch",
      project: "proj",
      session: "sess",
      growth_bytes: 18 * GIB,
      partial: true,
      review_link: "/api/candidates?tier=both&category=temp_and_browser_caches",
    },
    {
      path: "C:\\Users\\g\\work",
      label: "work",
      kind: "toplevel",
      project: "",
      session: "",
      growth_bytes: 1 * GIB,
      partial: false,
      review_link: null,
    },
  ],
  excluded_aggregate: { label: "Excluded projects", bytes: 3 * GIB },
  anomalies: [],
};

const state = () => document.getElementById("growth-state");
const rows = () => [...document.querySelectorAll("#growth-rows li")];

test("loading state shows while the request is pending", async () => {
  let release;
  globalThis.fetch = () => new Promise((resolve) => (release = () => resolve(reply(BODY))));
  const pending = loadGrowthPanel();
  assert.equal(state().querySelector("[data-kind=loading]") !== null, true);
  assert.equal(document.getElementById("growth-content").hidden, true);
  release();
  await pending;
});

test("renders volume line, rows, partial marker, excluded aggregate; read-only", async () => {
  globalThis.fetch = async () => reply(BODY);
  await loadGrowthPanel();
  assert.equal(document.getElementById("growth-content").hidden, false);
  assert.equal(state().textContent, "");
  assert.match(document.getElementById("growth-volumes").textContent, /C:\\ grew 20\.0 GB in the last day/);
  assert.match(document.getElementById("growth-volumes").textContent, /\+2\.0 GB not attributed/);
  assert.equal(rows().length, 2);
  assert.match(rows()[0].textContent, /\+18\.0 GB/);
  assert.ok(rows()[0].querySelector(".rc-growth-partial"), "partial marker on the cut-off row");
  assert.equal(rows()[1].querySelector(".rc-growth-partial"), null);
  assert.equal(document.getElementById("growth-excluded").textContent.includes("Excluded projects: +3.0 GB"), true);
  // Server text is inert, and nothing in the panel can delete or apply.
  assert.equal(rows()[0].querySelectorAll("b, script, img").length, 0);
  assert.equal(document.querySelectorAll("#growth-panel button:not([data-window])").length, 0);
});

test("only the row with a review_link gets a link, and it opens the Review tab pre-filtered", async () => {
  const seen = [];
  globalThis.fetch = async (path) => {
    seen.push(path);
    if (path === "/api/candidates/warm-status") return reply({ status: "ready" });
    if (path.startsWith("/api/candidates")) return reply({ has_scan: false, candidates: [] });
    return reply(BODY);
  };
  await loadGrowthPanel();
  assert.equal(rows()[0].querySelectorAll("a").length, 1);
  assert.equal(rows()[1].querySelectorAll("a").length, 0);
  rows()[0].querySelector("a").click();
  await new Promise((resolve) => setTimeout(resolve, 20)); // let the Review loader settle
  assert.ok(
    seen.includes("/api/candidates?tier=both&category=temp_and_browser_caches"),
    `the Review tab loaded with the row's filter, saw ${seen.join(" ")}`
  );
  assert.equal(document.getElementById("review-category-filter").value, "temp_and_browser_caches");
  assert.equal(document.getElementById("review-tier-filter").value, "both");
  assert.equal(document.getElementById("view-review").dataset.active, "true");
});

test("a link whose category the Review tab does not offer is not shown", () => {
  assert.equal(reviewFilterFromLink("/api/candidates?tier=both&category=nope"), null);
  assert.equal(reviewFilterFromLink(null), null);
  assert.deepEqual(reviewFilterFromLink("/api/candidates?tier=both&category=dev_artifacts"), {
    tier: "both",
    category: "dev_artifacts",
  });
});

test("insufficient data shows the empty state with the snapshot cadence", async () => {
  globalThis.fetch = async () => reply({ ...BODY, status: "insufficient data", rows: [] });
  await loadGrowthPanel();
  assert.equal(document.getElementById("growth-content").hidden, true);
  assert.match(state().textContent, /not enough snapshots yet — Reclaim takes one every 4 hours/i);
});

test("a failed request shows the error state with a retry button", async () => {
  globalThis.fetch = async () => reply({ detail: "boom" }, 500);
  await loadGrowthPanel();
  assert.ok(state().querySelector("[data-kind=error]"));
  assert.match(state().textContent, /boom/);
  assert.ok(state().querySelector("button"), "retry button");
});

test("the 7d toggle requests the 7d window and marks it pressed", async () => {
  const seen = [];
  globalThis.fetch = async (path) => {
    seen.push(path);
    return reply({ ...BODY, window: "7d" });
  };
  initGrowthPanel(); // runs on DOMContentLoaded in the page, which this DOM never fires
  document.querySelector('#growth-panel [data-window="7d"]').click();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(seen, ["/api/growth?window=7d"]);
  assert.equal(document.querySelector('[data-window="7d"]').getAttribute("aria-pressed"), "true");
  assert.equal(document.querySelector('[data-window="1d"]').getAttribute("aria-pressed"), "false");
  assert.match(document.getElementById("growth-volumes").textContent, /in the last 7 days/);
});

test("the volume line says how old the data is: (as of HH:MM) in local time", async () => {
  const asOf = new Date(2026, 10, 1, 9, 5).toISOString(); // 09:05 local, whatever the zone
  globalThis.fetch = async () => reply({ ...BODY, volumes: [{ ...BODY.volumes[0], as_of: asOf }] });
  await loadGrowthPanel();
  assert.match(document.getElementById("growth-volumes").textContent, /not attributed \(as of 09:05\)\.$/);
});

test("a volume without data for the window still says when its latest data is from", async () => {
  const asOf = new Date(2026, 10, 1, 21, 40).toISOString();
  const stale = { ...BODY.volumes[0], status: "insufficient data", as_of: asOf };
  globalThis.fetch = async () => reply({ ...BODY, volumes: [stale] });
  await loadGrowthPanel();
  assert.match(
    document.getElementById("growth-volumes").textContent,
    /not enough snapshots for the last day yet \(as of 21:40\)\.$/
  );
});

test("no as_of from the server means no as-of text", async () => {
  globalThis.fetch = async () => reply(BODY);
  await loadGrowthPanel();
  assert.doesNotMatch(document.getElementById("growth-volumes").textContent, /as of/);
});
