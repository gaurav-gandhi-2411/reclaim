from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from reclaim.executor import QuarantineMethod
from reclaim.models import Mode, Tier, Verdict

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
    # ADR-0025: the category_group `apply_selection` assigns to an explicitly-named path that
    # wasn't already a deterministic candidate (the common case for an AI-suggestion apply) --
    # never emitted by `reclaim.detectors`, only by `api/service.py`'s apply-time safety check.
    "user_selected": "Individually Selected Items",
}


def category_label(category_group: str) -> str:
    """Display label for a `category_group` id. Falls back to a title-cased rendering of the
    id itself so an unrecognized future category still renders something legible rather than
    a raw snake_case id or a crash."""
    return _CATEGORY_LABELS.get(category_group, category_group.replace("_", " ").title())


# Plain-language (name, safety-reason) pairs for the one-click clean summary — deliberately
# distinct from `_CATEGORY_LABELS`/`category_label` above, which stay short and technical for
# the Overview/Treemap/Review Queue views that predate this. `safety_reason` states WHY
# something is safe to remove (the rebuild mechanism), never a confidence percentage (house
# rule: no fabricated confidence anywhere in UI copy) — `None` means no specific reason beyond
# what `rationale`/`rebuild_instruction` already say per-candidate.
_PLAIN_LANGUAGE_CATEGORY: dict[str, tuple[str, str | None]] = {
    "dev_artifacts": (
        "Rebuildable developer files",
        "Safe — your build tools recreate these automatically (e.g. npm install).",
    ),
    "package_caches": (
        "Package manager caches",
        "Safe — re-downloaded automatically when needed.",
    ),
    "temp_and_browser_caches": (
        "Temporary & browser cache files",
        "Safe — recreated automatically as you browse.",
    ),
    "crash_dumps": (
        "Crash report files",
        "Safe — only useful for debugging past crashes.",
    ),
    "old_installers": (
        "Old installer downloads",
        "The installed program keeps working — this is just the setup file.",
    ),
    "archive_pairs": (
        "Extracted archives",
        "You already extracted this — the archive itself is redundant.",
    ),
    "large_logs": ("Large log files", None),
    "duplicates": ("Duplicate copies", "One copy is always kept."),
}


def plain_language_category(category_group: str) -> tuple[str, str | None]:
    """Non-technical (name, safety_reason) pair for a `category_group` id, for the one-click
    clean summary. Falls back to `category_label`'s technical label with no safety reason for
    any `category_group` this mapping doesn't cover (e.g. `model_caches`, `other`, a future
    `ai_`-namespaced group) so an unmapped id still renders something legible rather than a
    raw snake_case id or a crash."""
    if category_group in _PLAIN_LANGUAGE_CATEGORY:
        return _PLAIN_LANGUAGE_CATEGORY[category_group]
    return category_label(category_group), None


# --- Scan --------------------------------------------------------------------------------


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class SuggestedScanRootOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    path: str


class SuggestedScanRootsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roots: list[SuggestedScanRootOut]


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
    # D12: real count (+ a sample of actual paths) of entries the scan could not stat/list
    # (permission error, genuine I/O fault) -- see `reclaim.scanner.SkippedPath`.
    skipped_unreadable_count: int | None
    skipped_unreadable_paths: list[str] | None


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
    # D12: real count (+ a sample of actual paths) of entries the most recent completed scan
    # (this process's session) could not stat/list -- see `reclaim.scanner.SkippedPath`. Always
    # `0`/`[]` when no scan has completed yet in this process, never `None` -- unlike
    # `ScanStatusOut`, this endpoint has no "no scan status recorded" state of its own to
    # distinguish from "zero skipped".
    skipped_unreadable_count: int
    skipped_unreadable_paths: list[str]


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


class DuplicateClusterReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: DuplicateClusterOut
    reclaimable_bytes: int
    reclaimable_bytes_human: str
    needs_review: bool
    rationale: str


class DuplicateClusterReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    clusters: list[DuplicateClusterReviewOut]


class CandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    candidates: list[CandidateOut]
    count: int
    total_bytes: int
    total_bytes_human: str


# --- One-click clean (categorically-safe groups only) ----------------------------------------


class OneClickGroupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_group: str
    plain_label: str
    safety_reason: str | None
    file_count: int
    total_bytes: int
    total_bytes_human: str
    # Explicit, enumerated paths for this group — the dashboard's one-click apply sends these
    # straight through to `/api/apply`'s `paths` field, never a blanket tier/category-group
    # selection (safe mode's `apply_selection` guard refuses that regardless; see
    # `service.build_one_click_summary`'s docstring for why this endpoint is the single
    # source of the group -> paths resolution, not a second copy of it in the frontend).
    paths: list[str]


class OneClickCleanSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_scan: bool
    groups: list[OneClickGroupOut]
    total_bytes: int
    total_bytes_human: str
    total_file_count: int


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


# --- AI suggestions (recommend-only; reclaim.ai.presentation output only, never a raw
# AICluster/AIClusterMember -- see ADR-0025) ---------------------------------------------------


class AITrackSkipOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    reason: str


class AIAnalysisStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str  # "unavailable" | "idle" | "running" | "completed" | "failed"
    unavailable_reason: str | None
    scan_generation: int | None
    stale: bool
    started_at: float | None
    finished_at: float | None
    error: str | None
    tracks_run: list[str]
    tracks_skipped: list[AITrackSkipOut]
    files_considered: dict[str, int]
    files_capped: dict[str, int]


class AIClusterMemberOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    size_human: str
    is_recommended_keep: bool
    position: int | None


class AISuggestionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    track: str
    headline: str
    detail_lines: list[str]
    is_suggestion: bool
    browse_only_note: str | None
    keep_path: str | None
    technical_detail: str
    members: list[AIClusterMemberOut]


class AISuggestionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    unavailable_reason: str | None
    stale: bool
    suggestions: list[AISuggestionOut]


# --- Stage 2: mode + first-run ----------------------------------------------------------------


class ModeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode
    # Not a secret — the phrase is meant to be displayed and typed by the user. Included here
    # (rather than only checked server-side) so the dashboard renders the single source of
    # truth (reclaim.mode.REQUIRED_POWER_MODE_CONFIRMATION) instead of a second, hardcoded
    # copy in app.js that could drift from what the server actually requires.
    required_power_confirmation: str


class PowerModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Must exactly equal reclaim.mode.REQUIRED_POWER_MODE_CONFIRMATION — validated in
    # reclaim.mode.switch_to_power_mode, not here, so there is exactly one definition of the
    # required phrase.
    confirmation_text: str


class FirstRunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged: bool
