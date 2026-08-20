from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from reclaim.executor import PreflightSkipReason, QuarantineMethod
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
    # P0-5 treemap follow-up: the synthetic, non-deletable bucket `build_treemap` appends for
    # `ScanIndex.inaccessible_summary` -- never emitted by any detector, so it can never collide
    # with a real category_group a candidate might carry.
    "inaccessible": "Inaccessible (permission denied)",
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
    # P0-2 (2026-08 audit): added so the new Settings tab's category descriptions
    # (`api.service.settings_categories`) have a real, non-fallback entry for every category
    # `CategoriesConfig` defines, not just the ones the one-click-clean summary already covered.
    "model_caches": (
        "ML model weight caches",
        "Vaulted for 30 days before permanent delete — a gated/private/fine-tuned model may "
        "not be re-downloadable at all once original access has lapsed.",
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

    status: str  # "idle" | "running" | "completed" | "failed" | "cancelled" -- see
    # `reclaim.api.state.ScanStatusLiteral`. "cancelled" (scan cancellation, `POST
    # /api/scan/cancel`) is a user-requested stop, never an error -- `error` stays `None`.
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
    # full-drive-scan-eta: live progress/ETA fields, optional and additive -- every field above
    # this line is the original, 100% backward-compatible `POST /api/scan` contract, unchanged.
    # Populated for both the existing single-path scan (`drives_total=1`) and the new full-drive
    # scan (`POST /api/scan/full-drive`, `drives_total=len(list_fixed_drives())`) -- see
    # `api.service.run_scan`, the one orchestration path underneath both.
    phase: str | None
    entries_processed: int | None
    entries_estimated_total: int | None
    eta_seconds: float | None
    current_drive: str | None
    drives_total: int | None
    drives_done: int | None


class FixedDrivesResponse(BaseModel):
    """`GET /api/scan/fixed-drives` -- every locally-attached fixed drive on this machine
    (`reclaim.drives.list_fixed_drives`), so a SIMPLE-mode "scan my whole computer" UI can show
    what's about to be scanned before the user commits, without its own drive-enumeration logic
    (full-drive-scan-eta)."""

    model_config = ConfigDict(extra="forbid")

    drives: list[str]


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
    # P0-5: persisted (index-wide, survives an app restart -- unlike `skipped_unreadable_*`
    # above, which is this process session's most-recent-scan snapshot only) accounting of
    # directories/entries the scanner could not fully account for -- see
    # `reclaim.index.InaccessibleSummary`. `inaccessible_known_bytes` is a best-effort estimate,
    # never a claim of completeness; `inaccessible_unknown_count` is how many of
    # `inaccessible_path_count` have no size estimate at all.
    inaccessible_path_count: int
    inaccessible_known_bytes: int
    inaccessible_unknown_count: int
    # Volume-level reconciliation (`reclaim.reconciliation.compute_disk_reconciliation`) is only
    # meaningful when the most recently completed scan covered a WHOLE drive -- `None` for every
    # other case (a subtree scan, a multi-drive full-drive scan, or no completed scan yet at
    # all), never a fabricated number for a scan scope this can't honestly evaluate.
    reconciliation_volume: str | None
    reconciliation_delta_bytes: int | None
    reconciliation_delta_pct: float | None


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
    # P0-5 treemap follow-up: `True` only for the single synthetic `inaccessible` bucket node
    # `build_treemap` appends -- never a real scanned path, never selectable/deletable (always
    # paired with `is_candidate=False`). `explanation` is a one-line, non-empty reason string
    # for that same node (`None` for every ordinary node) so the treemap itself -- not just the
    # `/api/summary` banner -- says WHY this bucket's size is a best-effort estimate rather than
    # an exact figure.
    is_inaccessible: bool = False
    explanation: str | None = None


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
    # Audit P0-1 (docs/AUDIT-2026-08.md), API-boundary follow-up: `None` for every existing
    # outcome (a genuine success, or a failure where `error` carries the real exception message
    # from an ATTEMPTED mutation) -- set only when `apply_batch`'s pre-flight probe skipped this
    # item WITHOUT ever attempting the move/delete. Mirrors `executor.ItemApplyResult.skip_reason`
    # exactly; without this field a caller of `POST /api/apply` sees `succeeded=False, error=None`
    # for a locked file and a hardlink-shared file with no way to tell them apart -- the reason
    # was previously only visible in structlog, never in the response body.
    skip_reason: PreflightSkipReason | None = None
    # ADR-0032: mirrors `executor.ItemApplyResult.synchronously_purged` exactly -- `True` only
    # for a guard-downgraded, rebuildable, retention_days=0 candidate whose vault copy was ALSO
    # purged back out, synchronously, within this same apply call. See that field's docstring.
    synchronously_purged: bool = False
    # K2a follow-up (API-boundary gap, same shape as the skip_reason follow-up above): mirrors
    # `executor.ItemApplyResult.postcondition_verification_failed` exactly -- `True` only when
    # apply_batch's real-filesystem post-condition check caught the OS silently no-op'ing a
    # mutation it reported as successful (e.g. the K2b junction/reparse-point rmtree case).
    # `error` already carries a human-readable message for this case, but without this field a
    # caller of `POST /api/apply` can only detect "silent no-op" by string-matching `error`
    # rather than checking a structured field. Always `False` when `succeeded` is `True`.
    postcondition_verification_failed: bool = False


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
    # ADR-0032: mirrors `executor.BatchApplyReport.synchronously_purged_count`/`bytes_
    # synchronously_purged` exactly.
    synchronously_purged_count: int = 0
    bytes_synchronously_purged: int = 0


class ApplyStatusOut(BaseModel):
    """`GET /api/apply/status`'s body (fix/apply-progress-feedback) -- mirrors `ScanStatusOut`'s
    shape, plus a nested `result` (the same `ApplyResponse` shape `POST /api/apply` used to
    return synchronously, before that endpoint became a background-task + polling pattern)
    populated only once `status == "completed"`."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "idle" | "running" | "completed" | "failed"
    items_processed: int | None
    items_total: int | None
    current_category: str | None
    started_at: float | None
    finished_at: float | None
    error: str | None
    result: ApplyResponse | None = None


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


class RecoveryItemOut(BaseModel):
    """One crash-orphaned intent (ADR-0026), classified against real on-disk state."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    batch_id: str
    original_path: str
    outcome: str
    detail: str


class RecoveryStatusResponse(BaseModel):
    """Read-only preview (`reclaim.recovery.compute_reconciliation`) — never writes anything;
    a genuine reconcile still requires `reclaim recover --apply` from the CLI. `needs_review`
    items are the ones worth a banner; `completed`/`aborted` items are informational (they'll
    resolve themselves the next time anything runs `reclaim recover --apply`)."""

    model_config = ConfigDict(extra="forbid")

    scanned_intents: int
    already_resolved: int
    pending: list[RecoveryItemOut]
    has_needs_review: bool


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


class RestoreStatusOut(BaseModel):
    """`GET /api/restore/status`'s body (fix/apply-progress-feedback) -- mirrors `ApplyStatusOut`
    exactly, one level down: `POST /api/restore/{batch_id}` became the same background-task +
    polling pattern, `result` is the same `RestoreResponse` shape that endpoint used to return
    synchronously."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "idle" | "running" | "completed" | "failed"
    items_processed: int | None
    items_total: int | None
    current_category: str | None
    started_at: float | None
    finished_at: float | None
    error: str | None
    result: RestoreResponse | None = None


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


# --- P0-2 fix (2026-08 audit): in-app category settings -----------------------------------------


class CategorySettingOut(BaseModel):
    """One cleanup category's current enable state, for the in-app Settings tab. `enabled`
    reflects `config.toml` (or the built-in default) exactly as written -- never the SAFE-mode-
    resolved value -- so toggling a category off in POWER mode and switching back to SAFE doesn't
    look like the toggle silently reverted; `forced_off_in_safe_mode` is the separate, honest
    signal for "this category is on, but has no effect until you switch to power mode"."""

    model_config = ConfigDict(extra="forbid")

    category_group: str
    category_label: str
    description: str
    enabled: bool
    forced_off_in_safe_mode: bool


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[CategorySettingOut]


class UpdateCategorySettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


# --- Update check (opt-in; see PRIVACY.md's "Updates" section and reclaim.update_check) --------


class UpdateCheckResponse(BaseModel):
    """`GET /api/update-check`'s response. `enabled=False` means the feature is off in
    config.toml — `reclaim.update_check.check_for_update` was never called, so no network
    request happened for this response at all (see `api.service.check_for_update_status`).
    `status="unknown"` means the feature is on but the check itself couldn't get a usable
    answer (offline, GitHub down, malformed response, etc.) — a normal, expected outcome that
    must render as "couldn't check right now," never as an error."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    status: str  # "disabled" | "ok" | "unknown"
    current_version: str
    latest_version: str | None
    update_available: bool
    release_url: str


# --- G25: bug-report diagnostics ----------------------------------------------------------------


class DiagnosticsResponse(BaseModel):
    """Everything the dashboard's "Copy diagnostics" button hands the user for a bug report.

    PRIVACY (non-negotiable, see PRIVACY.md): every field here is a path, a version string, a
    mode name, or a tail of the structured log file itself — never file content or OCR'd text.
    `log_tail` is safe by construction, not by filtering: every `logger.*(...)` call site in
    this codebase only ever passes paths/counts/error strings as structured fields (see
    `reclaim.logging_config`'s module docstring and PRIVACY.md), so nothing the log file could
    contain violates this guarantee in the first place.
    """

    model_config = ConfigDict(extra="forbid")

    reclaim_version: str
    mode: Mode
    ai_extra_installed: bool
    os_version: str
    log_path: str
    log_tail: str


# --- R2: Anthropic API key settings + per-category LLM explanations ----------------------------
#
# PRIVACY (non-negotiable, matching DiagnosticsResponse's own posture above): no response shape
# in this section ever carries the plaintext API key — `AnthropicKeyStatusResponse` reports only
# whether a key is configured, never the key itself, and `DiagnosticsResponse` above must never
# gain a field from this module either (see reclaim.anthropic_key_store's module docstring).


class AnthropicKeyStatusResponse(BaseModel):
    """`GET /api/settings/anthropic-key`'s response, and the shape every mutating endpoint in
    this section returns after acting — reports presence only, never the key itself."""

    model_config = ConfigDict(extra="forbid")

    configured: bool


class SetAnthropicKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str


class TestAnthropicKeyRequest(BaseModel):
    """`api_key` is optional — omit it to test the already-stored key (e.g. re-validating after
    it may have been revoked on Anthropic's side); provide it to test a candidate key BEFORE
    saving it, which is the primary "Test key" button flow."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None


class TestAnthropicKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    message: str


class CategoryExplanationResponse(BaseModel):
    """`GET /api/ai/category-explanation/{category_group}`'s response. `status="unavailable"`
    covers both "no scan data for this category" and "no API key configured" — both are normal,
    expected, non-error states (mirrors `AISuggestionsResponse`'s own "unavailable" convention);
    `status="error"` covers a real Anthropic API failure (network/auth/malformed response) —
    `message` is always a safe, pre-sanitized string (never a raw exception repr that could leak
    request internals), and the API key itself never appears in any field here."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "ok" | "unavailable" | "error"
    category_group: str
    message: str | None  # unavailable_reason / error message; None only when status == "ok"
    explanation: str | None
    cached: bool
