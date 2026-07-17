from __future__ import annotations

import itertools
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path

import blake3
import structlog

from reclaim.config import Config
from reclaim.index import HashCacheEntry, ScanIndex, cached_full_hash, cached_partial_hash
from reclaim.linkinfo import estimate_reclaimable_bytes
from reclaim.models import (
    Candidate,
    DuplicateCluster,
    FileRecord,
    HashSkip,
    MaterialityExclusionStats,
    Tier,
    Verdict,
)
from reclaim.safety import SafetyValidator

logger = structlog.get_logger(__name__)

_PARTIAL_HASH_CHUNK_BYTES = 64 * 1024
# Files at or below this size are hashed whole in one read rather than as two 64KB chunks —
# reading first+last 64KB of a <=128KB file would double-read overlapping bytes (spec: "don't
# double-read overlapping regions").
_PARTIAL_HASH_WHOLE_FILE_THRESHOLD = 2 * _PARTIAL_HASH_CHUNK_BYTES
_FULL_HASH_READ_CHUNK_BYTES = 1024 * 1024

_KEEP_HEURISTIC_LOCATION_SEGMENTS = frozenset({"downloads", "temp"})

_CATEGORY = "exact_duplicate"
_CATEGORY_GROUP = "duplicates"

# Observability: a multi-hour hash pass that prints nothing is indistinguishable from a hang
# (the real-disk-run incident this guards against — a 3.1M-file scan whose hash stage produced
# zero output and zero incrementing SQLite rows for as long as anyone watched it). One heartbeat
# line at most every this many seconds, plus flushing hash writes in batches rather than one
# giant commit at the very end, so `SELECT COUNT(*) FROM files WHERE partial_hash IS NOT NULL`
# actually moves while a run is in progress.
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_WRITE_BATCH_SIZE = 500

# Per-file hang guard: a locked system file or a pathological read must never wedge the whole
# pipeline (known cloud-placeholder files are already excluded upstream by
# `ScanIndex.candidate_inventory()`'s `is_cloud_placeholder` filter, but this is the backstop
# for everything that filter doesn't catch). The read runs on a worker thread so a stuck
# syscall only costs one pool slot, not the calling thread — Python has no cross-platform way
# to preempt a blocked `read()`, so a timed-out thread is abandoned, not killed.
_HASH_READ_TIMEOUT_SECONDS = 30.0
_HASH_TIMEOUT_WORKERS = 8

# Mirrors `config.DuplicatesConfig.min_reclaim_bytes`'s default — the value real callers
# (`generate_duplicate_candidates`, driven by `config.categories.duplicates.min_reclaim_bytes`)
# actually use. Kept as a literal default here too so `find_duplicate_clusters`/
# `materiality_exclusion_stats` stay usable without a `Config` object (evals, the API service
# layer's direct `find_duplicate_clusters` call) without silently reverting to "hash
# everything" if a caller forgets to pass it.
_DEFAULT_MIN_RECLAIM_BYTES = 1024 * 1024


def _is_downloads_or_temp(path: Path) -> bool:
    """Spec's keep-heuristic rule 1: "prefer copy outside Downloads/Temp"."""
    return any(part.lower() in _KEEP_HEURISTIC_LOCATION_SEGMENTS for part in path.parts)


def _location_rank(record: FileRecord) -> int:
    """ADR-0007: lower ranks first (preferred as keep). `0`: inside a git repository / active
    project — the strongest "this is the real, working copy" signal available. `1`: neither in
    a git repo nor in Downloads/Temp — a generic, undetermined location. `2`: inside
    Downloads/Temp — exactly the places an incidental/junk copy lives.

    A plain "not Downloads/Temp" boolean (this project's original rule 1) doesn't distinguish a
    genuine project copy from an arbitrary non-Downloads/Temp location — a copy sitting in, say,
    a random `Documents/backup` folder would tie with a git-repo copy on that check alone and
    fall through to the ctime/depth tiebreaks, which could pick the non-project copy as keep and
    propose the git-repo copy for deletion. This three-way rank makes git-repo membership a
    POSITIVE, first-class preference instead of just the absence of a Downloads/Temp penalty —
    a git-repo member is never outranked by a non-git, non-Downloads/Temp member.
    """
    if record.git_repo_root is not None:
        return 0
    if _is_downloads_or_temp(record.path):
        return 2
    return 1


