from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from reclaim.executor import QuarantineMethod
from reclaim.models import Tier, Verdict

# --- Shared formatting -----------------------------------------------------------------------


def format_bytes(size_bytes: int) -> str:
    """Human-readable size string (e.g. "4.2 GB"). Base-1024 division, labeled with the
    decimal-style unit names Windows Explorer itself uses for this exact tool's target
    audience — deliberately matching what the user sees when they compare against Explorer,
    not a stricter-but-unfamiliar KiB/MiB/GiB labeling. Every caller of this function also
    carries the exact integer byte count alongside the formatted string (never formatted-only),
    so nothing here can misrepresent precision the API doesn't actually have (spec: "no
    fabricated confidence" applied to size reporting, not just detection scores)."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size_bytes)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"  # pragma: no cover -- unreachable, loop always returns above


_CATEGORY_LABELS: dict[str, str] = {
    "dev_artifacts": "Dev Artifacts",
    "package_caches": "Package Caches",
    "model_caches": "Model Weight Caches",
    "temp_and_browser_caches": "Browser & Temp Caches",
    "crash_dumps": "Crash Dumps & WER Reports",
    "old_installers": "Old Installers (Downloads)",
    "archive_pairs": "Extracted Archive Pairs",
    "large_logs": "Large Stale Logs",
    "duplicates": "Exact Duplicates",
    "other": "Uncategorized",
}


def category_label(category_group: str) -> str:
    """Display label for a `category_group` id. Falls back to a title-cased rendering of the
    id itself so an unrecognized future category still renders something legible rather than
    a raw snake_case id or a crash."""
    return _CATEGORY_LABELS.get(category_group, category_group.replace("_", " ").title())


# --- Scan --------------------------------------------------------------------------------


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class ScanStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    root: str | None
    started_at: float | None
    finished_at: float | None
    error: str | None
    dirs_visited: int | None
    entries_total: int | None
    files_written: int | None
    files_unchanged: int | None
    files_pruned: int | None
    elapsed_seconds: float | None


# --- Summary / category cards -------------------------------------------------------------


class CategoryCardOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_group: str
    category_label: str
    tier: Tier
    file_count: int
    total_bytes: int
    total_bytes_human: str


class SummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    total_indexed_bytes: int
    total_indexed_human: str
    tier_a_bytes: int
    tier_a_count: int
    tier_b_bytes: int
    tier_b_count: int
    categories: list[CategoryCardOut]


# --- Treemap -------------------------------------------------------------------------------


class TreemapNodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    label: str
    size_bytes: int
    size_human: str
    category_group: str
    category_label: str
    is_dir: bool
    is_candidate: bool


class TreemapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    root: str | None
    total_bytes: int
    total_bytes_human: str
    nodes: list[TreemapNodeOut]


# --- Candidates / duplicate clusters --------------------------------------------------------


class DuplicateMemberOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    size_human: str
    ctime: float
    ctime_iso: str
    is_keep: bool


class DuplicateClusterOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_hash: str
    members: list[DuplicateMemberOut]


class CandidateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    is_dir: bool
    category: str
    category_group: str
    category_label: str
    size_bytes: int
    size_human: str
    tier: Tier
    rationale: str
    rebuild_instruction: str | None
    recovery_cost_note: str | None = None
    # ADR-0006: hardlink-aware estimate, distinct from size_bytes's logical size. `None` means
    # "not computed for this category" — the dashboard must never treat that as a claim of zero.
    reclaimable_bytes: int | None = None
    safety_verdict: Verdict
    safety_reason_code: str
    duplicate_cluster: DuplicateClusterOut | None = None


class CandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    candidates: list[CandidateOut]
    count: int
    total_bytes: int
    total_bytes_human: str


# --- Apply / dry-run -------------------------------------------------------------------------


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str = "A"  # "A" | "B" | "both"
    category_group: str | None = None
    paths: list[str] | None = None
    method: QuarantineMethod = "vault"
    # Mirrors `executor.apply_batch`'s own default (`apply=False`) exactly: omitting this field,
    # or setting it to `true` explicitly, must both be a true no-op on disk. `dry_run=False` is
    # the only value that ever calls `apply_batch(..., apply=True)`.
    dry_run: bool = True


class ItemApplyResultOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    category: str
    category_group: str
    size_bytes: int
    tier: Tier
    method: QuarantineMethod
    succeeded: bool
    error: str | None
    vault_path: str | None


class CategoryBreakdownOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_group: str
    category_label: str
    count: int
    bytes_freed: int
    bytes_freed_human: str


class ApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    apply: bool  # False => dry-run, nothing touched disk.
    method: QuarantineMethod
    items: list[ItemApplyResultOut]
    files_processed: int
    files_succeeded: int
    files_failed: int
    bytes_freed: int
    bytes_freed_human: str
    category_breakdown: list[CategoryBreakdownOut]
    disk_free_before_bytes: int | None
    disk_free_after_bytes: int | None
    disk_free_delta_bytes: int | None


# --- Quarantine / restore --------------------------------------------------------------------


class QuarantineItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_path: str
    size_bytes: int
    size_human: str
    category: str
    category_group: str
    rationale: str
    tier: Tier
    method: QuarantineMethod
    restored: bool
    restored_at: float | None


class QuarantineBatchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    method: QuarantineMethod
    quarantined_at: float
    item_count: int
    bytes_total: int
    bytes_total_human: str
    restored_count: int
    can_restore: bool
    restore_blocked_reason: str | None
    items: list[QuarantineItemOut]


class QuarantineListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batches: list[QuarantineBatchOut]


class RestoreItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_path: str
    size_bytes: int
    succeeded: bool
    already_restored: bool
    error: str | None
    restore_unsupported: bool = False


class RestoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    items: list[RestoreItemOut]
    files_processed: int
    files_succeeded: int
    files_failed: int
    files_unsupported: int
    bytes_restored: int
    bytes_restored_human: str
