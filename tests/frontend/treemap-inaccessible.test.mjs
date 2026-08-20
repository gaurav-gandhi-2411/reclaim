// P0-5 treemap follow-up: the synthetic "inaccessible" bucket node build_treemap appends must
// actually render inside the treemap SVG itself -- distinctly styled, non-interactive, and with
// its explanation string surfaced via aria-label / tooltip -- not just live in the API response.
// Mirrors scan-confirm.test.mjs/xss.test.mjs's harness pattern (a real JSDOM + the production
// treemap.js module, not a reimplementation).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  '<!doctype html><html><body>' +
    '<svg id="treemap-svg" viewBox="0 0 800 520"></svg>' +
    '<div id="treemap-tooltip"></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { renderTreemap } = await import("../../src/reclaim/api/static/treemap.js");

const ORDINARY_NODE = {
  path: "C:/Users/gaura/Downloads/old_installer.exe",
  label: "old_installer.exe",
  size_bytes: 500_000_000,
  size_human: "476.8 MB",
  category_group: "old_installers",
  category_label: "Old Installers (Downloads)",
  is_dir: false,
  is_candidate: true,
  is_inaccessible: false,
  explanation: null,
};

const INACCESSIBLE_NODE = {
  path: "__inaccessible__",
  label: "Inaccessible / unreadable",
  size_bytes: 12_345,
  size_human: "12.1 KB",
  category_group: "inaccessible",
  category_label: "Inaccessible (permission denied)",
  is_dir: false,
  is_candidate: false,
  is_inaccessible: true,
  explanation:
    "2 path(s) could not be read due to permissions or a real I/O error -- size shown is a " +
    "best-effort estimate, not an exact figure. 1 of those have no size estimate at all, so " +
    "the true total is larger than the bytes shown here.",
};

function render(nodes) {
  const svg = document.getElementById("treemap-svg");
  const tooltip = document.getElementById("treemap-tooltip");
  renderTreemap(svg, tooltip, nodes);
  return { svg, tooltip };
}

test("renderTreemap: an ordinary candidate node gets no inaccessible styling", () => {
  const { svg } = render([ORDINARY_NODE]);
  const groups = svg.querySelectorAll("g.rc-treemap-node");
  assert.equal(groups.length, 1);
  assert.equal(groups[0].classList.contains("rc-treemap-node--inaccessible"), false);
  assert.ok(!groups[0].getAttribute("aria-label").includes("estimated"));
});

test("renderTreemap: the inaccessible bucket node renders with its own distinct class", () => {
  const { svg } = render([ORDINARY_NODE, INACCESSIBLE_NODE]);
  const groups = svg.querySelectorAll("g.rc-treemap-node");
  assert.equal(groups.length, 2);
  const inaccessibleGroup = [...groups].find((g) =>
    g.classList.contains("rc-treemap-node--inaccessible")
  );
  assert.ok(inaccessibleGroup, "inaccessible node must carry rc-treemap-node--inaccessible");
});

test("renderTreemap: the inaccessible node's aria-label carries its explanation, visible without a hover", () => {
  const { svg } = render([INACCESSIBLE_NODE]);
  const group = svg.querySelector("g.rc-treemap-node--inaccessible");
  const ariaLabel = group.getAttribute("aria-label");
  assert.ok(ariaLabel.includes(INACCESSIBLE_NODE.explanation), "aria-label must include the real explanation text");
});

test("renderTreemap: hovering the inaccessible node shows its explanation in the tooltip, never the synthetic path", () => {
  const { svg, tooltip } = render([INACCESSIBLE_NODE]);
  const group = svg.querySelector("g.rc-treemap-node--inaccessible");
  group.dispatchEvent(new dom.window.Event("mouseenter"));
  assert.ok(tooltip.textContent.includes(INACCESSIBLE_NODE.explanation));
  assert.ok(!tooltip.textContent.includes("__inaccessible__"));
  assert.equal(tooltip.style.visibility, "visible");
});

test("renderTreemap: hovering an ordinary node's tooltip still shows its real path (no regression)", () => {
  const { svg, tooltip } = render([ORDINARY_NODE]);
  const group = svg.querySelector("g.rc-treemap-node");
  group.dispatchEvent(new dom.window.Event("mouseenter"));
  assert.ok(tooltip.textContent.includes(ORDINARY_NODE.path));
});
