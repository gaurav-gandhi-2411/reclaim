from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog

from reclaim.api.schemas import (
    ApplyRequest,
    ApplyResponse,
    CandidateOut,
    CandidatesResponse,
    CategoryBreakdownOut,
    CategoryCardOut,
    DuplicateClusterOut,
    DuplicateMemberOut,
    ItemApplyResultOut,
    QuarantineBatchOut,
    QuarantineItemOut,
    QuarantineListResponse,
    RestoreItemOut,
    RestoreResponse,
    ScanStatusOut,
    SummaryResponse,
    TreemapNodeOut,
    TreemapResponse,
    category_label,
    format_bytes,
)
from reclaim.api.state import AppState, ScanStatus
from reclaim.dedup import find_duplicate_clusters, generate_duplicate_candidates
from reclaim.detectors import generate_candidates
from reclaim.executor import (
    BatchApplyReport,
    QuarantineManifestEntry,
    RestoreReport,
    apply_batch,
)
from reclaim.index import ScanIndex, physical_size_bytes
from reclaim.models import Candidate, DuplicateCluster, FileRecord, Tier
from reclaim.scanner import scan_tree

logger = structlog.get_logger(__name__)

_TIER_SELECTIONS: dict[str, frozenset[Tier]] = {
    "A": frozenset({Tier.A}),
    "B": frozenset({Tier.B}),
    "both": frozenset({Tier.A, Tier.B}),
}


def _all_candidates(index: ScanIndex, state: AppState) -> list[Candidate]:
    """Combined detector + exact-duplicate candidate list — the same two-function contract
    `cli.py::_run_apply` already uses, just orchestrated for the API layer instead."""
    candidates = generate_candidates(index, state.config, state.safety)
    candidates += generate_duplicate_candidates(index, state.config, state.safety)
    return candidates


# --- Scan --------------------------------------------------------------------------------


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
            _index_clusters_by_duplicate_path(find_duplicate_clusters(index))
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


# --- Apply / dry-run -------------------------------------------------------------------------


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
    with ScanIndex(state.db_path) as index:
        candidates = _all_candidates(index, state)

    tiers = _TIER_SELECTIONS[request.tier]
    selected = [c for c in candidates if c.tier in tiers]
    if request.category_group is not None:
        selected = [c for c in selected if c.category_group == request.category_group]
    if request.paths is not None:
        wanted = {Path(p).as_posix() for p in request.paths}
        selected = [c for c in selected if c.path.as_posix() in wanted]

    report = apply_batch(
        selected,
        safety=state.safety,
        apply=not request.dry_run,  # `dry_run=True` (the default) => `apply=False` (executor's
        # own default) — the two defaults agree on "touch nothing", which is the invariant that
        # actually matters; see `ApplyRequest.dry_run`'s docstring for why this is not an
        # inversion bug.
        method=request.method,
        vault_dir=state.vault_dir,
        manifest_path=state.manifest_path,
    )
    return _apply_response(report)


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
        recycle_bin_entries = [e for e in batch_entries if e.method == "recycle_bin"]
        direct_delete_entries = [e for e in batch_entries if e.method == "direct_delete"]
        bytes_total = sum(e.size_bytes for e in batch_entries)
        if direct_delete_entries:
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
        )
        for item in report.items
    ]
    return RestoreResponse(
        batch_id=report.batch_id,
        items=items,
        files_processed=report.files_processed,
        files_succeeded=report.files_succeeded,
        files_failed=report.files_failed,
        bytes_restored=report.bytes_restored,
        bytes_restored_human=format_bytes(report.bytes_restored),
    )
