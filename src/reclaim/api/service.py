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
    CandidateOut,
    CandidatesResponse,
    CategoryBreakdownOut,
    CategoryCardOut,
    DiagnosticsResponse,
    DuplicateClusterOut,
    DuplicateClusterReviewOut,
    DuplicateClusterReviewResponse,
    DuplicateMemberOut,
    FirstRunStatusResponse,
    ItemApplyResultOut,
    ModeStatusResponse,
    OneClickCleanSummaryResponse,
    OneClickGroupOut,
    PowerModeRequest,
    QuarantineBatchOut,
    QuarantineItemOut,
    QuarantineListResponse,
    RestoreItemOut,
    RestoreResponse,
    ScanStatusOut,
    SuggestedScanRootOut,
    SuggestedScanRootsResponse,
    SummaryResponse,
    TreemapNodeOut,
    TreemapResponse,
    category_label,
    format_bytes,
    plain_language_category,
)
from reclaim.api.state import AIAnalysisStatus, AppState, ScanStatus
from reclaim.dedup import (
    cluster_needs_manual_review,
    find_duplicate_clusters,
    generate_duplicate_candidates,
)
from reclaim.detectors import generate_candidates
from reclaim.executor import (
    BatchApplyReport,
    QuarantineManifestEntry,
    QuarantineMethod,
    RestoreReport,
    SafeModeViolationError,
    apply_batch,
)
from reclaim.first_run import acknowledge as acknowledge_first_run
from reclaim.first_run import is_acknowledged as first_run_is_acknowledged
from reclaim.index import ScanIndex, physical_size_bytes
from reclaim.mode import (
    REQUIRED_POWER_MODE_CONFIRMATION,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Candidate, DuplicateCluster, FileRecord, Mode, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path, scan_tree

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
    directly — see `AppState.effective_config`'s docstring."""
    config = state.effective_config
    candidates = generate_candidates(index, config, state.safety)
    candidates += generate_duplicate_candidates(index, config, state.safety)
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
    )


def run_scan(state: AppState, root: Path, started_at: float) -> None:
    """Background-task body for `POST /api/scan`. Runs on Starlette's worker-thread pool (sync
    callables passed to `BackgroundTasks.add_task` are dispatched via `run_in_threadpool`), so
    this never blocks the event loop; `state.lock` guards every read/write of `scan_status`
    against a concurrent `GET /api/scan/status` poll from the request-handling thread(s).

    A scan failure is recorded on `scan_status` (surfaced via the status endpoint), never
    raised into the background-task machinery where it would just be logged and lost.
    """
    try:
        state.db_path.parent.mkdir(parents=True, exist_ok=True)
        with ScanIndex(state.db_path) as index:
            stats = scan_tree(root, index, incremental=True)
    except Exception as exc:  # broad on purpose: a background-task exception must surface via
        # the status endpoint, never crash silently into Starlette's background-task machinery.
        logger.warning("api.scan_failed", root=str(root), error=str(exc))
        with state.lock:
            state.scan_status = ScanStatus(
                status="failed",
                root=root,
                started_at=started_at,
                finished_at=time.time(),
                error=str(exc),
            )
        return

    with state.lock:
        state.scan_status = ScanStatus(
            status="completed",
            root=root,
            started_at=started_at,
            finished_at=time.time(),
            dirs_visited=stats.dirs_visited,
            entries_total=stats.entries_total,
            files_written=stats.files_written,
            files_unchanged=stats.files_unchanged,
            files_pruned=stats.files_pruned,
            elapsed_seconds=stats.elapsed_seconds,
        )
        # ADR-0025: a new completed scan invalidates any cached AI analysis -- callers compare
        # this against `AIAnalysisStatus.scan_generation` to detect a stale cache.
        state.scan_generation += 1


# --- Summary / category cards -------------------------------------------------------------


def _category_cards(candidates: Sequence[Candidate]) -> list[CategoryCardOut]:
    grouped: dict[tuple[str, Tier], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.category_group, candidate.tier)].append(candidate)

    cards = [
        CategoryCardOut(
            category_group=group,
            category_label=category_label(group),
            tier=tier,
            file_count=len(items),
            total_bytes=sum(item.size_bytes for item in items),
            total_bytes_human=format_bytes(sum(item.size_bytes for item in items)),
        )
        for (group, tier), items in grouped.items()
    ]
    cards.sort(key=lambda c: c.total_bytes, reverse=True)
    return cards


