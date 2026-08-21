from __future__ import annotations

import platform
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace as _dataclass_replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import structlog

from reclaim import update_check
from reclaim.ai import presentation
from reclaim.ai.models import AICluster
from reclaim.api import ai_orchestration
from reclaim.api.schemas import (
    AIAnalysisStatusOut,
    AIClusterMemberOut,
    AISuggestionOut,
    AISuggestionsResponse,
    AITrackSkipOut,
    ApplyRequest,
    ApplyResponse,
    ApplyStatusOut,
    CandidateOut,
    CandidatesResponse,
    CategoryBreakdownOut,
    CategoryCardOut,
    CategorySettingOut,
    DiagnosticsResponse,
    DuplicateClusterOut,
    DuplicateClusterReviewOut,
    DuplicateClusterReviewResponse,
    DuplicateMemberOut,
    FirstRunStatusResponse,
    FixedDrivesResponse,
    ItemApplyResultOut,
    ModeStatusResponse,
    OneClickCleanSummaryResponse,
    OneClickGroupOut,
    PowerModeRequest,
    QuarantineBatchOut,
    QuarantineItemOut,
    QuarantineListResponse,
    RecoveryItemOut,
    RecoveryStatusResponse,
    RestoreItemOut,
    RestoreResponse,
    RestoreStatusOut,
    ScanStatusOut,
    SettingsResponse,
    SuggestedScanRootOut,
    SuggestedScanRootsResponse,
    SummaryResponse,
    TreemapNodeOut,
    TreemapResponse,
    UpdateCheckResponse,
    category_label,
    format_bytes,
    plain_language_category,
)
from reclaim.api.state import AIAnalysisStatus, ApplyStatus, AppState, RestoreStatus, ScanStatus
from reclaim.config import CategoriesConfig, set_category_enabled
from reclaim.dedup import (
    cluster_needs_manual_review,
    find_duplicate_clusters,
    generate_duplicate_candidates,
)
from reclaim.detectors import generate_candidates
from reclaim.drives import list_fixed_drives
from reclaim.executor import (
    BatchApplyReport,
    QuarantineManifestEntry,
    QuarantineMethod,
    RestoreReport,
    SafeModeViolationError,
    apply_batch,
    read_manifest_entries,
    resolve_restorable_entries,
    restore_batch,
)
from reclaim.first_run import acknowledge as acknowledge_first_run
from reclaim.first_run import is_acknowledged as first_run_is_acknowledged
from reclaim.index import InaccessibleSummary, ScanIndex, physical_size_bytes
from reclaim.mode import (
    REQUIRED_POWER_MODE_CONFIRMATION,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import (
    SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS,
    Candidate,
    DuplicateCluster,
    FileRecord,
    Mode,
    Tier,
    Verdict,
)
from reclaim.reconciliation import NotAVolumeRootError, compute_disk_reconciliation, is_volume_root
from reclaim.recovery import compute_reconciliation
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path, count_entries_fast, scan_tree

logger = structlog.get_logger(__name__)

_TIER_SELECTIONS: dict[str, frozenset[Tier]] = {
    "A": frozenset({Tier.A}),
    "B": frozenset({Tier.B}),
    "both": frozenset({Tier.A, Tier.B}),
}


def _all_candidates(index: ScanIndex, state: AppState) -> list[Candidate]:
    """Combined detector + exact-duplicate candidate list — the same two-function contract
    `cli.py::_run_apply` already uses, just orchestrated for the API layer instead.

    Uses `state.effective_config` (mode-resolved fresh on every call), never `state.config`
    directly — see `AppState.effective_config`'s docstring.

    UNCACHED — this is the expensive pass `_cached_all_candidates` below memoizes per scan
    generation. Call sites that need every candidate for the CURRENT scan should go through that
    wrapper instead; call this directly only when scoping to a smaller universe already avoids
    the cost (see `resolve_apply_selection`'s `request.paths is not None` branch)."""
    config = state.effective_config
    candidates = generate_candidates(index, config, state.safety)
    candidates += generate_duplicate_candidates(index, config, state.safety)
    return candidates


def _cached_all_candidates(index: ScanIndex, state: AppState) -> list[Candidate]:
    """Cached, concurrency-guarded wrapper around `_all_candidates` (perf/dedup-cache,
    docs/AUDIT-2026-08.md P0-3) — every call site that needs the full candidate universe for the
    CURRENT scan (`build_summary`, `build_treemap`, `list_candidates`,
    `build_one_click_summary`, and `resolve_apply_selection`'s blanket-apply path) should call
    this instead of `_all_candidates` directly.

    `state.candidates_cache_lock` is held across the ENTIRE compute-or-fetch critical section,
    deliberately — a second caller racing the first for the same `scan_generation` blocks on the
    lock rather than starting its own redundant whole-index BLAKE3 hash pass, and sees the first
    caller's now-cached result the moment it acquires the lock. See `AppState.candidates_cache`'s
    docstring for why this is a dedicated lock rather than `state.lock`.
    """
    with state.candidates_cache_lock:
        generation = state.scan_generation
        if state.candidates_cache is not None and state.candidates_cache_generation == generation:
            return state.candidates_cache
        candidates = _all_candidates(index, state)
        state.candidates_cache = candidates
        state.candidates_cache_generation = generation
        return candidates


# --- Scan --------------------------------------------------------------------------------


def suggested_scan_roots(*, home: Path | None = None) -> SuggestedScanRootsResponse:
    """Server-resolved default scan-root suggestions (Downloads, home folder) for the dashboard's
    quick-pick scan buttons — non-technical users can't be expected to type a path, so this
    fills in the common cases while the free-text `#scan-path` input stays available for
    advanced use. `home` is injectable for tests; production callers always resolve it fresh
    (never cached) since this reads real filesystem state, not app state.

    Only ever suggests a folder that demonstrably exists on THIS machine right now — a
    suggestion whose folder doesn't exist is omitted entirely (not shown disabled), since a
    profile with no Downloads folder has nothing useful to click there anyway."""
    resolved_home = home if home is not None else Path.home()
    candidates = (
        ("Downloads", resolved_home / "Downloads"),
        ("Home folder", resolved_home),
    )
    roots = [
        SuggestedScanRootOut(label=label, path=path.as_posix())
        for label, path in candidates
        if path.is_dir()
    ]
    return SuggestedScanRootsResponse(roots=roots)


def fixed_drives() -> FixedDrivesResponse:
    """Backs `GET /api/scan/fixed-drives` -- every locally-attached fixed drive on this machine,
    so a SIMPLE-mode "scan my whole computer" UI can show what's about to be scanned before the
    user commits (full-drive-scan-eta). Propagates `reclaim.drives.NoFixedDrivesFoundError`
    straight through -- the route layer converts it to a 500, same posture `SafeModeViolationError`
    gets in `apply`."""
    return FixedDrivesResponse(drives=[d.as_posix() for d in list_fixed_drives()])


def fixed_drive_roots() -> list[Path]:
    """Same enumeration as `fixed_drives()`, as raw `Path` objects for `run_scan`'s `roots`
    parameter rather than the API response's string shape. Kept as its own thin function (rather
    than having `POST /api/scan/full-drive` reuse `fixed_drives()` and re-parse the response)
    so a test can monkeypatch just `reclaim.drives.list_fixed_drives` and have both this and
    `fixed_drives()` pick up the fixture roots identically."""
    return list_fixed_drives()


def to_scan_status_out(status: ScanStatus) -> ScanStatusOut:
    return ScanStatusOut(
        status=status.status,
        root=status.root.as_posix() if status.root is not None else None,
        started_at=status.started_at,
        finished_at=status.finished_at,
        error=status.error,
        dirs_visited=status.dirs_visited,
        entries_total=status.entries_total,
        files_written=status.files_written,
        files_unchanged=status.files_unchanged,
        files_pruned=status.files_pruned,
        elapsed_seconds=status.elapsed_seconds,
        skipped_unreadable_count=status.skipped_unreadable_count,
        skipped_unreadable_paths=(
            list(status.skipped_unreadable_paths)
            if status.skipped_unreadable_paths is not None
            else None
        ),
        phase=status.phase,
        entries_processed=status.entries_processed,
        entries_estimated_total=status.entries_estimated_total,
        eta_seconds=status.eta_seconds,
        current_drive=status.current_drive,
        drives_total=status.drives_total,
        drives_done=status.drives_done,
    )


# A real rate below this (entries/second) is close enough to zero that trusting it for a
# division would produce a wild, misleading ETA rather than a conservative one -- `None` (no
# estimate yet) is the honest answer at that point, not a number.
_ETA_MIN_RATE_ENTRIES_PER_SECOND = 1e-9


def _compute_eta_seconds(
    entries_processed: int, entries_estimated_total: int | None, elapsed_seconds: float
) -> float | None:
    """Pure ETA-computing function behind `run_scan`'s live `on_progress` callbacks -- extracted
    standalone (matching this codebase's own `_due`-style "pure predicate, cheaply testable"
    convention) rather than inlined in the callback closures.

    `None` whenever an honest estimate isn't yet possible: no `entries_estimated_total` (the
    "estimating" phase hasn't finished yet, or a caller genuinely didn't supply one),
    non-positive `entries_processed`/`elapsed_seconds` (the very first progress tick -- dividing
    by an unstable near-zero rate would produce a wild number, not a conservative one), or a rate
    too close to zero to trust (see `_ETA_MIN_RATE_ENTRIES_PER_SECOND`).

    `entries_estimated_total - entries_processed` going negative (the real walk visited MORE
    than the fast estimate predicted -- a real possibility, since the two passes race against a
    live, mutating filesystem) clamps to `0.0` -- "basically done", never a negative ETA.
    """
    if entries_estimated_total is None or entries_processed <= 0 or elapsed_seconds <= 0:
        return None
    rate = entries_processed / elapsed_seconds
    if rate <= _ETA_MIN_RATE_ENTRIES_PER_SECOND:
        return None
    remaining = entries_estimated_total - entries_processed
    if remaining <= 0:
        return 0.0
    return remaining / rate


# Sample-list cap for `run_scan`'s `skipped_unreadable_paths`, aggregated across every scanned
# root -- mirrors `scanner._SKIPPED_PATHS_SAMPLE_LIMIT`'s own reasoning (the COUNT is always
# exact; only the sample of actual path strings is capped) at this multi-root aggregation layer.
_AGGREGATE_SKIPPED_PATHS_SAMPLE_LIMIT = 20


def run_scan(state: AppState, roots: Sequence[Path], started_at: float) -> None:
    """Background-task body for both `POST /api/scan` (`roots=[the one path]`, `drives_total=1`)
    and `POST /api/scan/full-drive` (`roots=list_fixed_drives()`) -- ONE orchestration path
    underneath both (full-drive-scan-eta), so the two-phase estimate+scan flow and its live ETA
    only ever have one place to be correct. Runs on Starlette's worker-thread pool (sync
    callables passed to `BackgroundTasks.add_task` are dispatched via `run_in_threadpool`), so
    this never blocks the event loop; `state.lock` guards every read/write of `scan_status`
    against a concurrent `GET /api/scan/status` poll from the request-handling thread(s).

    Two phases per root, sequential across roots -- drives are scanned one at a time, never in
    parallel: a single drive's `scan_tree` already fans out across a `ThreadPoolExecutor` of its
    own, so running several drives' worker pools concurrently would only contend with each other
    for no real throughput gain, and keeps this orchestration layer's own progress accounting
    trivially single-threaded.

    (a) "estimating" -- `scanner.count_entries_fast` walks `root` once, stat-free, to derive
        `entries_estimated_total`; its own interval-gated progress callback republishes onto
        `scan_status.entries_processed` so this phase is never silent either, even on a drive
        large enough for the count itself to take several seconds.
    (b) "scanning" -- the real `scanner.scan_tree`, whose `on_progress` callback computes a live
        `eta_seconds` (`_compute_eta_seconds`) from `entries_processed`/`entries_estimated_total`/
        elapsed time and republishes it onto `scan_status` for every `GET /api/scan/status`
        poller.

    `scan_status.root`/`current_drive` mirror whichever root is CURRENTLY active while running,
    so `app.js`'s existing "Scanning {root}…" text stays meaningful for a full-drive scan too,
    with zero frontend changes needed. On completion, `root` is the single scanned path for a
    one-root scan (`drives_total == 1` -- true for both an ordinary single-path scan and a
    full-drive scan that happens to find exactly one fixed drive), or `None` for a genuine
    multi-drive scan -- no single root would be honest to report there, and `build_treemap`
    already treats `root=None` as "has data, nothing to enumerate a subtree of" gracefully (the
    same fallback a fresh-process restart with stale persisted data already exercises).

    `dirs_visited`/`entries_total`/`files_written`/`files_unchanged`/`files_pruned`/
    `elapsed_seconds`/`skipped_unreadable_count`/`skipped_unreadable_paths` are SUMMED across
    every scanned root -- the pre-full-drive-scan single-path contract's exact per-scan meaning
    is preserved when `drives_total == 1` (a sum of one term is that term).

    A failure on any one root aborts the WHOLE scan immediately (`status="failed"`, whatever
    partial aggregate stats accumulated so far are kept, not discarded) -- a real
    `scan_tree`/`count_entries_fast` failure partway through a multi-drive full scan means the
    remaining drives' data would be incomplete anyway, so continuing past it would silently
    under-report free-space math for a scan that looks like it "completed".

    Scan cancellation: `state.cancel_scan_event` (cleared by the route handler before this task
    is ever scheduled -- see `AppState.cancel_scan_event`'s docstring) is threaded into every
    `count_entries_fast`/`scan_tree` call below. A cancellation observed during "estimating"
    stops before that root's real scan phase ever starts; a cancellation observed during
    "scanning" is reported by `scan_tree` itself via `ScanStats.cancelled`. Either way, this
    function stops processing any REMAINING roots (unlike a genuine failure, which also stops
    immediately, a cancellation is not an error -- `status="cancelled"`, `error=None`, and
    whatever partial aggregate stats accumulated so far are kept, same "never discard partial
    progress" posture the failure branch already has).
    """
    roots = list(roots)
    drives_total = len(roots)

    dirs_visited_total = 0
    entries_total_total = 0
    files_written_total = 0
    files_unchanged_total = 0
    files_pruned_total = 0
    elapsed_seconds_total = 0.0
    skipped_unreadable_count_total = 0
    skipped_unreadable_paths_sample: list[str] = []
    cancelled = False

    try:
        state.db_path.parent.mkdir(parents=True, exist_ok=True)
        with ScanIndex(state.db_path) as index:
            for drive_index, root in enumerate(roots):
                with state.lock:
                    state.scan_status = _dataclass_replace(
                        state.scan_status,
                        status="running",
                        root=root,
                        started_at=started_at,
                        finished_at=None,
                        error=None,
                        phase="estimating",
                        current_drive=root.as_posix(),
                        drives_total=drives_total,
                        drives_done=drive_index,
                        entries_processed=0,
                        entries_estimated_total=None,
                        eta_seconds=None,
                    )

                def on_count_progress(counted: int, elapsed: float) -> None:
                    with state.lock:
                        state.scan_status = _dataclass_replace(
                            state.scan_status, entries_processed=counted
                        )

                entries_estimated_total = count_entries_fast(
                    root, on_progress=on_count_progress, cancel_event=state.cancel_scan_event
                )

                if state.cancel_scan_event.is_set():
                    cancelled = True
                    break

                with state.lock:
                    state.scan_status = _dataclass_replace(
                        state.scan_status,
                        phase="scanning",
                        entries_processed=0,
                        entries_estimated_total=entries_estimated_total,
                    )

                def on_scan_progress(
                    processed: int, estimated_total: int | None, elapsed: float
                ) -> None:
                    eta = _compute_eta_seconds(processed, estimated_total, elapsed)
                    with state.lock:
                        state.scan_status = _dataclass_replace(
                            state.scan_status, entries_processed=processed, eta_seconds=eta
                        )

                stats = scan_tree(
                    root,
                    index,
                    incremental=True,
                    on_progress=on_scan_progress,
                    entries_estimated_total=entries_estimated_total,
                    cancel_event=state.cancel_scan_event,
                )

                dirs_visited_total += stats.dirs_visited
                entries_total_total += stats.entries_total
                files_written_total += stats.files_written
                files_unchanged_total += stats.files_unchanged
                files_pruned_total += stats.files_pruned
                elapsed_seconds_total += stats.elapsed_seconds
                skipped_unreadable_count_total += stats.skipped_unreadable_count
                remaining_slots = _AGGREGATE_SKIPPED_PATHS_SAMPLE_LIMIT - len(
                    skipped_unreadable_paths_sample
                )
                if remaining_slots > 0:
                    skipped_unreadable_paths_sample.extend(
                        stats.skipped_unreadable_paths[:remaining_slots]
                    )

                with state.lock:
                    state.scan_status = _dataclass_replace(
                        state.scan_status, drives_done=drive_index + 1
                    )

                if stats.cancelled:
                    cancelled = True
                    break
    except Exception as exc:  # broad on purpose: a background-task exception must surface via
        # the status endpoint, never crash silently into Starlette's background-task machinery.
        logger.warning("api.scan_failed", roots=[str(r) for r in roots], error=str(exc))
        with state.lock:
            state.scan_status = ScanStatus(
                status="failed",
                root=state.scan_status.root,
                started_at=started_at,
                finished_at=time.time(),
                error=str(exc),
                dirs_visited=dirs_visited_total,
                entries_total=entries_total_total,
                files_written=files_written_total,
                files_unchanged=files_unchanged_total,
                files_pruned=files_pruned_total,
                elapsed_seconds=elapsed_seconds_total,
                skipped_unreadable_count=skipped_unreadable_count_total,
                skipped_unreadable_paths=tuple(skipped_unreadable_paths_sample),
                phase=state.scan_status.phase,
                current_drive=state.scan_status.current_drive,
                drives_total=drives_total,
                drives_done=state.scan_status.drives_done,
            )
        return

    if cancelled:
        with state.lock:
            state.scan_status = ScanStatus(
                status="cancelled",
                root=state.scan_status.root,
                started_at=started_at,
                finished_at=time.time(),
                error=None,
                dirs_visited=dirs_visited_total,
                entries_total=entries_total_total,
                files_written=files_written_total,
                files_unchanged=files_unchanged_total,
                files_pruned=files_pruned_total,
                elapsed_seconds=elapsed_seconds_total,
                skipped_unreadable_count=skipped_unreadable_count_total,
                skipped_unreadable_paths=tuple(skipped_unreadable_paths_sample),
                phase=state.scan_status.phase,
                current_drive=None,
                drives_total=drives_total,
                drives_done=state.scan_status.drives_done,
            )
            # A cancelled run can still have written real records to the index (every batch
            # flushed before the stop point is durable -- see scanner.scan_tree's cancel_event
            # docstring), so any cached AI analysis must be treated as stale exactly like a
            # normal completion -- see ADR-0025.
            state.scan_generation += 1
        return

    with state.lock:
        state.scan_status = ScanStatus(
            status="completed",
            root=roots[0] if drives_total == 1 else None,
            started_at=started_at,
            finished_at=time.time(),
            dirs_visited=dirs_visited_total,
            entries_total=entries_total_total,
            files_written=files_written_total,
            files_unchanged=files_unchanged_total,
            files_pruned=files_pruned_total,
            elapsed_seconds=elapsed_seconds_total,
            skipped_unreadable_count=skipped_unreadable_count_total,
            skipped_unreadable_paths=tuple(skipped_unreadable_paths_sample),
            phase="done",
            entries_processed=entries_total_total,
            entries_estimated_total=entries_total_total,
            eta_seconds=0.0,
            current_drive=None,
            drives_total=drives_total,
            drives_done=drives_total,
        )
        # ADR-0025: a new completed scan invalidates any cached AI analysis -- callers compare
        # this against `AIAnalysisStatus.scan_generation` to detect a stale cache.
        state.scan_generation += 1


# --- Summary / category cards -------------------------------------------------------------


def _effective_reclaimable_bytes(candidate: Candidate) -> int:
    """The real byte count a user gets back if `candidate` is deleted -- hardlink-aware
    `reclaimable_bytes` (ADR-0006, populated today only for `exact_duplicate`) when set, else
    the naive logical `size_bytes` (every other category, where the two are equal in practice
    since only `duplicates` can share blocks with a surviving copy). Never sum `size_bytes`
    directly over a category that can contain hardlinked duplicates -- see
    docs/AUDIT-2026-08.md's P1 finding this fixes. Same pattern `list_duplicate_cluster_review`
    below already used correctly one tab over; every top-line byte total in this module that can
    include the `duplicates` category now goes through this one function instead of each
    re-deriving the same ternary."""
    if candidate.reclaimable_bytes is not None:
        return candidate.reclaimable_bytes
    return candidate.size_bytes


def _category_cards(candidates: Sequence[Candidate]) -> list[CategoryCardOut]:
    grouped: dict[tuple[str, Tier], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.category_group, candidate.tier)].append(candidate)

    cards = []
    for (group, tier), items in grouped.items():
        total_bytes = sum(_effective_reclaimable_bytes(item) for item in items)
        cards.append(
            CategoryCardOut(
                category_group=group,
                category_label=category_label(group),
                tier=tier,
                file_count=len(items),
                total_bytes=total_bytes,
                total_bytes_human=format_bytes(total_bytes),
            )
        )
    cards.sort(key=lambda c: c.total_bytes, reverse=True)
    return cards


