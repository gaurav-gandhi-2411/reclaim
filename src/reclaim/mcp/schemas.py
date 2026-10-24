from __future__ import annotations

from typing import Literal

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


class DeleteRequestSummary(BaseModel):
    """One row of `delete_requests()`: enough to find a cut-off request, nothing to approve with."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    status: str
    rule_id_or_category: str
    tier: str
    item_count: int
    bytes_total: int


class DeleteRequestsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: list[DeleteRequestSummary]


class DeleteResult(BaseModel):
    """`delete(scan_id, rule_id_or_category, tier, selection_hash)`'s response once the
    selection_hash check passed and `reclaim.api.service.mcp_execute_delete` actually ran --
    mirrors the real, disk-mutating `BatchApplyReport`'s summary fields (never the full
    `ApplyResponse.items` list -- same "no reason for an agent to enumerate every path"
    reasoning as `PreviewApplyResult.sample_paths` above)."""

    model_config = ConfigDict(extra="forbid")

    # "executed": the user approved in Reclaim's window and the delete ran (the fields below are
    # real). "awaiting_user": nothing has been deleted; the user has not answered yet. Call
    # `delete_status(approval_id)` to keep waiting. Declined / expired / stale are errors.
    status: Literal["executed", "awaiting_user"] = "executed"
    approval_id: str | None = None
    message: str | None = None
    batch_id: str | None = None
    files_processed: int = 0
    files_succeeded: int = 0
    files_failed: int = 0
    # Genuinely freed bytes only; Recycle Bin / vault moves (recoverable, space still held) are
    # reported in `bytes_moved`.
    bytes_freed: int = 0
    bytes_moved: int = 0
    # ADR-0036: how many items apply_batch's pre-flight skipped without attempting them, per
    # `PreflightSkipReason` (e.g. {"size_or_mtime_changed_since_scan": 2}) -- counts only, never
    # paths, same no-enumeration posture as the rest of this model. `files_failed` includes them;
    # without this an agent cannot tell "skipped because it changed" from a real failure.
    skipped_by_reason: dict[str, int] = {}


class PlannedSelection(BaseModel):
    """One whole selection in a `plan_free_space` result: pass its `rule_id_or_category`,
    `tier` and `selection_hash` to `delete` (which still asks the user)."""

    model_config = ConfigDict(extra="forbid")

    rule_id_or_category: str
    category_group: str
    tier: str
    selection_hash: str
    item_count: int
    bytes_total: int
    reversible: bool
    risk_note: str


class PlanFreeSpaceResult(BaseModel):
    """`plan_free_space(...)`: a deterministic plan over the current scan. `reaches_target`
    false means the constraints cannot reach the target: `shortfall_bytes` says by how much,
    and no constraint was relaxed to hide it. `frees_now_bytes` is 0 by construction: every
    assistant delete goes to the vault or the Recycle Bin (invariant I7), which frees the
    space only when it is purged or emptied."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str
    target_bytes: int
    planned_bytes: int
    reaches_target: bool
    shortfall_bytes: int
    frees_now_bytes: int
    selections: list[PlannedSelection]
    left_out: list[str]


class DriveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drive: str
    total_bytes: int
    free_bytes: int


class DiskStatusResult(BaseModel):
    """`get_disk_status()`: every fixed drive's free and total space."""

    model_config = ConfigDict(extra="forbid")

    drives: list[DriveStatus]


class ExclusionsResult(BaseModel):
    """`list_exclusions()`: what Reclaim never touches. No tool argument can widen these."""

    model_config = ConfigDict(extra="forbid")

    project_names: list[str]
    deny_patterns: list[str]


class UsageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    is_dir: bool
    reclaimable: bool


class CategoryUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_group: str
    label: str
    tier: str
    file_count: int
    bytes: int


class DiskUsageResult(BaseModel):
    """`explain_disk_usage(scan_id)`: the largest entries under the scanned root, and what
    Reclaim can reclaim by category and tier."""

    model_config = ConfigDict(extra="forbid")

    scan_id: str
    root: str | None
    total_bytes: int
    largest: list[UsageEntry]
    reclaimable: list[CategoryUsage]


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    size_bytes: int
    referenced_by: list[str]


class UnusedModelsResult(BaseModel):
    """`find_unused_models()`: cached Hugging Face models and the project files naming them.
    An empty `referenced_by` means no readable project file names the model; excluded projects
    are never read (`excluded_projects_not_read`), so they may still use it."""

    model_config = ConfigDict(extra="forbid")

    cache_dir: str
    models: list[ModelEntry]
    files_read: int
    stopped_at_limit: bool
    excluded_projects_not_read: bool


class RelocationOption(BaseModel):
    """One folder the "Move to Another Drive" feature knows about. `verdict` is `movable`,
    `guided` (moved with its program's own steps), `already_moved` or `must_stay`; `reasons`
    says why for anything not movable. An output only: no tool takes a path."""

    model_config = ConfigDict(extra="forbid")

    path: str
    verdict: str
    size_bytes: int
    files: int
    reasons: list[str]
    suggested_target: str | None


class RelocationOptionsResult(BaseModel):
    """`list_relocation_options()`: large folders that could move to another drive, movable
    first, then by size. Nothing is moved by this tool or by any other: the user moves a folder
    on the dashboard's "Move to Another Drive" page or with `reclaim relocate`, after a dry
    run they confirm."""

    model_config = ConfigDict(extra="forbid")

    target_drive: str | None
    target_drive_free_bytes: int | None
    options: list[RelocationOption]