def _is_risky_sole_survivor_location(record: FileRecord) -> bool:
    """ADR-0007: true if `record` sits in Downloads/Temp or is a cloud-sync placeholder (not
    fully synced locally) — the two location classes unsuitable to be the SOLE surviving copy
    of a duplicate cluster once every other member is deleted. Distinct from `_location_rank`'s
    keep-heuristic ranking: this asks "is the location we ended up keeping actually durable,"
    not "which candidate should we prefer."""
    return _is_downloads_or_temp(record.path) or record.is_cloud_placeholder


def cluster_needs_manual_review(cluster: DuplicateCluster) -> bool:
    """ADR-0007: true if the kept copy would be the cluster's sole survivor in a risky location
    (Downloads/Temp/cloud-placeholder) while at least one about-to-be-deleted member sits
    somewhere more durable. Auto-applying a cluster like this would strand the only surviving
    copy somewhere less durable than what's being thrown away — flagged for manual review
    (forced Tier B) instead of auto-picked, regardless of `config.categories.duplicates.enabled`.

    Honest reachability note: given `select_keep`'s location-rank ordering (`_location_rank` —
    Downloads/Temp always ranks worst), a Downloads/Temp copy can only win as keep if every
    OTHER cluster member is ALSO Downloads/Temp, in which case there is no more-durable member
    being thrown away and this function correctly returns `False` regardless. Given
    `ScanIndex.duplicate_size_candidates`'s pre-existing `is_cloud_placeholder = 0` filter, a
    cloud-placeholder file never becomes a cluster member (keep or duplicate) through the real
    pipeline at all. So this function's `True` branch is not reachable via
    `generate_duplicate_candidates` as the codebase stands today — it exists as defense-in-depth
    should either of those two upstream guarantees ever change, and is verified directly at the
    unit level (see `test_cluster_needs_manual_review_when_keep_is_risky_and_a_deleted_copy_is_
    stable`) rather than an end-to-end scenario that can't currently be constructed.
    """
    if not _is_risky_sole_survivor_location(cluster.keep):
        return False
    return any(not _is_risky_sole_survivor_location(d) for d in cluster.duplicates)


def _compute_partial_hash(path: Path, size_bytes: int) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        if size_bytes <= _PARTIAL_HASH_WHOLE_FILE_THRESHOLD:
            hasher.update(fh.read())
        else:
            hasher.update(fh.read(_PARTIAL_HASH_CHUNK_BYTES))
            fh.seek(size_bytes - _PARTIAL_HASH_CHUNK_BYTES)
            hasher.update(fh.read(_PARTIAL_HASH_CHUNK_BYTES))
    return hasher.hexdigest()


def _compute_full_hash(path: Path) -> str:
    hasher = blake3.blake3()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_FULL_HASH_READ_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _due(*, last: float, now: float, interval: float) -> bool:
    """Pure predicate behind the heartbeat gate — split out from the hashing loops so the
    timing logic itself is unit-testable without needing a multi-second real sleep."""
    return (now - last) >= interval


def _hash_with_guard(
    executor: ThreadPoolExecutor,
    fn: Callable[..., str],
    path: Path,
    *args: object,
    timeout_seconds: float = _HASH_READ_TIMEOUT_SECONDS,
) -> tuple[str | None, str | None]:
    """Runs `fn(path, *args)` on `executor` and returns `(digest, skip_reason)` — exactly one
    is `None`. Never raises: a timeout or `OSError` both become a skip reason instead of
    propagating and killing the whole dedup run over one bad file."""
    future = executor.submit(fn, path, *args)
    try:
        return future.result(timeout=timeout_seconds), None
    except FutureTimeoutError:
        return None, "timeout"
    except OSError as exc:
        return None, str(exc)