def _reconciliation_fields(
    index: ScanIndex, state: AppState
) -> tuple[str | None, int | None, float | None]:
    """`(volume, delta_bytes, delta_pct)` for `SummaryResponse`'s volume-level reconciliation
    (P0-5) -- `(None, None, None)` whenever the most recently completed scan wasn't a genuine
    whole-single-drive scan, since `compute_disk_reconciliation` has no way to distinguish a
    real inaccessible-directory undercount from "this index just never covered the whole
    volume", and this function never guesses. `OSError` (a real `shutil.disk_usage` failure --
    e.g. the volume was unmounted since the scan) is likewise treated as "not available" rather
    than surfaced as a 500 on an otherwise-working summary endpoint.
    """
    with state.lock:
        root = state.scan_status.root
        drives_total = state.scan_status.drives_total
        status = state.scan_status.status
    if status != "completed" or root is None or drives_total != 1 or not is_volume_root(root):
        return None, None, None
    try:
        report = compute_disk_reconciliation(index, root)
    except (NotAVolumeRootError, OSError) as exc:
        logger.warning("api.reconciliation_unavailable", volume=str(root), error=str(exc))
        return None, None, None
    return report.volume, report.delta_bytes, report.delta_pct


def build_summary(state: AppState) -> SummaryResponse:
    # D12: the most recent COMPLETED scan's skipped/unreadable accounting -- in-memory,
    # process-session state like `scan_status` itself (read under `state.lock`, same pattern
    # `build_treemap` uses for `scan_status.root`), never persisted to the index (it isn't a
    # `FileRecord`, there's nothing on disk to store).
    with state.lock:
        skipped_unreadable_count = state.scan_status.skipped_unreadable_count or 0
        skipped_unreadable_paths = list(state.scan_status.skipped_unreadable_paths or ())

    with ScanIndex(state.db_path) as index:
        # P0-5: persisted (index-wide, survives an app restart) accounting -- unlike
        # `skipped_unreadable_*` above, computed even when `has_any_records()` is False (a scan
        # whose very root was itself unreadable can produce an inaccessible-path row with zero
        # real `files` rows to show for it).
        inaccessible = index.inaccessible_summary()
        reconciliation_volume, reconciliation_delta_bytes, reconciliation_delta_pct = (
            _reconciliation_fields(index, state)
        )

        if not index.has_any_records():
            return SummaryResponse(
                has_scan=False,
                total_indexed_bytes=0,
                total_indexed_human=format_bytes(0),
                tier_a_bytes=0,
                tier_a_count=0,
                tier_b_bytes=0,
                tier_b_count=0,
                categories=[],
                skipped_unreadable_count=skipped_unreadable_count,
                skipped_unreadable_paths=skipped_unreadable_paths,
                inaccessible_path_count=inaccessible.path_count,
                inaccessible_known_bytes=inaccessible.known_bytes,
                inaccessible_unknown_count=inaccessible.unknown_count,
                reconciliation_volume=reconciliation_volume,
                reconciliation_delta_bytes=reconciliation_delta_bytes,
                reconciliation_delta_pct=reconciliation_delta_pct,
            )
        total_indexed_bytes = physical_size_bytes(index.full_inventory())
        candidates = _cached_all_candidates(index, state)

    tier_a = [c for c in candidates if c.tier == Tier.A]
    tier_b = [c for c in candidates if c.tier == Tier.B]
    return SummaryResponse(
        has_scan=True,
        total_indexed_bytes=total_indexed_bytes,
        total_indexed_human=format_bytes(total_indexed_bytes),
        tier_a_bytes=sum(_effective_reclaimable_bytes(c) for c in tier_a),
        tier_a_count=len(tier_a),
        tier_b_bytes=sum(_effective_reclaimable_bytes(c) for c in tier_b),
        tier_b_count=len(tier_b),
        categories=_category_cards(candidates),
        skipped_unreadable_count=skipped_unreadable_count,
        skipped_unreadable_paths=skipped_unreadable_paths,
        inaccessible_path_count=inaccessible.path_count,
        inaccessible_known_bytes=inaccessible.known_bytes,
        inaccessible_unknown_count=inaccessible.unknown_count,
        reconciliation_volume=reconciliation_volume,
        reconciliation_delta_bytes=reconciliation_delta_bytes,
        reconciliation_delta_pct=reconciliation_delta_pct,
    )


