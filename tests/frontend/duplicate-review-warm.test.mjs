// The Review Queue's "Largest duplicate clusters" loader draws from the warm candidate cache like
// every other view: a cold/stale cache answers a typed 409 `candidates_not_warm` and the loader
// must show the "Indexing your files..." progress, wait for the warm-up and read once more --
// never an error banner. (Incident 2026-10-08: the endpoint used to run its own ~30 min dedup
// pass per request, competing with the warm-up.)
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<div id="duplicate-review-state"></div><div id="duplicate-review-content" hidden></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const reply = (body, status = 200) => ({
  ok: status < 400,
  status,
  statusText: "",
  headers: { get: () => "application/json" },
  json: async () => body,
});

const app = await import("../../src/reclaim/api/static/app.js");
const stateEl = () => document.getElementById("duplicate-review-state");

function installServer({ extra409Reads }) {
  const server = { calls: [], ready: false, reviewReads: 0, warmPollsLeft: 1, seen: "" };
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    server.calls.push(`${method} ${path}`);
    server.seen += stateEl().textContent;
    if (path === "/api/candidates/warm-status") {
      if (!server.ready && server.warmPollsLeft-- <= 0) server.ready = true;
      return reply(
        server.ready
          ? { status: "ready", stale_reason: null }
          : { status: "computing", elapsed_seconds: 3, stale_reason: null }
      );
    }
    if (path === "/api/candidates/warm" && method === "POST") {
      return reply({ status: "computing", elapsed_seconds: 0 }, 202);
    }
    if (path === "/api/duplicate-clusters/review") {
      server.reviewReads += 1;
      if (!server.ready || extra409Reads > 0) {
        extra409Reads -= 1;
        return reply({ detail: "not warm", code: "candidates_not_warm", stale_reason: "cold" }, 409);
      }
      return reply({ has_scan: true, clusters: [] });
    }
    throw new Error(`unexpected request ${method} ${path}`);
  };
  return server;
}

test("cold cache: shows the indexing progress, waits for warm, never an error banner", async () => {
  const server = installServer({ extra409Reads: 0 });
  await app.loadDuplicateClusterReview();
  assert.match(server.seen, /Indexing your files/);
  assert.equal(server.reviewReads, 1, "the review endpoint is only read once the cache is warm");
  assert.doesNotMatch(stateEl().textContent, /Could not load/);
  assert.match(stateEl().textContent, /No duplicate clusters to review/);
});

test("409 between the warm check and the read: one bounded retry, no error banner", async () => {
  // Status says ready from the first poll, but the first read still answers 409.
  const server = installServer({ extra409Reads: 1 });
  server.ready = true;
  await app.loadDuplicateClusterReview();
  assert.equal(server.reviewReads, 2, "one 409 then exactly one retry");
  assert.doesNotMatch(stateEl().textContent, /Could not load/);
});