def select_keep(members: Sequence[FileRecord]) -> FileRecord:
    """Picks the one cluster member to keep. Ranking, in order (ADR-0007): (1) location rank —
    prefer a copy inside a git repository/active project, then a copy in neither a git repo nor
    Downloads/Temp, then last a copy under Downloads/Temp (see `_location_rank`); (2) shortest
    path depth (fewest path segments from the drive root); (3) oldest `ctime` — Windows creation
    time, the "which copy existed first" signal, not POSIX change time; (4) lexicographic path
    sort as a final, deterministic tiebreak so output is reproducible run-to-run.

    Content survival was never the risk here — every cluster member is byte-identical by
    construction. Keeping the WRONG copy is: this ordering guarantees a git-repo/project member
    is never the one proposed for deletion as long as at least one exists in the cluster, even
    if a Downloads/Temp copy happens to have an older creation time or shallower path (the
    failure mode rank (1) alone, without git-repo awareness, could otherwise produce).
    """
    return min(
        members,
        key=lambda record: (
            _location_rank(record),
            len(record.path.parts),
            record.ctime,
            record.path.as_posix(),
        ),
    )


def _cached_partial_lookup(entry: HashCacheEntry | None, size: int, mtime: float) -> str | None:
    return cached_partial_hash(entry, current_size=size, current_mtime=mtime)


def _cached_full_lookup(entry: HashCacheEntry | None, size: int, mtime: float) -> str | None:
    return cached_full_hash(entry, current_size=size, current_mtime=mtime)


def _hash_member(
    *,
    index: ScanIndex,
    executor: ThreadPoolExecutor,
    hash_cache: dict[str, HashCacheEntry],
    record: FileRecord,
    stage: str,
    cached_lookup: Callable[[HashCacheEntry | None, int, float], str | None],
    compute: Callable[..., str],
    compute_args: tuple[object, ...],
    pending_writes: list[tuple[Path, int, float, str]],
    skips: list[HashSkip] | None,
) -> str | None:
    """Shared body of one hash computation (partial or full), used identically by both stages
    of `find_duplicate_clusters`'s per-bucket loop: check the cache, else hash-with-guard, else
    record a skip; queue a DB write on a genuine cache miss. Returns `None` on skip."""
    entry = hash_cache.get(record.path.as_posix())
    digest = cached_lookup(entry, record.size_bytes, record.mtime)
    if digest is not None:
        return digest
    digest, reason = _hash_with_guard(executor, compute, record.path, *compute_args)
    if digest is None:
        logger.warning("dedup.hash_unreadable", stage=stage, path=str(record.path), reason=reason)
        if skips is not None:
            skips.append(HashSkip(path=record.path, stage=stage, reason=reason or ""))
        return None
    pending_writes.append((record.path, record.size_bytes, record.mtime, digest))
    return digest


def materiality_exclusion_stats(
    index: ScanIndex, *, min_reclaim_bytes: int = _DEFAULT_MIN_RECLAIM_BYTES
) -> MaterialityExclusionStats:
    """How many duplicate-size buckets `find_duplicate_clusters` will skip for falling below
    `min_reclaim_bytes`, and their summed theoretical (never measured, always labeled as an
    upper bound) reclaim — a cheap, independent SQL aggregate query, safe to call any time
    (e.g. from the CLI report) without running the hash pass itself."""
    bucket_count, theoretical_bytes = index.immaterial_duplicate_bucket_stats(
        min_reclaim_bytes=min_reclaim_bytes
    )
    return MaterialityExclusionStats(
        excluded_bucket_count=bucket_count, theoretical_bytes=theoretical_bytes
    )