# --- Treemap -------------------------------------------------------------------------------

# P0-5 treemap follow-up: the synthetic category_group `_inaccessible_treemap_node` emits --
# see `schemas.category_label`'s "inaccessible" entry and `TreemapNodeOut.is_inaccessible`'s
# docstring for why this can never collide with a real detector's category_group.
_INACCESSIBLE_CATEGORY_GROUP = "inaccessible"


def _inaccessible_explanation(summary: InaccessibleSummary) -> str:
    """One-line, human-readable reason string for the synthetic inaccessible-bucket treemap
    node -- rendered by `treemap.js`'s tooltip so the WHY is visible in the treemap itself, not
    only in the separate `/api/summary` banner (`app.js::renderInaccessibleNote`). Mirrors that
    function's own wording (same "best-effort estimate, not a claim of completeness" framing)
    rather than inventing a second copy voice for the same underlying fact."""
    text = (
        f"{summary.path_count} path(s) could not be read due to permissions or a real I/O "
        "error -- size shown is a best-effort estimate, not an exact figure."
    )
    if summary.unknown_count:
        text += (
            f" {summary.unknown_count} of those have no size estimate at all, so the true "
            "total is larger than the bytes shown here."
        )
    return text


def _inaccessible_treemap_node(summary: InaccessibleSummary) -> TreemapNodeOut:
    """The single synthetic node representing `ScanIndex.inaccessible_summary`'s bucket inside
    the treemap itself (P0-5 follow-up) -- `is_candidate=False`/`is_inaccessible=True` mark it
    as informational-only so nothing that later grows a "click a node to select it" flow can
    ever mistake this for a real, actionable path: the underlying paths are, by definition,
    ones Reclaim could not read, so there is nothing real behind this node to select or delete.
    `path` is a synthetic marker (never a real filesystem path) for the same reason."""
    return TreemapNodeOut(
        path="__inaccessible__",
        label="Inaccessible / unreadable",
        size_bytes=summary.known_bytes,
        size_human=format_bytes(summary.known_bytes),
        category_group=_INACCESSIBLE_CATEGORY_GROUP,
        category_label=category_label(_INACCESSIBLE_CATEGORY_GROUP),
        is_dir=False,
        is_candidate=False,
        is_inaccessible=True,
        explanation=_inaccessible_explanation(summary),
    )


