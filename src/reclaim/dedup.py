from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import blake3

from reclaim.config import Config
from reclaim.index import ScanIndex, cached_full_hash, cached_partial_hash
from reclaim.models import Candidate, DuplicateCluster, FileRecord, Tier, Verdict
from reclaim.safety import SafetyValidator

_PARTIAL_HASH_CHUNK_BYTES = 64 * 1024
# Files at or below this size are hashed whole in one read rather than as two 64KB chunks —
# reading first+last 64KB of a <=128KB file would double-read overlapping bytes (spec: "don't
# double-read overlapping regions").
_PARTIAL_HASH_WHOLE_FILE_THRESHOLD = 2 * _PARTIAL_HASH_CHUNK_BYTES
_FULL_HASH_READ_CHUNK_BYTES = 1024 * 1024

_KEEP_HEURISTIC_LOCATION_SEGMENTS = frozenset({"downloads", "temp"})

_CATEGORY = "exact_duplicate"
_CATEGORY_GROUP = "duplicates"


def _is_downloads_or_temp(path: Path) -> bool:
    """Spec's keep-heuristic rule 1: "prefer copy outside Downloads/Temp"."""
    return any(part.lower() in _KEEP_HEURISTIC_LOCATION_SEGMENTS for part in path.parts)


def _size_buckets(records: Sequence[FileRecord]) -> dict[int, list[FileRecord]]:
    """Groups non-directory, non-empty files by `size_bytes`, dropping singleton buckets —
    only files sharing a size with at least one other file are ever hash candidates, which is
    what keeps the pipeline from hashing the whole index. 0-byte files are skipped entirely:
    deduping them reclaims no space."""
    buckets: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.is_dir or record.size_bytes == 0:
            continue
        buckets[record.size_bytes].append(record)
    return {size: members for size, members in buckets.items() if len(members) >= 2}


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


def find_duplicate_clusters(index: ScanIndex) -> list[DuplicateCluster]:
    """Size bucket -> 64KB partial hash -> full BLAKE3 hash, exactly in that order, reusing
    cached hashes from a prior run wherever a file's (size, mtime) hasn't changed since."""
    records = index.candidate_inventory()
    size_groups = _size_buckets(records)
    if not size_groups:
        return []

    hash_cache = index.load_hash_cache()

    partial_groups: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    partial_writes: list[tuple[Path, int, float, str]] = []
    for size, members in size_groups.items():
        for record in members:
            entry = hash_cache.get(record.path.as_posix())
            digest = cached_partial_hash(
                entry, current_size=record.size_bytes, current_mtime=record.mtime
            )
            if digest is None:
                digest = _compute_partial_hash(record.path, record.size_bytes)
                partial_writes.append((record.path, record.size_bytes, record.mtime, digest))
            partial_groups[(size, digest)].append(record)
    if partial_writes:
        index.store_partial_hashes(partial_writes)

    survivors = {key: members for key, members in partial_groups.items() if len(members) >= 2}
    if not survivors:
        return []

    full_groups: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
    full_writes: list[tuple[Path, int, float, str]] = []
    for members in survivors.values():
        for record in members:
            entry = hash_cache.get(record.path.as_posix())
            digest = cached_full_hash(
                entry, current_size=record.size_bytes, current_mtime=record.mtime
            )
            if digest is None:
                digest = _compute_full_hash(record.path)
                full_writes.append((record.path, record.size_bytes, record.mtime, digest))
            full_groups[(record.size_bytes, digest)].append(record)
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
    index: ScanIndex, config: Config, safety: SafetyValidator
) -> list[Candidate]:
    """Mirrors `detectors.py::generate_candidates()`'s contract/shape: runs every non-keep
    cluster member through `SafetyValidator.evaluate()` before it is ever tagged a tier.
    `BLOCKED` -> excluded entirely; `REVIEW_ONLY` -> forced Tier B; `ELIGIBLE` -> Tier A only if
    `config.categories.duplicates` is enabled, else Tier B. The kept member of a cluster is
    never evaluated and never appears in the output — it isn't being proposed for any action.
    """
    candidates: list[Candidate] = []
    for cluster in find_duplicate_clusters(index):
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