def build_summary(state: AppState) -> SummaryResponse:
    with ScanIndex(state.db_path) as index:
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
            )
        total_indexed_bytes = physical_size_bytes(index.full_inventory())
        candidates = _all_candidates(index, state)

    tier_a = [c for c in candidates if c.tier == Tier.A]
    tier_b = [c for c in candidates if c.tier == Tier.B]
    return SummaryResponse(
        has_scan=True,
        total_indexed_bytes=total_indexed_bytes,
        total_indexed_human=format_bytes(total_indexed_bytes),
        tier_a_bytes=sum(c.size_bytes for c in tier_a),
        tier_a_count=len(tier_a),
        tier_b_bytes=sum(c.size_bytes for c in tier_b),
        tier_b_count=len(tier_b),
        categories=_category_cards(candidates),
    )


# --- Treemap -------------------------------------------------------------------------------


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
        candidates = _all_candidates(index, state)
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

        candidates = _all_candidates(index, state)
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
    total_bytes = sum(c.size_bytes for c in filtered)
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
        candidates = _all_candidates(index, state)

    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.category_group in _ONE_CLICK_SAFE_CATEGORY_GROUPS:
            grouped[candidate.category_group].append(candidate)

    groups: list[OneClickGroupOut] = []
    for group, items in grouped.items():
        plain_label, safety_reason = plain_language_category(group)
        total_bytes = sum(item.size_bytes for item in items)
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
        reclaimable_total = sum(
            c.reclaimable_bytes if c.reclaimable_bytes is not None else c.size_bytes
            for c in member_candidates
        )
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
    )


def apply_selection(state: AppState, request: ApplyRequest) -> ApplyResponse:
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

    with ScanIndex(state.db_path) as index:
        candidates = _all_candidates(index, state)

    tiers = _TIER_SELECTIONS[request.tier]
    selected = [c for c in candidates if c.tier in tiers]
    if request.category_group is not None:
        selected = [c for c in selected if c.category_group == request.category_group]
    if request.paths is not None:
        wanted = {Path(p).as_posix() for p in request.paths}
        selected = [c for c in selected if c.path.as_posix() in wanted]

        # ADR-0025 decision 6: a requested path NOT already a deterministic candidate (the
        # common case for an AI-suggestion apply) still gets a real, independent safety pass
        # here and, if eligible, joins the batch at Tier B -- "acting on an AI suggestion flows
        # through the exact same apply path as if hand-picked," not silently dropped just
        # because no rule detector happened to flag it.
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

    report = apply_batch(
        selected,
        safety=state.safety,
        apply=not request.dry_run,  # `dry_run=True` (the default) => `apply=False` (executor's
        # own default) — the two defaults agree on "touch nothing", which is the invariant that
        # actually matters; see `ApplyRequest.dry_run`'s docstring for why this is not an
        # inversion bug.
        method=method,
        mode=live_mode,
        vault_dir=state.vault_dir,
        manifest_path=state.manifest_path,
        direct_delete_size_guard_bytes=state.config.safety.direct_delete_size_guard_bytes,
        direct_delete_size_guard_retention_days=(
            state.config.safety.direct_delete_size_guard_retention_days
        ),
    )
    return _apply_response(report)


# --- AI suggestions (recommend-only; ADR-0025) ------------------------------------------------


def has_scan_data(state: AppState) -> bool:
    """Whether this process's index has any scanned records at all -- `POST /api/ai/analyze`'s
    precondition check, kept in `service` (not `routes`) so `routes.py` never needs to import
    `ScanIndex` directly, matching every other route's "call into service, stay thin" shape."""
    with ScanIndex(state.db_path) as index:
        return index.has_any_records()


_AI_UNAVAILABLE_REASON = (
    "AI features need the optional AI component — install with: pip install reclaim[ai] "
    "(or `uv sync --extra ai` from a source checkout)."
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


def _read_manifest_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    if not manifest_path.exists():
        return []
    entries: list[QuarantineManifestEntry] = []
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            entries.append(QuarantineManifestEntry.model_validate_json(stripped))
    return entries


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
    entries = _read_manifest_entries(state.manifest_path)
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


def first_run_status(state: AppState) -> FirstRunStatusResponse:
    acknowledged = first_run_is_acknowledged(state.first_run_state_path)
    return FirstRunStatusResponse(acknowledged=acknowledged)


def acknowledge_first_run_screen(state: AppState) -> FirstRunStatusResponse:
    acknowledge_first_run(state.first_run_state_path)
    return FirstRunStatusResponse(acknowledged=True)


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