def build_treemap(state: AppState, *, max_nodes: int = 60) -> TreemapResponse:
    with ScanIndex(state.db_path) as index:
        if not index.has_any_records():
            return TreemapResponse(
                has_scan=False,
                root=None,
                total_bytes=0,
                total_bytes_human=format_bytes(0),
                nodes=[],
            )

        with state.lock:
            root = state.scan_status.root

        if root is None:
            # Persisted data exists (from a prior process's scan) but this process's in-memory
            # session never recorded a root (fresh restart) — nothing safe to enumerate one
            # level under without guessing, so report real data presence with an empty node
            # list rather than fabricating a root. See AppState's docstring on the in-memory
            # session-state simplification this follows from.
            return TreemapResponse(
                has_scan=True, root=None, total_bytes=0, total_bytes_human=format_bytes(0), nodes=[]
            )

        children = index.direct_children(root)
        candidates = _cached_all_candidates(index, state)
        candidate_by_path = {c.path: c for c in candidates}

        nodes: list[TreemapNodeOut] = []
        for child in children:
            size = index.subtree_size_bytes(child.path) if child.is_dir else child.size_bytes
            if size <= 0:
                continue
            candidate = candidate_by_path.get(child.path)
            group = candidate.category_group if candidate is not None else "other"
            nodes.append(
                TreemapNodeOut(
                    path=child.path.as_posix(),
                    label=child.path.name,
                    size_bytes=size,
                    size_human=format_bytes(size),
                    category_group=group,
                    category_label=category_label(group),
                    is_dir=child.is_dir,
                    is_candidate=candidate is not None,
                )
            )
        nodes.sort(key=lambda n: n.size_bytes, reverse=True)
        nodes = nodes[:max_nodes]

        # P0-5 treemap follow-up: the inaccessible bucket is a real, always-visible node in the
        # treemap itself (not just the `/api/summary` banner), scoped to THIS root the same way
        # `total_bytes` below is -- appended after the size-based `max_nodes` truncation above so
        # it's never silently dropped for being small relative to the biggest real directories,
        # and never counts against that cap.
        inaccessible = index.inaccessible_summary(under=root)
        if inaccessible.path_count > 0:
            nodes.append(_inaccessible_treemap_node(inaccessible))

        total_bytes = index.subtree_size_bytes(root)

    return TreemapResponse(
        has_scan=True,
        root=root.as_posix(),
        total_bytes=total_bytes,
        total_bytes_human=format_bytes(total_bytes),
        nodes=nodes,
    )


# --- Candidates / duplicate clusters --------------------------------------------------------


def _duplicate_member_out(record: FileRecord, *, is_keep: bool) -> DuplicateMemberOut:
    return DuplicateMemberOut(
        path=record.path.as_posix(),
        size_bytes=record.size_bytes,
        size_human=format_bytes(record.size_bytes),
        ctime=record.ctime,
        ctime_iso=datetime.fromtimestamp(record.ctime, tz=UTC).isoformat(),
        is_keep=is_keep,
    )


def _duplicate_cluster_out(cluster: DuplicateCluster) -> DuplicateClusterOut:
    return DuplicateClusterOut(
        full_hash=cluster.full_hash,
        members=[
            _duplicate_member_out(member, is_keep=member.path == cluster.keep.path)
            for member in cluster.members
        ],
    )


def _index_clusters_by_duplicate_path(
    clusters: Sequence[DuplicateCluster],
) -> dict[Path, DuplicateCluster]:
    """Maps every non-keep cluster member's path to its cluster — the shape `Candidate` records
    (one per non-keep duplicate) need to look up their side-by-side comparison data."""
    by_path: dict[Path, DuplicateCluster] = {}
    for cluster in clusters:
        for duplicate in cluster.duplicates:
            by_path[duplicate.path] = cluster
    return by_path


def _candidate_out(candidate: Candidate, cluster: DuplicateCluster | None) -> CandidateOut:
    return CandidateOut(
        path=candidate.path.as_posix(),
        is_dir=candidate.is_dir,
        category=candidate.category,
        category_group=candidate.category_group,
        category_label=category_label(candidate.category_group),
        size_bytes=candidate.size_bytes,
        size_human=format_bytes(candidate.size_bytes),
        tier=candidate.tier,
        rationale=candidate.rationale,
        rebuild_instruction=candidate.rebuild_instruction,
        recovery_cost_note=candidate.recovery_cost_note,
        reclaimable_bytes=candidate.reclaimable_bytes,
        safety_verdict=candidate.safety_verdict,
        safety_reason_code=candidate.safety_reason_code,
        duplicate_cluster=_duplicate_cluster_out(cluster) if cluster is not None else None,
    )


def list_candidates(
    state: AppState, *, tier: str, category_group: str | None
) -> CandidatesResponse:
    with ScanIndex(state.db_path) as index:
        if not index.has_any_records():
            return CandidatesResponse(
                has_scan=False,
                candidates=[],
                count=0,
                total_bytes=0,
                total_bytes_human=format_bytes(0),
            )

        candidates = _cached_all_candidates(index, state)
        needs_cluster_info = category_group in (None, "duplicates") and any(
            c.category_group == "duplicates" for c in candidates
        )
        cluster_by_path = (
            _index_clusters_by_duplicate_path(
                find_duplicate_clusters(
                    index, min_reclaim_bytes=state.config.categories.duplicates.min_reclaim_bytes
                )
            )
            if needs_cluster_info
            else {}
        )

    tiers = _TIER_SELECTIONS[tier]
    filtered = [c for c in candidates if c.tier in tiers]
    if category_group is not None:
        filtered = [c for c in filtered if c.category_group == category_group]

    out = [_candidate_out(c, cluster_by_path.get(c.path)) for c in filtered]
    total_bytes = sum(_effective_reclaimable_bytes(c) for c in filtered)
    return CandidatesResponse(
        has_scan=True,
        candidates=out,
        count=len(out),
        total_bytes=total_bytes,
        total_bytes_human=format_bytes(total_bytes),
    )


# One-click clean is scoped to categorically-safe groups ONLY — safe by the rebuild-command
# definition ADR-0005's `REBUILDABLE_CATEGORY_GROUPS` already establishes for three of these
# four, plus `crash_dumps` (never useful once the crash they document is resolved). Deliberately
# excludes `duplicates` (keeps exactly one copy by design — which copy needs eyeballing, not a
# one-click default), `model_caches` (large, sometimes gated/unrecoverable), `old_installers`/
# `archive_pairs`/`large_logs` (all still go through per-item review), and every AI suggestion
# (recommend-only by construction; see evals/test_ai_safety_gate.py) — those all stay in the
# existing Review Queue's per-item confirmation flow, never auto-selected here.
_ONE_CLICK_SAFE_CATEGORY_GROUPS: frozenset[str] = frozenset(
    {"package_caches", "temp_and_browser_caches", "crash_dumps", "dev_artifacts"}
)


def build_one_click_summary(state: AppState) -> OneClickCleanSummaryResponse:
    """Groups the current scan's `_ONE_CLICK_SAFE_CATEGORY_GROUPS` candidates for the
    dashboard's one-click clean button, in plain language (`plain_language_category`) with the
    real measured size/count per group.

    This is the SINGLE place that resolves "which categorically-safe items exist right now" to
    an explicit path list — the dashboard's one-click apply flattens `OneClickGroupOut.paths`
    across the selected groups and sends that list straight through to the existing
    `POST /api/apply`'s `paths` field (with `tier="both"`, since safe mode forces every
    candidate's tier to B — see ADR-0023 guarantee 3 — and the review-queue apply flow already
    defaults tier to "A"). This is a UI/API presentation grouping only: it never bypasses
    `apply_selection`'s safe-mode guard (a blanket tier/category-group selection with no
    explicit `paths` is still refused regardless of what this function returns), and every
    category/tier/method decision continues to run through the exact same `apply_batch` call
    every other apply path uses.
    """
    with ScanIndex(state.db_path) as index:
        if not index.has_any_records():
            return OneClickCleanSummaryResponse(
                has_scan=False,
                groups=[],
                total_bytes=0,
                total_bytes_human=format_bytes(0),
                total_file_count=0,
            )
        candidates = _cached_all_candidates(index, state)

    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.category_group in _ONE_CLICK_SAFE_CATEGORY_GROUPS:
            grouped[candidate.category_group].append(candidate)

    groups: list[OneClickGroupOut] = []
    for group, items in grouped.items():
        plain_label, safety_reason = plain_language_category(group)
        total_bytes = sum(_effective_reclaimable_bytes(item) for item in items)
        groups.append(
            OneClickGroupOut(
                category_group=group,
                plain_label=plain_label,
                safety_reason=safety_reason,
                file_count=len(items),
                total_bytes=total_bytes,
                total_bytes_human=format_bytes(total_bytes),
                paths=[item.path.as_posix() for item in items],
            )
        )
    groups.sort(key=lambda g: g.total_bytes, reverse=True)

    total_bytes = sum(g.total_bytes for g in groups)
    total_file_count = sum(g.file_count for g in groups)
    return OneClickCleanSummaryResponse(
        has_scan=True,
        groups=groups,
        total_bytes=total_bytes,
        total_bytes_human=format_bytes(total_bytes),
        total_file_count=total_file_count,
    )


_DUPLICATE_CLUSTER_REVIEW_LIMIT = 15


def list_duplicate_cluster_review(
    state: AppState, *, limit: int = _DUPLICATE_CLUSTER_REVIEW_LIMIT
) -> DuplicateClusterReviewResponse:
    """ADR-0007: the `limit` largest exact-duplicate clusters by hardlink-aware reclaimable
    bytes, keep-vs-delete paths shown side by side — so a human eyeballs the survivor before any
    apply, not just the biggest logical-size candidates. Reuses `generate_duplicate_candidates`'s
    own safety filtering (whole-cluster exclusion on a BLOCKED non-kept member; ADR-0008's
    per-member model-cache/cross-environment exclusion) rather than recomputing it, so this view
    can never show a cluster the apply pipeline itself would refuse to touch — and the member
    list actually DISPLAYED is restricted to the kept copy plus only the members that survived
    that filtering, never a member ADR-0008 excluded (a raw, unfiltered `cluster` would otherwise
    show an excluded path as if it were still proposed for deletion, which it isn't)."""
    with ScanIndex(state.db_path) as index:
        if not index.has_any_records():
            return DuplicateClusterReviewResponse(has_scan=False, clusters=[])

        config = state.effective_config
        # Computed once and threaded through to `generate_duplicate_candidates` below — that
        # function would otherwise recompute clusters itself, hashing every candidate file a
        # second time (see `generate_duplicate_candidates`'s `clusters` param docstring).
        clusters = find_duplicate_clusters(
            index, min_reclaim_bytes=config.categories.duplicates.min_reclaim_bytes
        )
        duplicate_candidates = generate_duplicate_candidates(
            index, config, state.safety, clusters=clusters
        )
        candidate_by_path = {c.path: c for c in duplicate_candidates}

    rows: list[DuplicateClusterReviewOut] = []
    for cluster in clusters:
        surviving_duplicates = tuple(d for d in cluster.duplicates if d.path in candidate_by_path)
        if not surviving_duplicates:
            # Every non-kept member is missing from the candidate list — either the whole
            # cluster was excluded (a member is SafetyValidator-BLOCKED), every member was
            # excluded per-path (ADR-0008: model-cache/cross-environment), or it fell below the
            # materiality floor. Either way there's nothing here to review.
            continue
        member_candidates = [candidate_by_path[d.path] for d in surviving_duplicates]
        display_cluster = _dataclass_replace(cluster, duplicates=surviving_duplicates)
        reclaimable_total = sum(_effective_reclaimable_bytes(c) for c in member_candidates)
        rows.append(
            DuplicateClusterReviewOut(
                cluster=_duplicate_cluster_out(display_cluster),
                reclaimable_bytes=reclaimable_total,
                reclaimable_bytes_human=format_bytes(reclaimable_total),
                needs_review=cluster_needs_manual_review(display_cluster),
                rationale=member_candidates[0].rationale,
            )
        )

    rows.sort(key=lambda row: row.reclaimable_bytes, reverse=True)
    return DuplicateClusterReviewResponse(has_scan=True, clusters=rows[:limit])


