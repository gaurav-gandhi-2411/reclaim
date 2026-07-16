from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path

import blake3
import structlog

from reclaim.config import Config
from reclaim.index import ScanIndex, cached_full_hash, cached_partial_hash
from reclaim.models import Candidate, DuplicateCluster, FileRecord, HashSkip, Tier, Verdict
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


def _is_downloads_or_temp(path: Path) -> bool:
    """Spec's keep-heuristic rule 1: "prefer copy outside Downloads/Temp"."""
    return any(part.lower() in _KEEP_HEURISTIC_LOCATION_SEGMENTS for part in path.parts)


def _group_by_size(records: Iterable[FileRecord]) -> dict[int, list[FileRecord]]:
    """Groups an already-narrowed iterable of records by `size_bytes`.

    Deliberately does no filtering itself (no `is_dir`/`size == 0`/singleton-bucket check):
    `records` is expected to already be `ScanIndex.duplicate_size_candidates()`'s output, which
    pushes that filtering into a SQL `GROUP BY size HAVING COUNT(*) >= 2` query — only files
    sharing a size with at least one other file are ever selected in the first place, so a
    unique-size file is never loaded into a Python `FileRecord` at all, let alone hashed. This
    replaced an earlier in-memory `_size_buckets(records: Sequence[FileRecord])` that received
    the *entire* inventory and filtered it in Python — correct, but on a real disk-scale index
    that meant materializing millions of rows before this function ever got to discard most of
    them.
    """
    buckets: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        buckets[record.size_bytes].append(record)
    return dict(buckets)


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
    """Picks the one cluster member to keep. Ranking, in order: (1) prefer a path not under a
    Downloads/Temp directory, (2) oldest `ctime` — Windows creation time, the "which copy
    existed first" signal, not POSIX change time — (3) shortest path depth (fewest path
    segments from the drive root), (4) lexicographic path sort as a final, deterministic
    tiebreak so output is reproducible run-to-run."""
    return min(
        members,
        key=lambda record: (
            _is_downloads_or_temp(record.path),
            record.ctime,
            len(record.path.parts),
            record.path.as_posix(),
        ),
    )


def find_duplicate_clusters(
    index: ScanIndex, *, skips: list[HashSkip] | None = None
) -> list[DuplicateCluster]:
    """Size bucket -> 64KB partial hash -> full BLAKE3 hash, exactly in that order, reusing
    cached hashes from a prior run wherever a file's (size, mtime) hasn't changed since.

    `skips` is an optional out-param (append-to list, default `None` = don't bother collecting)
    rather than a second return value, so existing callers (`generate_duplicate_candidates`,
    the API service layer, evals) keep working unchanged against the plain
    `list[DuplicateCluster]` return type; callers that care about the skipped/unreadable files
    pass their own list and read it back after the call.

    SQL-pushdown: `index.duplicate_size_candidates()` runs the whole "does this file's size
    collide with another file's size" prefilter as one indexed `GROUP BY`/`IN` query — a
    unique-size file is never loaded into a Python `FileRecord`, versus the earlier design that
    called `index.candidate_inventory()` (a full-table load of every row) and filtered in
    Python via `_size_buckets`.
    """
    size_groups = _group_by_size(index.duplicate_size_candidates())
    if not size_groups:
        return []

    hash_cache = index.load_hash_cache()
    total_partial_candidates = sum(len(members) for members in size_groups.values())
    logger.info(
        "dedup.partial_hash_start",
        size_groups=len(size_groups),
        candidate_files=total_partial_candidates,
    )

    partial_groups: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    partial_writes: list[tuple[Path, int, float, str]] = []
    hashed = 0
    last_heartbeat = time.monotonic()
    with ThreadPoolExecutor(max_workers=_HASH_TIMEOUT_WORKERS) as executor:
        for size, members in size_groups.items():
            for record in members:
                entry = hash_cache.get(record.path.as_posix())
                digest = cached_partial_hash(
                    entry, current_size=record.size_bytes, current_mtime=record.mtime
                )
                if digest is None:
                    digest, reason = _hash_with_guard(
                        executor, _compute_partial_hash, record.path, record.size_bytes
                    )
                    if digest is None:
                        logger.warning(
                            "dedup.hash_unreadable",
                            stage="partial",
                            path=str(record.path),
                            reason=reason,
                        )
                        if skips is not None:
                            skips.append(
                                HashSkip(path=record.path, stage="partial", reason=reason or "")
                            )
                        continue
                    partial_writes.append((record.path, record.size_bytes, record.mtime, digest))
                    if len(partial_writes) >= _WRITE_BATCH_SIZE:
                        index.store_partial_hashes(partial_writes)
                        partial_writes.clear()
                partial_groups[(size, digest)].append(record)
                hashed += 1
                now = time.monotonic()
                if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                    logger.info(
                        "dedup.progress",
                        stage="partial_hash",
                        hashed=hashed,
                        total=total_partial_candidates,
                    )
                    last_heartbeat = now
    if partial_writes:
        index.store_partial_hashes(partial_writes)

    survivors = {key: members for key, members in partial_groups.items() if len(members) >= 2}
    if not survivors:
        return []

    total_full_candidates = sum(len(members) for members in survivors.values())
    logger.info("dedup.full_hash_start", candidate_files=total_full_candidates)

    full_groups: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    full_writes: list[tuple[Path, int, float, str]] = []
    hashed = 0
    last_heartbeat = time.monotonic()
    with ThreadPoolExecutor(max_workers=_HASH_TIMEOUT_WORKERS) as executor:
        for members in survivors.values():
            for record in members:
                entry = hash_cache.get(record.path.as_posix())
                digest = cached_full_hash(
                    entry, current_size=record.size_bytes, current_mtime=record.mtime
                )
                if digest is None:
                    digest, reason = _hash_with_guard(executor, _compute_full_hash, record.path)
                    if digest is None:
                        logger.warning(
                            "dedup.hash_unreadable",
                            stage="full",
                            path=str(record.path),
                            reason=reason,
                        )
                        if skips is not None:
                            skips.append(
                                HashSkip(path=record.path, stage="full", reason=reason or "")
                            )
                        continue
                    full_writes.append((record.path, record.size_bytes, record.mtime, digest))
                    if len(full_writes) >= _WRITE_BATCH_SIZE:
                        index.store_full_hashes(full_writes)
                        full_writes.clear()
                full_groups[(record.size_bytes, digest)].append(record)
                hashed += 1
                now = time.monotonic()
                if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                    logger.info(
                        "dedup.progress",
                        stage="full_hash",
                        hashed=hashed,
                        total=total_full_candidates,
                    )
                    last_heartbeat = now
    if full_writes:
        index.store_full_hashes(full_writes)

    clusters: list[DuplicateCluster] = []
    for (size, full_hash), members in full_groups.items():
        if len(members) < 2:
            continue
        keep = select_keep(members)
        duplicates = tuple(m for m in members if m.path != keep.path)
        clusters.append(
            DuplicateCluster(full_hash=full_hash, size_bytes=size, keep=keep, duplicates=duplicates)
        )
    logger.info(
        "dedup.done", clusters=len(clusters), skipped=len(skips) if skips is not None else 0
    )
    return clusters


