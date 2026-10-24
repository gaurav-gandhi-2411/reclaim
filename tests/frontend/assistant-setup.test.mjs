// DOM-level test for the Settings panel "Use Reclaim from your AI assistant" (run spec Phase C).
// The text comes from the server and holds a path; it must render as text, and there must be no
// control that writes a client's config.
import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body>" +
    '<p id="assistant-setup-status"></p><div id="assistant-setup" hidden>' +
    '<code id="assistant-claude-desktop"></code><code id="assistant-claude-code"></code>' +
    '<code id="assistant-codex"></code></div>' +
    "</body></html>"
);
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const calls = [];
globalThis.fetch = async (path, options = {}) => {
  calls.push({ path, options });
  return new Response(
    JSON.stringify({
      command: 'C:\\Users\\<img src=x onerror="globalThis.pwned=1">\\reclaim.exe',
      args: ["mcp-serve"],
      installed: true,
      claude_desktop_json: '{"mcpServers": {"reclaim": {"command": "<b>x</b>"}}}',
      claude_code_command: 'claude mcp add --scope user reclaim -- "<img>" mcp-serve',
      codex_toml: "[mcp_servers.reclaim]\ncommand = '<script>x</script>'",
    }),
    { status: 200, headers: { "content-type": "application/json" } }
  );
};

const { loadAssistantSetup } = await import("../../src/reclaim/api/static/app.js");

test("setup text renders inert and the panel only reads", async () => {
  await loadAssistantSetup();
  const box = document.getElementById("assistant-setup");
  assert.equal(box.hidden, false, document.getElementById("assistant-setup-status").textContent);
  assert.equal(box.querySelector("img, b, script"), null);
  assert.ok(document.getElementById("assistant-codex").textContent.includes("<script>"));
  assert.equal(globalThis.pwned, undefined);
  assert.deepEqual(
    calls.map((c) => [c.path, c.options.method ?? "GET"]),
    [["/api/assistant/setup", "GET"]]
  );
});
