// "Explain this category" calls an endpoint that reads the warm candidates cache server-side, so a
// cold/stale cache answers the typed 409 `candidates_not_warm`. It must show the wait (and retry
// once after the warm-up), never a red error.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body><button id=b></button><p id=r></p></body></html>");
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

function installServer({ ready }) {
  const server = { ready, reads: 0, seen: "" };
  const resultEl = () => document.getElementById("r");
  globalThis.fetch = async (path, options = {}) => {
    const method = options.method ?? "GET";
    server.seen += resultEl().textContent;
    if (path === "/api/candidates/warm-status") {
      server.ready = true;
      return reply({ status: "ready", stale_reason: null });
    }
    if (path === "/api/candidates/warm" && method === "POST") {
      return reply({ status: "computing", elapsed_seconds: 0 }, 202);
    }
    if (path.startsWith("/api/ai/category-explanation/")) {
      server.reads += 1;
      if (server.reads === 1 && !ready) {
        return reply({ detail: "not warm", code: "candidates_not_warm", stale_reason: "cold" }, 409);
      }
      return reply({ status: "ok", explanation: "EXPLAINED", message: null });
    }
    throw new Error(`unexpected request ${method} ${path}`);
  };
  return server;
}

test("409 from the explanation endpoint: one bounded retry, no red error", async () => {
  const server = installServer({ ready: false });
  const buttonEl = document.getElementById("b");
  const resultEl = document.getElementById("r");
  await app.explainCategory("duplicates", buttonEl, resultEl);
  assert.equal(server.reads, 2, "one 409 then exactly one retry");
  assert.equal(resultEl.textContent, "EXPLAINED");
  assert.notEqual(resultEl.dataset.tone, "error");
  assert.equal(buttonEl.disabled, false);
});

test("warm cache: a single read, explanation shown", async () => {
  const server = installServer({ ready: true });
  const resultEl = document.getElementById("r");
  await app.explainCategory("duplicates", document.getElementById("b"), resultEl);
  assert.equal(server.reads, 1);
  assert.equal(resultEl.textContent, "EXPLAINED");
});
