// SIMPLE mode's one-line disk-growth summary (ADR-0046 data, GET /api/growth?window=7d). Same
// harness as growth-panel.test.mjs: a real JSDOM + the real app.js module, fetch faked on
// globalThis. The full "What changed" panel must keep working unchanged (ADVANCED mode).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html data-mode=\"simple\"><body>" +
    '<section id="simple-view"><div id="simple-view-content"></div></section>' +
    '<section class="rc-view" id="view-overview" data-active="true">' +
    '<section id="growth-panel">' +
    '<button data-window="1d" aria-pressed="true"></button>' +
    '<button data-window="7d" aria-pressed="false"></button>' +
    '<div id="growth-state"></div><div id="growth-content" hidden>' +
    '<ul id="growth-volumes"></ul><ol id="growth-rows"></ol><p id="growth-excluded"></p>' +
    "</div></section></section>" +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { renderSimpleIdle, simpleGrowthLineText, loadGrowthPanel } = await import(
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
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

const volume = (change, extra = {}) => ({
  volume: "C:\\",
  change_bytes: change,
  attributed_bytes: Math.max(change, 0),
  not_attributed_bytes: 0,
  status: "ok",
  ...extra,
});
const row = (label, growth, extra = {}) => ({
  path: `C:\\secret\\${label}`,
  label,
  kind: "cache",
  project: "",
  session: "",
  growth_bytes: growth,
  partial: false,
  review_link: null,
  ...extra,
});
const body = (over = {}) => ({
  window: "7d",
  generated_at: "2026-11-01T00:00:00+00:00",
  status: "ok",
  volumes: [volume(6.2 * GIB)],
  rows: [row("Docker", 5 * GIB), row("npm cache", 1 * GIB)],
  excluded_aggregate: { label: "Excluded projects", bytes: 2 * GIB },
  anomalies: [],
  ...over,
});

const lineEl = () => document.getElementById("simple-growth-line");

async function idleWith(response) {
  globalThis.fetch = async () => response;
  renderSimpleIdle();
  await settle();
}

test("growth: names the volume, the GB figure (1 decimal) and the top contributor", async () => {
  const seen = [];
  globalThis.fetch = async (path) => {
    seen.push(path);
    return reply(body());
  };
  renderSimpleIdle();
  await settle();
  assert.equal(lineEl().textContent, "C: grew 6.2 GB this week, mostly Docker.");
  assert.deepEqual(seen, ["/api/growth?window=7d"], "same endpoint as the panel, 7-day window");
  assert.equal(lineEl().hidden, false);
});

test("growth: when no single folder dominates the line says 'biggest', not 'mostly'", () => {
  const text = simpleGrowthLineText(
    body({ rows: [row("Docker", 2 * GIB), row("npm cache", 1 * GIB)] })
  );
  assert.equal(text, "C: grew 6.2 GB this week, biggest: Docker.");
});

test("growth: with no itemised rows the figure is shown without a contributor", () => {
  assert.equal(simpleGrowthLineText(body({ rows: [] })), "C: grew 6.2 GB this week.");
});

test("shrink: says it shrank, plainly, with a positive figure", async () => {
  await idleWith(reply(body({ volumes: [volume(-3 * GIB)], rows: [] })));
  assert.equal(lineEl().textContent, "C: shrank 3.0 GB this week.");
});

test("steady: a change that would round to 0.0 GB reads as held steady", async () => {
  await idleWith(reply(body({ volumes: [volume(0.01 * GIB)], rows: [] })));
  assert.equal(lineEl().textContent, "C: held steady this week.");
});

test("no history: the panel's own empty condition shows a neutral line", async () => {
  await idleWith(reply(body({ status: "insufficient data", rows: [] })));
  assert.equal(lineEl().textContent, "Disk growth: not enough snapshots yet.");
  assert.equal(lineEl().hidden, false);
});

test("no history: a volume without data is not summarised", () => {
  assert.equal(
    simpleGrowthLineText(body({ volumes: [volume(0, { status: "insufficient data" })] })),
    null
  );
});

test("loading: a placeholder shows while the request is pending, in the same paragraph", async () => {
  let release;
  globalThis.fetch = () => new Promise((resolve) => (release = () => resolve(reply(body()))));
  renderSimpleIdle();
  assert.equal(lineEl().textContent, "Checking disk growth…");
  assert.equal(lineEl().getAttribute("aria-live"), "polite");
  release();
  await settle();
  assert.match(lineEl().textContent, /^C: grew/);
});

test("error: a failed request hides the line quietly and the page still renders", async () => {
  globalThis.fetch = async () => reply({ detail: "boom" }, 500);
  renderSimpleIdle();
  await settle();
  assert.equal(lineEl().hidden, true);
  assert.equal(lineEl().textContent, "", "no error wording on the Simple page");
  assert.equal(
    [...document.querySelectorAll("#simple-view-content button")].filter(
      (b) => b.textContent === "Clean My Computer"
    ).length,
    1
  );
});

test("error: a network failure (fetch rejects) is swallowed the same way", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("network down");
  };
  renderSimpleIdle();
  await settle();
  assert.equal(lineEl().hidden, true);
});

test("layout: the line comes after every button, so the main button cannot move", async () => {
  await idleWith(reply(body()));
  const kids = [...document.getElementById("simple-view-content").children];
  const lineIdx = kids.indexOf(lineEl());
  const lastButtonIdx = Math.max(
    ...kids.map((el, i) => (el.tagName === "BUTTON" ? i : -1))
  );
  assert.ok(lineIdx > lastButtonIdx, "growth line is below the buttons");
  assert.equal(lineEl().tagName, "P");
});

test("excluded projects: only the aggregate label may appear, never a path or folder", async () => {
  const excludedRow = row("<excluded projects>", 5 * GIB, {
    path: "C:\\Users\\g\\secret-client-project",
    kind: "excluded",
  });
  await idleWith(reply(body({ rows: [excludedRow, row("npm cache", 1 * GIB)] })));
  const text = lineEl().textContent;
  assert.equal(text, "C: grew 6.2 GB this week, mostly Excluded projects.");
  assert.equal(text.includes("secret"), false);
  assert.equal(text.includes("C:\\Users"), false);
});

test("the row's path is never used, only its label", () => {
  const text = simpleGrowthLineText(body());
  assert.equal(text.includes("secret"), false);
});

test("a server label is rendered as inert text", async () => {
  await idleWith(reply(body({ rows: [row('<img src=x onerror="window.__fired=1">', 6 * GIB)] })));
  assert.equal(lineEl().querySelectorAll("img").length, 0);
  assert.equal(window.__fired, undefined);
});

test("advanced: the full What-changed panel still renders, without the one-liner", async () => {
  globalThis.fetch = async () => reply(body({ window: "1d" }));
  await loadGrowthPanel();
  assert.equal(document.getElementById("growth-content").hidden, false);
  assert.match(document.getElementById("growth-volumes").textContent, /C:\\ grew 6\.2 GB in the last day/);
  assert.equal(document.querySelectorAll("#growth-rows li").length, 2);
  assert.match(document.getElementById("growth-excluded").textContent, /Excluded projects: \+2\.0 GB/);
  assert.equal(
    document.querySelector("#growth-panel #simple-growth-line"),
    null,
    "the one-liner is not part of the panel"
  );
});