def find_duplicate_clusters(
    index: ScanIndex,
    *,
    min_reclaim_bytes: int = _DEFAULT_MIN_RECLAIM_BYTES,
    skips: list[HashSkip] | None = None,
) -> list[DuplicateCluster]:
    """Size bucket -> 64KB partial hash -> full BLAKE3 hash, exactly in that order, reusing
    cached hashes from a prior run wherever a file's (size, mtime) hasn't changed since.

    `skips` is an optional out-param (append-to list, default `None` = don't bother collecting)
    rather than a second return value, so existing callers (`generate_duplicate_candidates`,
    the API service layer, evals) keep working unchanged against the plain
    `list[DuplicateCluster]` return type; callers that care about the skipped/unreadable files
    pass their own list and read it back after the call.

    `min_reclaim_bytes` is the materiality gate (2026-07-17 real-disk finding): a bucket whose
    theoretical best-case reclaim — `(member_count - 1) * size` — falls below this floor is
    never even queried for its members, let alone hashed. See
    `ScanIndex.duplicate_size_candidates`'s docstring and `materiality_exclusion_stats` (the
    reporting counterpart of this same filter).

    SQL-pushdown, streamed one size bucket at a time: `index.duplicate_size_candidates()`
    returns rows in size order (see its docstring), so `itertools.groupby` groups each size
    bucket's members together as they stream past — partial-hashed, and (for survivors)
    full-hashed, immediately, before the next bucket is even read from SQLite. This replaced an
    earlier design that materialized *every* candidate row into one `dict[size, list[...]]`
    before hashing anything: correct, but on a real disk where the size-uniqueness prefilter
    barely narrows anything (measured: 80% of files on one real `C:\\` shared a size with at
    least one other file), that meant building millions of `FileRecord` objects — and holding
    them all at once — before a single hash ran. Peak memory here is bounded by the *largest
    single size bucket*, not the total candidate count.
    """
    candidate_count = index.duplicate_size_candidate_count(min_reclaim_bytes=min_reclaim_bytes)
    if candidate_count == 0:
        return []

    hash_cache = index.load_hash_cache()
    excluded = materiality_exclusion_stats(index, min_reclaim_bytes=min_reclaim_bytes)
    logger.info(
        "dedup.start",
        candidate_files=candidate_count,
        min_reclaim_bytes=min_reclaim_bytes,
        materiality_excluded_buckets=excluded.excluded_bucket_count,
        materiality_excluded_theoretical_bytes=excluded.theoretical_bytes,
    )

    clusters: list[DuplicateCluster] = []
    partial_writes: list[tuple[Path, int, float, str]] = []
    full_writes: list[tuple[Path, int, float, str]] = []
    partial_hashed = 0
    full_hashed = 0
    buckets_seen = 0
    last_heartbeat = time.monotonic()

    with ThreadPoolExecutor(max_workers=_HASH_TIMEOUT_WORKERS) as executor:
        for size, members_iter in itertools.groupby(
            index.duplicate_size_candidates(min_reclaim_bytes=min_reclaim_bytes),
            key=lambda record: record.size_bytes,
        ):
            buckets_seen += 1
            members = list(members_iter)  # bounded by this one bucket, not the whole candidate set

            partial_groups: dict[str, list[FileRecord]] = defaultdict(list)
            for record in members:
                digest = _hash_member(
                    index=index,
                    executor=executor,
                    hash_cache=hash_cache,
                    record=record,
                    stage="partial",
                    cached_lookup=_cached_partial_lookup,
                    compute=_compute_partial_hash,
                    compute_args=(record.size_bytes,),
                    pending_writes=partial_writes,
                    skips=skips,
                )
                if digest is None:
                    continue
                partial_groups[digest].append(record)
                partial_hashed += 1
                if len(partial_writes) >= _WRITE_BATCH_SIZE:
                    index.store_partial_hashes(partial_writes)
                    partial_writes.clear()
                now = time.monotonic()
                if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                    logger.info(
                        "dedup.progress",
                        buckets_seen=buckets_seen,
                        partial_hashed=partial_hashed,
                        full_hashed=full_hashed,
                        candidate_files=candidate_count,
                        clusters_found=len(clusters),
                    )
                    last_heartbeat = now

            for subset in partial_groups.values():
                if len(subset) < 2:
                    continue
                full_groups: dict[str, list[FileRecord]] = defaultdict(list)
                for record in subset:
                    digest = _hash_member(
                        index=index,
                        executor=executor,
                        hash_cache=hash_cache,
                        record=record,
                        stage="full",
                        cached_lookup=_cached_full_lookup,
                        compute=_compute_full_hash,
                        compute_args=(),
                        pending_writes=full_writes,
                        skips=skips,
                    )
                    if digest is None:
                        continue
                    full_groups[digest].append(record)
                    full_hashed += 1
                    if len(full_writes) >= _WRITE_BATCH_SIZE:
                        index.store_full_hashes(full_writes)
                        full_writes.clear()
                    now = time.monotonic()
                    if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                        logger.info(
                            "dedup.progress",
                            buckets_seen=buckets_seen,
                            partial_hashed=partial_hashed,
                            full_hashed=full_hashed,
                            candidate_files=candidate_count,
                            clusters_found=len(clusters),
                        )
                        last_heartbeat = now

                for full_hash, final_members in full_groups.items():
                    if len(final_members) < 2:
                        continue
                    keep = select_keep(final_members)
                    duplicates = tuple(m for m in final_members if m.path != keep.path)
                    clusters.append(
                        DuplicateCluster(
                            full_hash=full_hash, size_bytes=size, keep=keep, duplicates=duplicates
                        )
                    )
    if partial_writes:
        index.store_partial_hashes(partial_writes)
    if full_writes:
        index.store_full_hashes(full_writes)

    logger.info(
        "dedup.done",
        buckets_seen=buckets_seen,
        clusters=len(clusters),
        skipped=len(skips) if skips is not None else 0,
    )
    return clusters