# --- Apply / dry-run -------------------------------------------------------------------------

# ADR-0025: the retention window given to a `_build_user_selected_candidate` result when applied
# in power mode with method="vault" -- irrelevant in safe mode, which forces recycle_bin
# unconditionally regardless of any candidate's retention_days (see `apply_batch`'s own
# docstring). 30 days matches this project's other reviewed-by-a-human category defaults
# (e.g. dev_artifacts' test fixture retention in tests/test_api.py) rather than inventing a new
# number.
_USER_SELECTED_RETENTION_DAYS = 30


def _build_user_selected_candidate(
    path_str: str, *, safety: SafetyValidator, git_cache: GitRepoCache
) -> Candidate | None:
    """ADR-0025 decision 6: builds a fresh, independently `SafetyValidator`-evaluated `Candidate`
    for a path the caller explicitly named that ISN'T already part of the deterministic
    candidate set -- the common case for an AI-suggestion apply (an ordinary photo/document no
    rule detector ever flags). Returns `None` (silently excluded, never an error) for a path
    that no longer exists, is a directory, or fails a FRESH safety evaluation -- the same
    "BLOCKED means excluded, not erroring the whole request" posture
    `reclaim.ai.safety.filter_paths_through_safety_validator` and `detectors.generate_candidates`
    already use. Always `Tier.B` (never A -- this path was never auto-quarantine-eligible) and
    a real, disclosed `retention_days` so a power-mode vault apply gets a genuine restore
    window."""
    path = Path(path_str)
    record = build_record_for_path(path, git_cache)
    if record is None or record.is_dir:
        return None
    result = safety.evaluate(record)
    if result.verdict != Verdict.ELIGIBLE:
        return None
    return Candidate(
        path=path,
        is_dir=False,
        category="user_selected_file",
        category_group="user_selected",
        size_bytes=record.size_bytes,
        tier=Tier.B,
        rationale=(
            "Individually selected (e.g. from the AI Suggestions view) -- safety-validated "
            "independently, the same SafetyValidator pass every deterministic candidate goes "
            "through, immediately before this apply."
        ),
        rebuild_instruction=None,
        safety_verdict=result.verdict,
        safety_reason_code=result.reason_code,
        retention_days=_USER_SELECTED_RETENTION_DAYS,
        # P0-K1a: `record` above is a FRESH `FileRecord` (just built by `build_record_for_path`
        # a few lines up, not a stale scan-index row) -- its dev/ino/mtime are the correct
        # scan-time-equivalent baseline for `executor._preflight_skip_reason`'s identity
        # re-check, same as the other two `Candidate`-construction sites. Not called out by name
        # in this fix's original design note (which only named `detectors.py`/`dedup.py`), but
        # this is the third and only other place a `Candidate` reaching `apply_batch` is built
        # from real data -- leaving it at the 0/0/0.0 default would silently disable the
        # identity check for every AI-suggestion/user-selected apply, not just narrow its
        # coverage.
        dev=record.dev,
        ino=record.ino,
        mtime=record.mtime,
    )


def _apply_response(report: BatchApplyReport) -> ApplyResponse:
    items = [
        ItemApplyResultOut(
            path=item.path.as_posix(),
            category=item.category,
            category_group=item.category_group,
            size_bytes=item.size_bytes,
            tier=item.tier,
            method=item.method,
            succeeded=item.succeeded,
            error=item.error,
            vault_path=item.vault_path.as_posix() if item.vault_path is not None else None,
            skip_reason=item.skip_reason,
            synchronously_purged=item.synchronously_purged,
            postcondition_verification_failed=item.postcondition_verification_failed,
        )
        for item in report.items
    ]
    breakdown = [
        CategoryBreakdownOut(
            category_group=group,
            category_label=category_label(group),
            count=data.count,
            bytes_freed=data.bytes_freed,
            bytes_freed_human=format_bytes(data.bytes_freed),
        )
        for group, data in sorted(report.category_breakdown.items())
    ]
    return ApplyResponse(
        batch_id=report.batch_id,
        apply=report.apply,
        method=report.method,
        items=items,
        files_processed=report.files_processed,
        files_succeeded=report.files_succeeded,
        files_failed=report.files_failed,
        bytes_freed=report.bytes_freed,
        bytes_freed_human=format_bytes(report.bytes_freed),
        category_breakdown=breakdown,
        disk_free_before_bytes=report.disk_free_before_bytes,
        disk_free_after_bytes=report.disk_free_after_bytes,
        disk_free_delta_bytes=report.disk_free_delta_bytes,
        synchronously_purged_count=report.synchronously_purged_count,
        bytes_synchronously_purged=report.bytes_synchronously_purged,
    )


def resolve_apply_selection(
    state: AppState, request: ApplyRequest
) -> tuple[list[Candidate], QuarantineMethod, bool]:
    """Synchronous, request-shape validation + candidate selection for `POST /api/apply`
    (fix/apply-progress-feedback) -- split out of what used to be `apply_selection`'s single
    synchronous call so a malformed/refused REQUEST (safe mode's blanket-selection gate) still
    fails fast with an immediate HTTP error exactly as before that endpoint became a
    background-task + polling pattern (see `run_apply`) -- only the real, potentially slow
    `apply_batch` filesystem work moved to the background.

    Returns `(selected, method, apply)` -- `apply` is `not request.dry_run`, resolved here so
    `run_apply` never needs to see the request/dry_run inversion again (see `ApplyRequest.
    dry_run`'s docstring for why `dry_run=True` -> `apply=False` is not an inversion bug).
    """
    live_mode = state.live_mode

    # Stage 2 "no batch-auto for ANY category" gate: a blanket tier/category-group selection
    # with no explicit per-item `paths` is exactly the one-click "apply everything this tier
    # matches" flow safe mode must never allow — every safe-mode apply must be an explicitly
    # enumerated, human-picked list of paths. (Tier is already forced to B for every candidate
    # in safe mode — see detectors.generate_candidates — so this is a second, independent gate,
    # not the only thing standing between a request and a blanket apply.)
    if live_mode == Mode.SAFE and request.paths is None:
        raise SafeModeViolationError(
            "safe mode requires an explicit paths list for /api/apply — a blanket tier/"
            "category-group selection with no per-item paths is refused, even as a dry run "
            "would be misleading about what a real apply is allowed to do. Select specific "
            "items to apply."
        )

    # perf/apply-scoped-to-paths: an explicit `request.paths` list means we only need to
    # know about THOSE files, not every duplicate cluster in the whole index. Computing
    # the full `_all_candidates` unconditionally here used to call
    # `generate_duplicate_candidates` -> `find_duplicate_clusters`, which BLAKE3-hashes
    # every size-duplicate candidate across the ENTIRE persisted index -- real, unavoidable
    # disk I/O and CPU that scales with total rows scanned, ever, not with this request.
    # Measured on a 3.15M-row index (a machine that had scanned its whole drive over time):
    # a dry-run preview of ONE explicitly-selected AI-suggested file held `state.lock` for
    # 6+ minutes of continuous multi-core work before this fix -- indistinguishable from a
    # hang from the caller's side. `generate_candidates` (rule-based detectors: dev
    # artifacts, temp files, etc.) stays in the scoped path below -- it's cheap per-row
    # metadata matching, no hashing, and a requested path that a rule already flagged still
    # needs its real category/tier, not a generic "user_selected_file" fallback label.
    # Only a genuinely blanket apply (`request.paths is None` -- power mode only; safe mode
    # already refuses this above) still needs the full duplicate-cluster universe, since
    # there's no explicit path list to build fallback candidates from. Goes through
    # `_cached_all_candidates` (perf/dedup-cache, docs/AUDIT-2026-08.md P0-3) rather than
    # `_all_candidates` directly -- this was one of the 5 uncached call sites that fix covers.
    if request.paths is None:
        with ScanIndex(state.db_path) as index:
            candidates = _cached_all_candidates(index, state)
    else:
        with ScanIndex(state.db_path) as index:
            candidates = generate_candidates(index, state.effective_config, state.safety)

    tiers = _TIER_SELECTIONS[request.tier]
    selected = [c for c in candidates if c.tier in tiers]
    if request.category_group is not None:
        selected = [c for c in selected if c.category_group == request.category_group]
    if request.paths is not None:
        wanted = {Path(p).as_posix() for p in request.paths}
        selected = [c for c in selected if c.path.as_posix() in wanted]

        # ADR-0025 decision 6: a requested path NOT already a deterministic candidate (the
        # common case for an AI-suggestion apply, and now also every exact-duplicate applied
        # by explicit path -- see the perf note above) still gets a real, independent safety
        # pass here and, if eligible, joins the batch at Tier B -- "acting on an AI suggestion
        # flows through the exact same apply path as if hand-picked," not silently dropped
        # just because no rule detector happened to flag it. Trade-off, disclosed rather than
        # hidden: an exact-duplicate file applied this way now reports
        # category="user_selected_file" instead of "duplicates" in the batch breakdown --
        # cosmetic (the safety evaluation, quarantine method, and restore path are all
        # identical either way), traded deliberately for not hashing the whole index on
        # every apply request.
        already_matched = {c.path.as_posix() for c in selected}
        unmatched_paths = wanted - already_matched
        if unmatched_paths:
            git_cache = GitRepoCache()
            for path_str in unmatched_paths:
                user_selected = _build_user_selected_candidate(
                    path_str, safety=state.safety, git_cache=git_cache
                )
                if user_selected is not None:
                    selected.append(user_selected)

    # Safe mode only ever allows recycle_bin (apply_batch enforces this structurally
    # regardless of what's resolved here) — auto-resolved so the dashboard doesn't need its
    # own method selector disabled/hidden depending on mode to avoid a confusing 400.
    method: QuarantineMethod = "recycle_bin" if live_mode == Mode.SAFE else request.method
    return selected, method, not request.dry_run