def _keep_rationale(cluster: DuplicateCluster) -> str:
    """Concrete, honest rationale naming the kept path and the factual heuristic-relevant
    properties that led to it being kept — never a fabricated claim about which single rule
    was decisive, since that depends on the other members it was compared against."""
    location = (
        "outside Downloads/Temp"
        if not _is_downloads_or_temp(cluster.keep.path)
        else ("under a Downloads/Temp directory (shared by every member of this cluster)")
    )
    created = datetime.fromtimestamp(cluster.keep.ctime, tz=UTC).isoformat()
    return (
        f"Exact duplicate of '{cluster.keep.path}' (byte-identical, BLAKE3 full-hash match); "
        f"kept copy is {location}, created {created}, path depth "
        f"{len(cluster.keep.path.parts)} — selected by the keep-heuristic (prefer outside "
        "Downloads/Temp, then oldest creation time, then shallowest path, then lexicographic "
        "order)."
    )


def generate_duplicate_candidates(
    index: ScanIndex,
    config: Config,
    safety: SafetyValidator,
    *,
    skips: list[HashSkip] | None = None,
) -> list[Candidate]:
    """Mirrors `detectors.py::generate_candidates()`'s contract/shape: runs every non-keep
    cluster member through `SafetyValidator.evaluate()` before it is ever tagged a tier.
    `BLOCKED` -> excluded entirely; `REVIEW_ONLY` -> forced Tier B; `ELIGIBLE` -> Tier A only if
    `config.categories.duplicates` is enabled, else Tier B. The kept member of a cluster is
    never evaluated and never appears in the output — it isn't being proposed for any action.

    `skips` is forwarded to `find_duplicate_clusters` unchanged — see its docstring.
    """
    candidates: list[Candidate] = []
    for cluster in find_duplicate_clusters(index, skips=skips):
        rationale = _keep_rationale(cluster)
        for duplicate in cluster.duplicates:
            result = safety.evaluate(duplicate)
            if result.verdict == Verdict.BLOCKED:
                continue
            if result.verdict == Verdict.REVIEW_ONLY:
                tier = Tier.B
            else:
                tier = Tier.A if config.categories.duplicates.enabled else Tier.B
            candidates.append(
                Candidate(
                    path=duplicate.path,
                    is_dir=False,
                    category=_CATEGORY,
                    category_group=_CATEGORY_GROUP,
                    size_bytes=duplicate.size_bytes,
                    tier=tier,
                    rationale=rationale,
                    rebuild_instruction=None,
                    safety_verdict=result.verdict,
                    safety_reason_code=result.reason_code,
                    retention_days=config.categories.duplicates.retention_days,
                )
            )
    return candidates
