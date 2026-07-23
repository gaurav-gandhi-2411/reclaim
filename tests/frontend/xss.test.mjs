// Regression test for the filename-driven XSS finding in the duplicate-cluster review table
// (src/reclaim/api/static/app.js::renderClusterTable). Reclaim's whole job is walking a real
// disk, so a file or directory literally named `<img src=x onerror=...>` is real, reachable
// input — this proves the render path treats it as inert text, never as markup, using the
// exact function the production dashboard calls (not a reimplementation).
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { renderClusterTable, renderAISuggestionCard } = await import(
  "../../src/reclaim/api/static/app.js"
);

function makeCluster(maliciousPath) {
  return {
    full_hash: "a".repeat(64),
    members: [
      {
        path: maliciousPath,
        size_bytes: 10,
        size_human: "10 B",
        ctime: 1_700_000_000,
        is_keep: true,
      },
      {
        path: "C:/Users/gg/Downloads/dupe.bin",
        size_bytes: 10,
        size_human: "10 B",
        ctime: 1_700_000_000,
        is_keep: false,
      },
    ],
  };
}

test("an <img onerror> payload as a filename renders as inert text, not markup", () => {
  const payload = '<img src=x onerror="window.__xssFired = true">';
  const table = renderClusterTable(makeCluster(payload));

  assert.equal(table.querySelectorAll("img").length, 0, "payload must not parse into an <img>");
  assert.equal(globalThis.window.__xssFired, undefined, "onerror must never execute");

  const pathCell = table.querySelector("td.rc-candidate-path");
  assert.equal(pathCell.textContent, payload, "the raw payload must survive as literal text");
  assert.equal(pathCell.innerHTML.includes("<img"), false, "no markup in the cell's own HTML");
});

test("a <script> payload as a filename renders as inert text, not markup", () => {
  const payload = "<script>window.__xssFired = true</script>evil.txt";
  const table = renderClusterTable(makeCluster(payload));

  assert.equal(table.querySelectorAll("script").length, 0, "payload must not parse into a <script>");
  assert.equal(globalThis.window.__xssFired, undefined, "the script body must never execute");

  const pathCell = table.querySelector("td.rc-candidate-path");
  assert.equal(pathCell.textContent, payload, "the raw payload must survive as literal text");
});

test("a benign path still renders correctly (no over-escaping regression)", () => {
  const path = "C:/Users/gg/Downloads/report (final).pdf";
  const table = renderClusterTable(makeCluster(path));

  const pathCell = table.querySelector("td.rc-candidate-path");
  assert.equal(pathCell.textContent, path);
});

// Regression coverage for the AI Suggestions render path (ADR-0025) -- reclaim.ai.presentation
// output is fixed, server-authored prose (safe to trust), but every `member.path` is still a
// raw, attacker-controllable filesystem path and must go through the exact same textContent-only
// discipline as renderClusterTable above.
function makeAISuggestion(maliciousPath) {
  return {
    cluster_id: "near-identical-0",
    track: "near_identical_image",
    headline: "These look like the same photo saved more than once",
    detail_lines: ["We recommend keeping this copy — it scored higher on our quality check."],
    is_suggestion: true,
    browse_only_note: null,
    keep_path: maliciousPath,
    technical_detail: "Hamming distance 4 of 64 bits (lower = more similar)",
    members: [
      { path: maliciousPath, size_bytes: 10, size_human: "10 B", is_recommended_keep: true, position: null },
      { path: "C:/Photos/img2.jpg", size_bytes: 9, size_human: "9 B", is_recommended_keep: false, position: null },
    ],
  };
}

test("an <img onerror> payload as an AI-suggestion member path renders as inert text", () => {
  const payload = '<img src=x onerror="window.__xssFired2 = true">';
  const card = renderAISuggestionCard(makeAISuggestion(payload));

  assert.equal(card.querySelectorAll("img").length, 0, "payload must not parse into an <img>");
  assert.equal(globalThis.window.__xssFired2, undefined, "onerror must never execute");

  const pathSpans = [...card.querySelectorAll("span.rc-candidate-path")];
  assert.ok(pathSpans.some((span) => span.textContent === payload));
  for (const span of pathSpans) {
    assert.equal(span.innerHTML.includes("<img"), false, "no markup in the member path span");
  }
});

test("a semantic_image AI suggestion never renders a checkbox (no action affordance at all)", () => {
  const suggestion = makeAISuggestion("C:/Photos/scene1.jpg");
  suggestion.track = "semantic_image";
  suggestion.is_suggestion = false;
  suggestion.browse_only_note = "Browse only — we won't suggest deleting any of these.";

  const card = renderAISuggestionCard(suggestion);

  assert.equal(
    card.querySelectorAll('input[type="checkbox"]').length,
    0,
    "semantic_image must never render a selection checkbox"
  );
});
