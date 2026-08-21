from __future__ import annotations

# reclaim.mcp — Model Context Protocol (MCP) control surface for Reclaim, audit gap R7
# (docs/AUDIT-2026-08.md).
#
# THREAT MODEL: an MCP client is, by definition, driven by an LLM agent this codebase must
# treat as potentially compromised or malicious — a prompt injection hidden in scanned file
# content, a misbehaving/buggy agent, or a genuinely hostile MCP client speaking the protocol
# directly. The one thing this package must NEVER allow, under any circumstance, is an agent
# constructing a novel delete target. Every tool exposed here selects files EXCLUSIVELY by
# `(scan_id, tier, rule_id_or_category)` against the deterministic candidate set
# `reclaim.api.service` already computes for every other read surface in this app — there is
# no tool, parameter, or code path anywhere under this package that accepts a raw filesystem
# path and forwards it toward deletion. Today's `POST /api/apply`'s free-form `paths:
# list[str]` field is exactly the shape this package structurally cannot expose — see
# `evals/test_mcp_safety_gate.py`, this package's own AST/schema-level proof, mirroring
# `evals/test_ai_safety_gate.py`'s guarantee for `reclaim.ai`.
#
# Two independent structural guarantees, HARD BOUNDARIES enforced by CI (evals/
# test_mcp_safety_gate.py), not just convention:
#
# 1. No module under this package ever imports `reclaim.executor` or `send2trash` directly —
#    every real mutation is reached only through `reclaim.api.service`'s choke-point functions
#    (`service.mcp_execute_delete`, which itself calls `reclaim.executor.apply_batch` — the one
#    place any of this codebase actually deletes/quarantines a file). `reclaim.mcp.selection`'s
#    hash/error types are pure data with zero dependency on either side.
# 2. `delete` never accepts a path. It accepts `(scan_id, tier, rule_id_or_category,
#    selection_hash)` — a commitment over a specific, server-computed selection a prior
#    `preview_apply` call already resolved — and refuses (a typed error, no partial/fallback
#    execution) if a fresh re-derivation of that exact selection doesn't hash to the same
#    value. This binds a delete to a specific prior preview: a stale scan (a newer scan
#    completed since), a race with a manual apply that changed the underlying candidate set, or
#    a tampered hash are all refused the same way — never a silent no-op, never a fallback to
#    some other selection.