def to_apply_status_out(status: ApplyStatus) -> ApplyStatusOut:
    """Pure formatter -- mirrors `to_scan_status_out`'s role for `ScanStatus`."""
    return ApplyStatusOut(
        status=status.status,
        items_processed=status.items_processed,
        items_total=status.items_total,
        current_category=status.current_category,
        started_at=status.started_at,
        finished_at=status.finished_at,
        error=status.error,
        result=_apply_response(status.result) if status.result is not None else None,
    )


def run_apply(
    state: AppState,
    selected: list[Candidate],
    method: QuarantineMethod,
    apply: bool,
    started_at: float,
) -> None:
    """Background-task body for `POST /api/apply` (fix/apply-progress-feedback) -- same
    threading/locking posture as `run_scan`: runs on Starlette's worker-thread pool, never blocks
    the event loop; `state.lock` guards every read/write of `apply_status` against a concurrent
    `GET /api/apply/status` poll.

    `on_progress` updates `state.apply_status` at the same interval-gated cadence `executor.
    apply_batch`'s own heartbeat log uses (ADR-0026), so polling sees real incremental progress
    during a long-running vault apply, not just idle/running/completed. A failure here --
    including `SafetyInvariantError` (defense-in-depth; should never trigger since `apply_batch`'s
    BLOCKED-candidate check should never find anything, every candidate already having passed
    `SafetyValidator` upstream) -- is recorded on `apply_status` (surfaced via the status
    endpoint), never raised into the background-task machinery where it would just be logged and
    lost -- identical posture to `run_scan`/`run_ai_analysis`.
    """

    def _on_progress(items_processed: int, items_total: int, current_category: str) -> None:
        with state.lock:
            state.apply_status.items_processed = items_processed
            state.apply_status.items_total = items_total
            state.apply_status.current_category = current_category

    try:
        # P0-K1a/M1: a fresh `ScanIndex` opened right here so `apply_batch`'s full-subtree
        # re-walk has the SAME persisted scan data to re-verify irreversible directory
        # candidates against -- `selected` above was built from its own separately-scoped
        # `with ScanIndex(...)` block that has already closed by this point.
        with ScanIndex(state.db_path) as apply_scan_index:
            report = apply_batch(
                selected,
                safety=state.safety,
                apply=apply,
                method=method,
                mode=state.live_mode,
                vault_dir=state.vault_dir,
                manifest_path=state.manifest_path,
                direct_delete_size_guard_bytes=state.config.safety.direct_delete_size_guard_bytes,
                direct_delete_size_guard_retention_days=(
                    state.config.safety.direct_delete_size_guard_retention_days
                ),
                direct_delete_entry_count_guard=(
                    state.config.safety.direct_delete_entry_count_guard
                ),
                on_progress=_on_progress,
                scan_index=apply_scan_index,
            )
    except Exception as exc:  # broad on purpose: a background-task exception must surface via
        # the status endpoint, never crash silently into Starlette's background-task machinery.
        logger.warning("api.apply_failed", error=str(exc))
        with state.lock:
            state.apply_status = ApplyStatus(
                status="failed", started_at=started_at, finished_at=time.time(), error=str(exc)
            )
        return

    with state.lock:
        state.apply_status = ApplyStatus(
            status="completed",
            items_processed=report.files_processed,
            items_total=report.files_processed,
            started_at=started_at,
            finished_at=time.time(),
            result=report,
        )


# --- AI suggestions (recommend-only; ADR-0025) ------------------------------------------------


def has_scan_data(state: AppState) -> bool:
    """Whether this process's index has any scanned records at all -- `POST /api/ai/analyze`'s
    precondition check, kept in `service` (not `routes`) so `routes.py` never needs to import
    `ScanIndex` directly, matching every other route's "call into service, stay thin" shape."""
    with ScanIndex(state.db_path) as index:
        return index.has_any_records()


# Reclaim isn't published to PyPI (installed via the Windows setup file, not `pip`), so a
# blanket "pip install reclaim[ai]" instruction is actively wrong for that audience -- most of
# this tool's real users. There is currently no way to add the AI component to an already-
# installed copy at all (see ADR-0029 for the planned fix -- a bundled/downloaded AI runtime,
# not yet built); this message says so honestly instead of pointing at a command that will just
# fail. The `uv sync --extra ai` instruction is real and correct, but only for the source-
# checkout audience it's scoped to below.
_AI_UNAVAILABLE_REASON = (
    "AI features need extra ML components that aren't included in this installer. There's no "
    "way to add them to an installed copy of Reclaim yet -- this is a known gap (see the "
    "project's ADRs), not a setting you're missing. If you're running Reclaim from a source "
    "checkout instead of the installer, add them with `uv sync --extra ai`."
)


def _ai_unavailable_status_out() -> AIAnalysisStatusOut:
    return AIAnalysisStatusOut(
        status="unavailable",
        unavailable_reason=_AI_UNAVAILABLE_REASON,
        scan_generation=None,
        stale=False,
        started_at=None,
        finished_at=None,
        error=None,
        tracks_run=[],
        tracks_skipped=[],
        files_considered={},
        files_capped={},
    )


def to_ai_status_out(
    status: AIAnalysisStatus, *, current_scan_generation: int
) -> AIAnalysisStatusOut:
    """Pure formatter -- mirrors `to_scan_status_out`'s role for `ScanStatus`. `stale` is true
    only once a NEWER scan generation exists than the one this status's analysis covered."""
    stale = status.scan_generation is not None and status.scan_generation != current_scan_generation
    return AIAnalysisStatusOut(
        status=status.status,
        unavailable_reason=None,
        scan_generation=status.scan_generation,
        stale=stale,
        started_at=status.started_at,
        finished_at=status.finished_at,
        error=status.error,
        tracks_run=list(status.tracks_run),
        tracks_skipped=[
            AITrackSkipOut(track=track, reason=reason) for track, reason in status.tracks_skipped
        ],
        files_considered=dict(status.files_considered),
        files_capped=dict(status.files_capped),
    )


def ai_status_out(state: AppState) -> AIAnalysisStatusOut:
    """`GET /api/ai/status`'s body -- checks `ai_orchestration.ai_extra_available()` FIRST, before
    touching any in-memory analysis state, so a core-only install reports "unavailable"
    immediately rather than a stale/never-run "idle"."""
    if not ai_orchestration.ai_extra_available():
        return _ai_unavailable_status_out()
    with state.lock:
        status = state.ai_status
        current_generation = state.scan_generation
    return to_ai_status_out(status, current_scan_generation=current_generation)


def _fail_ai_analysis(
    state: AppState, *, scan_generation: int, started_at: float, error: str
) -> None:
    with state.lock:
        state.ai_status = AIAnalysisStatus(
            status="failed",
            scan_generation=scan_generation,
            started_at=started_at,
            finished_at=time.time(),
            error=error,
        )


def run_ai_analysis(state: AppState, scan_generation: int, started_at: float) -> None:
    """Background-task body for `POST /api/ai/analyze` -- same threading/locking posture as
    `run_scan` (Starlette dispatches sync background tasks on its own worker threadpool, so this
    never blocks the event loop; `state.lock` guards every read/write of `ai_status`/
    `ai_clusters` against a concurrent `GET /api/ai/status` poll).

    A failure here (including "no scan root recorded for this session") is recorded on
    `ai_status` (surfaced via the status endpoint), never raised into the background-task
    machinery where it would just be logged and lost -- identical posture to `run_scan`."""
    with state.lock:
        root = state.scan_status.root
    if root is None:
        _fail_ai_analysis(
            state,
            scan_generation=scan_generation,
            started_at=started_at,
            error="no scan root recorded for this server session — run a new scan before "
            "analyzing with AI",
        )
        return

    try:
        with ScanIndex(state.db_path) as index:
            records = index.full_inventory(under=root)
        analysis = ai_orchestration.run_ai_analysis(records=records, safety=state.safety)
    except Exception as exc:  # broad on purpose: a background-task exception must surface via
        # the status endpoint, never crash silently into Starlette's background-task machinery.
        logger.warning("api.ai_analysis_failed", error=str(exc))
        _fail_ai_analysis(
            state, scan_generation=scan_generation, started_at=started_at, error=str(exc)
        )
        return

    with state.lock:
        state.ai_clusters = analysis.clusters
        state.ai_status = AIAnalysisStatus(
            status="completed",
            scan_generation=scan_generation,
            started_at=started_at,
            finished_at=time.time(),
            tracks_run=list(analysis.tracks_run),
            tracks_skipped=[(skip.track, skip.reason) for skip in analysis.tracks_skipped],
            files_considered=dict(analysis.files_considered),
            files_capped=dict(analysis.files_capped),
        )


