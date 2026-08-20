from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Tool input/output shapes for `reclaim.mcp.server`, mirroring `reclaim.api.schemas`'s own
# `extra="forbid"` convention (every unexpected field is a hard error, never silently dropped).
# Deliberately its own module, separate from `reclaim.api.schemas`: these are MCP-tool-facing
# contracts (what an agent sees), not HTTP-facing ones, and the two are allowed to diverge
# (e.g. no `path`/`paths` field exists ANYWHERE in this file — see `reclaim.mcp`'s module
# docstring for why that's the point, not an oversight).


class ScanTriggerResult(BaseModel):
    """`scan(path)`'s immediate response -- the scan itself runs in the background; poll
    `scan_status()` for progress and the resulting `scan_id`."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "running" -- scan() always starts a fresh scan or raises, never returns idle.
    root: str


class ScanStatusResult(BaseModel):
    """`scan_status()`'s response -- mirrors `reclaim.api.schemas.ScanStatusOut`'s core fields,
    trimmed to what an MCP client actually needs to decide "is it done, and what do I call
    list_candidates/preview_apply with." `scan_id` is populated only once `status ==
    "completed"` -- a running, failed, or cancelled scan has no citable scan_id yet (or, for a
    cancelled scan whose partial data still incremented `scan_generation`, no NEW id worth
    surfacing over whatever `scan_id` a prior completed scan already returned)."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "idle" | "running" | "completed" | "failed" | "cancelled"
    root: str | None
    scan_id: str | None
    entries_total: int | None
    files_written: int | None
    error: str | None


class CandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    category: str
    category_group: str
    tier: str
    size_bytes: int
    rationale: str


class ListCandidatesResult(BaseModel):
    """`list_candidates(scan_id, tier, category)`'s response -- wraps `GET /api/candidates`
    (`reclaim.api.service.list_candidates`), scoped to a `scan_id` that must match the scan
    currently reflected in the live index (see `reclaim.mcp.selection.StaleScanError`)."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str
    count: int
    total_bytes: int
    candidates: list[CandidateSummary]


class PreviewApplyResult(BaseModel):
    """`preview_apply(scan_id, rule_id_or_category, tier)`'s response -- `selection_hash` is the
    one value `delete` needs alongside the same three selector inputs to actually execute this
    exact selection; see `reclaim.mcp.selection.compute_selection_hash`'s docstring for the
    commitment this hash makes. `sample_paths` is a capped preview for a human/agent to sanity-
    check before calling `delete` -- never the full path list (an agent has no legitimate need
    to enumerate every path; `item_count`/`bytes_total` already answer "how much, how many")."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str
    tier: str
    rule_id_or_category: str
    selection_hash: str
    item_count: int
    bytes_total: int
    sample_paths: list[str]


class DeleteResult(BaseModel):
    """`delete(scan_id, rule_id_or_category, tier, selection_hash)`'s response once the
    selection_hash check passed and `reclaim.api.service.mcp_execute_delete` actually ran --
    mirrors the real, disk-mutating `BatchApplyReport`'s summary fields (never the full
    `ApplyResponse.items` list -- same "no reason for an agent to enumerate every path"
    reasoning as `PreviewApplyResult.sample_paths` above)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    files_processed: int
    files_succeeded: int
    files_failed: int
    bytes_freed: int