def _keep_rationale(cluster: DuplicateCluster) -> str:
    """Concrete, honest rationale naming the kept path and the factual heuristic-relevant
    properties that led to it being kept — never a fabricated claim about which single rule
    was decisive, since that depends on the other members it was compared against."""
    if cluster.keep.git_repo_root is not None:
        location = f"inside git repository '{cluster.keep.git_repo_root}'"
    elif _is_downloads_or_temp(cluster.keep.path):
        location = "under a Downloads/Temp directory (shared by every member of this cluster)"
    else:
        location = "outside Downloads/Temp (not in a git repository)"
    created = datetime.fromtimestamp(cluster.keep.ctime, tz=UTC).isoformat()
    return (
        f"Exact duplicate of '{cluster.keep.path}' (byte-identical, BLAKE3 full-hash match); "
        f"kept copy is {location}, created {created}, path depth "
        f"{len(cluster.keep.path.parts)} — selected by the keep-heuristic (ADR-0007: prefer "
        "git-repo/project membership, then shallowest path, then oldest creation time, then "
        "lexicographic order)."
    )


def generate_duplicate_candidates(
    index: ScanIndex,
    config: Config,
    safety: SafetyValidator,
    *,
    skips: list[HashSkip] | None = None,
    clusters: Sequence[DuplicateCluster] | None = None,
) -> list[Candidate]:
    """Mirrors `detectors.py::generate_candidates()`'s contract/shape: runs every non-keep
    cluster member through `SafetyValidator.evaluate()` before it is ever tagged a tier.
    `REVIEW_ONLY` -> forced Tier B; `ELIGIBLE` -> Tier A only if `config.categories.duplicates`
    is enabled, else Tier B. The kept member of a cluster is never evaluated for a tier and
    never appears in the output — it isn't being proposed for any action.

    `skips` is forwarded to `find_duplicate_clusters` unchanged — see its docstring.
    `min_reclaim_bytes` comes from `config.categories.duplicates.min_reclaim_bytes` — the
    materiality gate is config-driven, not hardcoded, same as every other category threshold.

    `clusters`: pass an already-computed cluster list (e.g. from a caller that also needs the
    keep/delete shape directly, like the dashboard's cluster-review endpoint) to skip a second
    full `find_duplicate_clusters` pass — hashing every candidate file twice on a multi-million-
    file real disk index is expensive, and a stale-quarantine-path `hash_unreadable` warning gets
    logged once per pass, so calling this twice back-to-back doubles both the runtime and the
    log noise for no benefit. Defaults to `None`, computing clusters here exactly as before.

    ADR-0006: each cluster's non-keep members also go through `linkinfo.
    estimate_reclaimable_bytes` (one direct `os.stat()` per member, bounded by cluster size —
    never the whole inventory). A "duplicate" that's actually a hardlink to the kept copy
    already shares the same on-disk blocks and reclaims 0 bytes if deleted; byte-identical
    content — this category's entire selection criterion — is exactly what a hardlink produces,
    so this is not a rare edge case here. Exposed as `Candidate.reclaimable_bytes`, distinct
    from `size_bytes`'s logical size and never silently substituted for it.

    ADR-0007, two safety checks beyond the per-member evaluation:
    1. If ANY non-kept member is `BLOCKED` (a protected root, git-repo membership, a protected
       extension, ...), the WHOLE cluster is excluded — not just that one member, silently
       proposing the rest as if nothing were unusual about this byte-identical group. A cluster
       with a protected member deserves a human looking at the whole group, not a partial
       candidate list with no visibility into what got left out.
    2. If the kept copy would be the cluster's sole surviving location and that location is
       risky (Downloads/Temp/a cloud-sync placeholder) while a more durable copy is being
       deleted, every surviving candidate in the cluster is forced to Tier B (review), regardless
       of `config.categories.duplicates.enabled` — see `cluster_needs_manual_review`.
    """
    candidates: list[Candidate] = []
    min_reclaim_bytes = config.categories.duplicates.min_reclaim_bytes
    resolved_clusters = (
        clusters
        if clusters is not None
        else find_duplicate_clusters(index, min_reclaim_bytes=min_reclaim_bytes, skips=skips)
    )
    for cluster in resolved_clusters:
        member_results = {
            duplicate.path: safety.evaluate(duplicate) for duplicate in cluster.duplicates
        }
        if any(result.verdict == Verdict.BLOCKED for result in member_results.values()):
            blocked_paths = [
                str(path)
                for path, result in member_results.items()
                if result.verdict == Verdict.BLOCKED
            ]
            logger.info(
                "dedup.cluster_excluded_protected_member",
                keep=str(cluster.keep.path),
                blocked_paths=blocked_paths,
            )
            continue

        rationale = _keep_rationale(cluster)
        needs_review = cluster_needs_manual_review(cluster)
        if needs_review:
            rationale = (
                f"{rationale} FLAGGED FOR REVIEW: the kept copy sits in a Downloads/Temp/"
                "cloud-placeholder location while at least one deleted copy sat somewhere more "
                "durable — auto-applying this cluster would strand the sole survivor somewhere "
                "less durable than what was thrown away."
            )
        reclaim_estimates = estimate_reclaimable_bytes(
            [(duplicate.path, duplicate.size_bytes) for duplicate in cluster.duplicates]
        )
        for duplicate in cluster.duplicates:
            result = member_results[duplicate.path]
            if result.verdict == Verdict.REVIEW_ONLY or needs_review:
                tier = Tier.B
            else:
                tier = Tier.A if config.categories.duplicates.enabled else Tier.B
            estimate = reclaim_estimates[duplicate.path]
            member_rationale = rationale
            if estimate.resolved and estimate.reclaimable_bytes == 0:
                member_rationale = (
                    f"{member_rationale} Already deduplicated at the filesystem level (this "
                    "path is a hardlink sharing the same on-disk blocks as another surviving "
                    "copy) — 0 bytes reclaimable if deleted, excluded from the reclaimable "
                    "total."
                )
            candidates.append(
                Candidate(
                    path=duplicate.path,
                    is_dir=False,
                    category=_CATEGORY,
                    category_group=_CATEGORY_GROUP,
                    size_bytes=duplicate.size_bytes,
                    tier=tier,
                    rationale=member_rationale,
                    rebuild_instruction=None,
                    safety_verdict=result.verdict,
                    safety_reason_code=result.reason_code,
                    retention_days=config.categories.duplicates.retention_days,
                    reclaimable_bytes=estimate.reclaimable_bytes,
                )
            )
    return candidates