def _ai_suggestion_out(cluster: AICluster) -> AISuggestionOut:
    presented = presentation.present_cluster(cluster)
    members = [
        AIClusterMemberOut(
            path=member.path.as_posix(),
            size_bytes=member.size_bytes,
            size_human=format_bytes(member.size_bytes),
            is_recommended_keep=member.is_recommended_keep,
            position=member.position,
        )
        for member in cluster.members
    ]
    return AISuggestionOut(
        cluster_id=presented.cluster_id,
        track=presented.track.value,
        headline=presented.headline,
        detail_lines=list(presented.detail_lines),
        is_suggestion=presented.is_suggestion,
        browse_only_note=presented.browse_only_note,
        keep_path=presented.keep_path,
        technical_detail=presented.technical_detail,
        members=members,
    )


def build_ai_suggestions(state: AppState) -> AISuggestionsResponse:
    """`GET /api/ai/suggestions`'s body -- calls `reclaim.ai.presentation.present_cluster` per
    cached `AICluster`; no `AICluster`/`AIClusterMember` object ever crosses the Pydantic
    response boundary (`AISuggestionOut` is a hand-mapped shape, not a pass-through)."""
    if not ai_orchestration.ai_extra_available():
        return AISuggestionsResponse(
            status="unavailable",
            unavailable_reason=_AI_UNAVAILABLE_REASON,
            stale=False,
            suggestions=[],
        )
    with state.lock:
        status = state.ai_status
        current_generation = state.scan_generation
        clusters = list(state.ai_clusters)
    stale = status.scan_generation is not None and status.scan_generation != current_generation
    return AISuggestionsResponse(
        status=status.status,
        unavailable_reason=None,
        stale=stale,
        suggestions=[_ai_suggestion_out(cluster) for cluster in clusters],
    )


# --- Quarantine / restore --------------------------------------------------------------------


def _recycle_bin_restore_message(count: int) -> str:
    """Verbatim wording from `executor.RecycleBinRestoreUnsupportedError`'s message (reproduced
    here, not reworded) so the quarantine *listing* view can show it before a restore is ever
    attempted. `POST /api/restore/{batch_id}` additionally surfaces the real exception (raised
    by `executor.restore_batch` itself) when a restore is actually attempted against such a
    batch — this string is a display-only preview of that same, real message, not a
    replacement for it."""
    return (
        f"this batch contains {count} Recycle-Bin-quarantined file(s); restore them manually "
        "via Windows Explorer's Recycle Bin — automated restore isn't supported for this method"
    )


def _direct_delete_restore_message(count: int) -> str:
    """Verbatim wording from `executor.DirectDeleteRestoreImpossibleError`'s message — same
    display-only-preview relationship to the real exception as `_recycle_bin_restore_message`
    above, kept distinct (never reworded to match) per ADR-0001: a direct-delete entry's
    situation is more final than a Recycle-Bin one, and the message says so."""
    return (
        f"this batch contains {count} permanently-deleted file(s) (retention=none for their "
        "category) — there is nothing to restore, they were not quarantined"
    )


def _quarantine_item_out(entry: QuarantineManifestEntry) -> QuarantineItemOut:
    return QuarantineItemOut(
        original_path=entry.original_path.as_posix(),
        size_bytes=entry.size_bytes,
        size_human=format_bytes(entry.size_bytes),
        category=entry.category,
        category_group=entry.category_group,
        rationale=entry.rationale,
        tier=entry.tier,
        method=entry.method,
        restored=entry.restored,
        restored_at=entry.restored_at,
    )


def recovery_status(state: AppState) -> RecoveryStatusResponse:
    """Read-only dashboard surface for ADR-0026's crash recovery: previews what `reclaim
    recover --apply` would do without writing anything (`compute_reconciliation` makes zero
    manifest mutations — safe to call on every dashboard load). A real fix (writing the
    reconciling manifest records) still requires the explicit `reclaim recover --apply` CLI
    command — this endpoint only ever reports, matching every other read endpoint in this
    module."""
    report = compute_reconciliation(manifest_path=state.manifest_path, vault_dir=state.vault_dir)
    pending = [
        RecoveryItemOut(
            operation=item.operation,
            batch_id=item.batch_id,
            original_path=str(item.original_path),
            outcome=item.outcome,
            detail=item.detail,
        )
        for item in report.reconciled
    ]
    return RecoveryStatusResponse(
        scanned_intents=report.scanned_intents,
        already_resolved=report.already_resolved,
        pending=pending,
        has_needs_review=any(item.outcome == "needs_review" for item in report.reconciled),
    )


def list_quarantine_batches(state: AppState) -> QuarantineListResponse:
    """Reads the manifest directly via the public `QuarantineManifestEntry` model (the same
    JSONL shape `executor.py` writes) and folds to latest-per-(batch_id, path) for display.

    This fold is a read-only UI projection, deliberately reimplemented here rather than
    imported from `executor._latest_entries_for_batch` — that helper is private to
    `executor.py` and this module only ever imports executor's public surface, per the task
    boundary ("only import and orchestrate them"). The folding *rule* (last JSONL line per key
    wins) is the same documented contract as the manifest's own append-only format, not a
    reinterpretation of it.
    """
    entries = read_manifest_entries(state.manifest_path)
    latest: dict[tuple[str, str], QuarantineManifestEntry] = {}
    for entry in entries:
        latest[(entry.batch_id, entry.original_path.as_posix())] = entry

    by_batch: dict[str, list[QuarantineManifestEntry]] = defaultdict(list)
    for entry in latest.values():
        by_batch[entry.batch_id].append(entry)

    batches: list[QuarantineBatchOut] = []
    for batch_id, batch_entries in by_batch.items():
        batch_entries.sort(key=lambda e: e.original_path.as_posix())
        vault_entries = [e for e in batch_entries if e.method == "vault"]
        recycle_bin_entries = [e for e in batch_entries if e.method == "recycle_bin"]
        direct_delete_entries = [e for e in batch_entries if e.method == "direct_delete"]
        bytes_total = sum(e.size_bytes for e in batch_entries)
        if vault_entries:
            # `restore_batch` (ADR-0004) restores every vault entry in a batch even if it also
            # contains direct_delete/recycle_bin entries — those are reported per-item as
            # restore_unsupported rather than blocking the whole batch, so the listing view only
            # blocks restore entirely when there is NOTHING restorable at all (below).
            restore_blocked_reason = None
        elif direct_delete_entries:
            # ADR-0001: checked first — a more final situation than a recycle-bin entry, and
            # `restore_batch` itself refuses on direct-delete entries before it ever reaches its
            # recycle-bin check, so the listing view's blocked-reason ordering matches that.
            restore_blocked_reason = _direct_delete_restore_message(len(direct_delete_entries))
        elif recycle_bin_entries:
            restore_blocked_reason = _recycle_bin_restore_message(len(recycle_bin_entries))
        else:
            restore_blocked_reason = None
        batches.append(
            QuarantineBatchOut(
                batch_id=batch_id,
                method=batch_entries[0].method,
                quarantined_at=min(e.quarantined_at for e in batch_entries),
                item_count=len(batch_entries),
                bytes_total=bytes_total,
                bytes_total_human=format_bytes(bytes_total),
                restored_count=sum(1 for e in batch_entries if e.restored),
                can_restore=restore_blocked_reason is None,
                restore_blocked_reason=restore_blocked_reason,
                items=[_quarantine_item_out(e) for e in batch_entries],
            )
        )
    batches.sort(key=lambda b: b.quarantined_at, reverse=True)
    return QuarantineListResponse(batches=batches)


def restore_response(report: RestoreReport) -> RestoreResponse:
    items = [
        RestoreItemOut(
            original_path=item.original_path.as_posix(),
            size_bytes=item.size_bytes,
            succeeded=item.succeeded,
            already_restored=item.already_restored,
            error=item.error,
            restore_unsupported=item.restore_unsupported,
        )
        for item in report.items
    ]
    return RestoreResponse(
        batch_id=report.batch_id,
        items=items,
        files_processed=report.files_processed,
        files_succeeded=report.files_succeeded,
        files_failed=report.files_failed,
        files_unsupported=report.files_unsupported,
        bytes_restored=report.bytes_restored,
        bytes_restored_human=format_bytes(report.bytes_restored),
    )


def validate_restorable_batch(state: AppState, batch_id: str) -> int:
    """Synchronous pre-check for `POST /api/restore/{batch_id}` (fix/apply-progress-feedback) --
    runs the EXACT validation `restore_batch` itself performs before touching anything
    (`executor.resolve_restorable_entries`: batch lookup, the manifest-integrity/zip-slip-
    equivalent guard, the whole-call refusal when nothing is restorable) so a bad batch id,
    an unsupported method, or a corrupted manifest still gets an immediate 404/409/500 from the
    route, exactly as before this endpoint became a background-task + polling pattern. Raises the
    same typed exceptions `restore_batch` would; `routes.restore` translates them to HTTP.

    Returns the vault-entry count -- the real `items_total` the background task's progress will
    report against (see `RestoreStatus`'s docstring for why that's the right total, not the
    whole batch's item count)."""
    vault_entries, _direct_delete_entries, _recycle_bin_entries = resolve_restorable_entries(
        batch_id, manifest_path=state.manifest_path, vault_dir=state.vault_dir, safety=state.safety
    )
    return len(vault_entries)


def to_restore_status_out(status: RestoreStatus) -> RestoreStatusOut:
    """Pure formatter -- mirrors `to_apply_status_out`'s role for `ApplyStatus`."""
    return RestoreStatusOut(
        status=status.status,
        items_processed=status.items_processed,
        items_total=status.items_total,
        current_category=status.current_category,
        started_at=status.started_at,
        finished_at=status.finished_at,
        error=status.error,
        result=restore_response(status.result) if status.result is not None else None,
    )


def run_restore(state: AppState, batch_id: str, started_at: float) -> None:
    """Background-task body for `POST /api/restore/{batch_id}` (fix/apply-progress-feedback) --
    mirrors `run_apply`'s threading/locking posture exactly. `routes.restore` already ran
    `validate_restorable_batch` synchronously before scheduling this, so the typed restore
    exceptions below should not normally trigger here -- but the manifest could in principle
    change between that check and this call (e.g. a second restore of the same batch racing
    ahead), so they're still handled the same broad "record on `restore_status`, never crash the
    background task" way as any other failure, identical posture to `run_scan`/`run_apply`."""

    def _on_progress(items_processed: int, items_total: int, current_category: str) -> None:
        with state.lock:
            state.restore_status.items_processed = items_processed
            state.restore_status.items_total = items_total
            state.restore_status.current_category = current_category

    try:
        report = restore_batch(
            batch_id,
            manifest_path=state.manifest_path,
            vault_dir=state.vault_dir,
            safety=state.safety,
            on_progress=_on_progress,
        )
    except Exception as exc:  # broad on purpose: see run_apply's identical reasoning above.
        logger.warning("api.restore_failed", batch_id=batch_id, error=str(exc))
        with state.lock:
            state.restore_status = RestoreStatus(
                status="failed", started_at=started_at, finished_at=time.time(), error=str(exc)
            )
        return

    with state.lock:
        # `report.files_processed` counts EVERY entry in the batch (direct_delete/recycle_bin
        # included), but `items_total` has meant "restorable (vault) entry count" since this
        # status was first created in `routes.restore` -- `restore_batch`'s per-item progress
        # loop only ever iterates vault entries, never the pre-classified-unsupported ones (see
        # `RestoreItemResult.restore_unsupported`). Reusing the already-tracked total (last set
        # by `_on_progress` above, or the initial vault-entry count if the batch was empty)
        # keeps `items_total` consistent end-to-end instead of silently redefining it to a
        # bigger number on completion -- a real, verifier-caught bug for any mixed-method batch
        # (e.g. the 2026-07-17 real batch: 7 vault entries alongside 23,565 direct_delete ones).
        final_total = state.restore_status.items_total
        state.restore_status = RestoreStatus(
            status="completed",
            items_processed=final_total,
            items_total=final_total,
            started_at=started_at,
            finished_at=time.time(),
            result=report,
        )


# --- Stage 2: mode + first-run --------------------------------------------------------------


def mode_status(state: AppState) -> ModeStatusResponse:
    return ModeStatusResponse(
        mode=state.live_mode, required_power_confirmation=REQUIRED_POWER_MODE_CONFIRMATION
    )


def switch_mode_to_power(state: AppState, request: PowerModeRequest) -> ModeStatusResponse:
    """Raises `ModeSwitchDeniedError` (translated to a 400 by routes.py) if
    `request.confirmation_text` doesn't exactly match the required phrase — mode stays safe,
    nothing is logged."""
    switch_to_power_mode(request.confirmation_text, log_path=state.mode_log_path)
    return ModeStatusResponse(
        mode=state.live_mode, required_power_confirmation=REQUIRED_POWER_MODE_CONFIRMATION
    )


def switch_mode_to_safe(state: AppState) -> ModeStatusResponse:
    switch_to_safe_mode(log_path=state.mode_log_path)
    return ModeStatusResponse(
        mode=state.live_mode, required_power_confirmation=REQUIRED_POWER_MODE_CONFIRMATION
    )


# --- P0-2 fix (2026-08 audit): in-app category settings --------------------------------------


def _category_setting_out(group: str, config: CategoriesConfig) -> CategorySettingOut:
    plain_label, safety_reason = plain_language_category(group)
    description = safety_reason if safety_reason is not None else category_label(group)
    return CategorySettingOut(
        category_group=group,
        category_label=plain_label,
        description=description,
        enabled=getattr(config, group).enabled,
        forced_off_in_safe_mode=group in SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS,
    )


def settings_categories(state: AppState) -> SettingsResponse:
    """Every category's current on-disk `enabled` flag (from `state.config`, i.e. exactly what
    `config.toml` says -- NOT `state.effective_config`'s SAFE-mode-resolved view) plus enough
    plain-language context for the Settings tab to render a real description, not just a raw
    category id. Iterates `CategoriesConfig.model_fields` (not a hand-maintained list) so a
    future category is picked up here automatically."""
    with state.lock:
        config = state.config.categories
    return SettingsResponse(
        categories=[_category_setting_out(group, config) for group in CategoriesConfig.model_fields]
    )


def update_category_setting(state: AppState, category: str, *, enabled: bool) -> SettingsResponse:
    """Toggles one category's `enabled` flag, both on disk (`state.config_path`, so it survives
    past this process) and in memory (`state.config`, so it takes effect immediately without a
    restart -- the same "no restart needed" posture `POST /api/mode/power|safe` already has).
    Raises `ValueError` for a `category` that isn't a real `CategoriesConfig` field.

    Also invalidates `state.candidates_cache` (perf/dedup-cache, docs/AUDIT-2026-08.md P0-3):
    that cache is keyed ONLY on `scan_generation`, which a category toggle never bumps, so
    without this a warmed cache would keep serving the pre-toggle tier classification -- directly
    contradicting the "takes effect immediately" claim above for the common case where the
    dashboard was already loaded once before the toggle. Cleared via the dedicated
    `candidates_cache_lock` (not `state.lock`, which only guards `state.config` here) so this
    never blocks on, or is blocked by, an in-flight `_cached_all_candidates` recompute."""
    if category not in CategoriesConfig.model_fields:
        raise ValueError(f"unknown category {category!r}")
    set_category_enabled(state.config_path, category, enabled=enabled)
    with state.lock:
        updated_category = getattr(state.config.categories, category).model_copy(
            update={"enabled": enabled}
        )
        new_categories = state.config.categories.model_copy(update={category: updated_category})
        state.config = state.config.model_copy(update={"categories": new_categories})
    with state.candidates_cache_lock:
        state.candidates_cache = None
        state.candidates_cache_generation = None
    return settings_categories(state)


def first_run_status(state: AppState) -> FirstRunStatusResponse:
    acknowledged = first_run_is_acknowledged(state.first_run_state_path)
    return FirstRunStatusResponse(acknowledged=acknowledged)


def acknowledge_first_run_screen(state: AppState) -> FirstRunStatusResponse:
    acknowledge_first_run(state.first_run_state_path)
    return FirstRunStatusResponse(acknowledged=True)


# --- Update check (opt-in; see PRIVACY.md's "Updates" section and reclaim.update_check) --------


def check_for_update_status(state: AppState) -> UpdateCheckResponse:
    """Backs `GET /api/update-check`. Checks `state.effective_config.update_check.enabled`
    FIRST — when the feature is off (the default; see PRIVACY.md), returns immediately with
    `status="disabled"` and `reclaim.update_check.check_for_update` is never called, so this
    request makes zero network calls. When on, delegates to that module's own cache/timeout/
    error handling (see its docstring) — this function itself never raises, matching that
    module's own no-raise guarantee."""
    current_version = installed_version()
    if not state.effective_config.update_check.enabled:
        return UpdateCheckResponse(
            enabled=False,
            status="disabled",
            current_version=current_version,
            latest_version=None,
            update_available=False,
            release_url=update_check.RELEASES_PAGE_URL,
        )
    result = update_check.check_for_update(current_version=current_version)
    return UpdateCheckResponse(
        enabled=True,
        status=result.status,
        current_version=result.current_version,
        latest_version=result.latest_version,
        update_available=result.update_available,
        release_url=result.release_url,
    )


# --- G25: bug-report diagnostics ----------------------------------------------------------------


def installed_version() -> str:
    """Resolves the installed `reclaim` distribution version (same source `pip show`/`uv pip
    show` use) — one definition shared by the FastAPI app's own `version=` (see `api.app`) and
    the diagnostics endpoint below, rather than two places that could drift. Falls back to
    `"dev"` for a source checkout with no installed distribution record (e.g. running straight
    out of the repo without `uv tool install .`/`pip install -e .`) — this must never raise.
    """
    try:
        return metadata.version("reclaim")
    except metadata.PackageNotFoundError:
        return "dev"


_DIAGNOSTICS_TAIL_LINES = 200


def _read_log_tail(log_path: Path, *, max_lines: int = _DIAGNOSTICS_TAIL_LINES) -> str:
    """Last `max_lines` lines of the persistent log file, or an explanatory placeholder if it
    doesn't exist yet (a fresh install that hasn't logged anything, or a log path override that
    was never written to). Reads the whole file rather than seeking from the end — the file is
    capped at a few MB by `logging_config`'s rotation, so this is cheap, and correctness (never
    splitting a multi-byte UTF-8 sequence by seeking to an arbitrary byte offset) matters more
    here than shaving a read of an already-small file."""
    if not log_path.exists():
        return "(no log file yet — nothing has been logged this install)"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail) if tail else "(log file is empty)"


def build_diagnostics(state: AppState) -> DiagnosticsResponse:
    """Assembles everything the dashboard's "Copy diagnostics" button hands the user for a bug
    report: paths, counts, and version/mode metadata only — never file content or OCR'd text
    (see `DiagnosticsResponse`'s docstring and PRIVACY.md)."""
    return DiagnosticsResponse(
        reclaim_version=installed_version(),
        mode=state.live_mode,
        ai_extra_installed=ai_orchestration.ai_extra_available(),
        os_version=platform.platform(),
        log_path=str(state.log_path),
        log_tail=_read_log_tail(state.log_path),
    )
