from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
from reclaim.config import CategoriesConfig, Config, DuplicatesConfig, SafetyConfig
from reclaim.dedup import (
    _HEARTBEAT_INTERVAL_SECONDS,
    _PARTIAL_HASH_CHUNK_BYTES,
    _PARTIAL_HASH_WHOLE_FILE_THRESHOLD,
    _compute_full_hash,
    _compute_partial_hash,
    _due,
    _hash_with_guard,
    _is_downloads_or_temp,
    find_duplicate_clusters,
    generate_duplicate_candidates,
    materiality_exclusion_stats,
    select_keep,
)
from reclaim.index import ScanIndex
from reclaim.models import FileRecord, HashSkip
from reclaim.safety import SafetyValidator

_NOW = 1_700_000_000.0


def _record(
    path: str,
    *,
    is_dir: bool = False,
    size_bytes: int = 1024,
    ctime: float = _NOW,
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=0,
        ext=p.suffix.lower() if not is_dir else "",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=ctime,
        ctime=ctime,
    )


# --- Partial/full hash computation -----------------------------------------------------------


def test_partial_hash_whole_file_for_small_files(tmp_path: Path) -> None:
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(b"x" * 100)
    path_b.write_bytes(b"x" * 100)
    assert _compute_partial_hash(path_a, 100) == _compute_partial_hash(path_b, 100)


def test_partial_hash_differs_when_small_file_content_differs(tmp_path: Path) -> None:
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(b"x" * 100)
    path_b.write_bytes(b"y" * 100)
    assert _compute_partial_hash(path_a, 100) != _compute_partial_hash(path_b, 100)


def test_partial_hash_ignores_middle_content_for_large_files(tmp_path: Path) -> None:
    """The adversarial case at the partial-hash granularity: same first/last 64KB, different
    middle -> partial hash must be equal (full hash is what's supposed to disambiguate). The
    first/last chunks must be exactly `_PARTIAL_HASH_CHUNK_BYTES` (what `_compute_partial_hash`
    actually reads) or this test would silently include middle bytes in the "shared" region."""
    middle_len = 1000
    size = _PARTIAL_HASH_WHOLE_FILE_THRESHOLD + middle_len
    first = b"\xaa" * _PARTIAL_HASH_CHUNK_BYTES
    last = b"\xbb" * _PARTIAL_HASH_CHUNK_BYTES
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(first + b"\x01" * middle_len + last)
    path_b.write_bytes(first + b"\x02" * middle_len + last)
    assert _compute_partial_hash(path_a, size) == _compute_partial_hash(path_b, size)
    assert _compute_full_hash(path_a) != _compute_full_hash(path_b)


def test_partial_hash_at_exact_whole_file_threshold_matches_full_hash(tmp_path: Path) -> None:
    """At exactly 128KB, first-64KB + last-64KB covers the file with no overlap, so the
    whole-file-read branch and a hypothetical chunked read would agree — this pins down that
    the <= comparison lands on the whole-file branch at the boundary, not off-by-one."""
    size = _PARTIAL_HASH_WHOLE_FILE_THRESHOLD
    path = tmp_path / "boundary.bin"
    path.write_bytes(b"\x01" * size)
    assert _compute_partial_hash(path, size) == _compute_full_hash(path)


def test_full_hash_reads_entire_file_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(b"same-content")
    path_b.write_bytes(b"same-content")
    assert _compute_full_hash(path_a) == _compute_full_hash(path_b)


# --- Keep heuristic ---------------------------------------------------------------------------


def test_is_downloads_or_temp_matches_either_segment() -> None:
    assert _is_downloads_or_temp(Path("C:/Users/gg/Downloads/file.exe")) is True
    assert _is_downloads_or_temp(Path("C:/Users/gg/AppData/Local/Temp/file.tmp")) is True
    assert _is_downloads_or_temp(Path("C:/Users/gg/Documents/file.txt")) is False


def test_select_keep_prefers_path_outside_downloads_over_older_ctime() -> None:
    """Location beats ctime: a Downloads copy created earlier must still lose to a
    non-Downloads copy created later."""
    downloads = _record("C:/Users/gg/Downloads/file.bin", ctime=_NOW - 100)
    documents = _record("C:/Users/gg/Documents/file.bin", ctime=_NOW - 1)
    assert select_keep([downloads, documents]) is documents


def test_select_keep_falls_back_to_oldest_ctime_when_location_ties() -> None:
    older = _record("C:/Data/a/file.bin", ctime=_NOW - 100)
    newer = _record("C:/Data/b/file.bin", ctime=_NOW - 1)
    assert select_keep([older, newer]) is older


def test_select_keep_falls_back_to_shortest_depth_when_location_and_ctime_tie() -> None:
    shallow = _record("C:/Data/file.bin", ctime=_NOW)
    deep = _record("C:/Data/nested/deeper/file.bin", ctime=_NOW)
    assert select_keep([deep, shallow]) is shallow


def test_select_keep_falls_back_to_lexicographic_path_when_fully_tied() -> None:
    a = _record("C:/Data/a.bin", ctime=_NOW)
    b = _record("C:/Data/b.bin", ctime=_NOW)
    assert select_keep([b, a]) is a


def test_select_keep_is_order_independent() -> None:
    downloads = _record("C:/Users/gg/Downloads/file.bin", ctime=_NOW - 100)
    documents = _record("C:/Users/gg/Documents/file.bin", ctime=_NOW - 1)
    assert select_keep([downloads, documents]) is select_keep([documents, downloads])


# --- Observability / hang-guard regression tests -------------------------------------------
#
# Root cause of the real-disk-run stall (3.1M-file `C:\` scan): the hash pass had zero
# progress output and batched every DB write until the very end, so it was indistinguishable
# from a hang. These tests pin down the fix: (1) the heartbeat gate is a pure, fast-testable
# predicate rather than a real multi-second wait, (2) a per-file read timeout is enforced via
# a worker thread rather than blocking the caller indefinitely, and (3) the pipeline degrades
# to a recorded skip — never a crash or a wedge — on an unreadable file.


def test_due_is_false_before_the_interval_elapses() -> None:
    assert (
        _due(
            last=100.0,
            now=100.0 + _HEARTBEAT_INTERVAL_SECONDS - 0.001,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
        )
        is False
    )


def test_due_is_true_once_the_interval_elapses() -> None:
    assert (
        _due(
            last=100.0,
            now=100.0 + _HEARTBEAT_INTERVAL_SECONDS,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
        )
        is True
    )


def test_hash_with_guard_returns_digest_on_success() -> None:
    def _ok(path: Path, *args: object) -> str:
        return "digest"

    with ThreadPoolExecutor(max_workers=1) as executor:
        digest, reason = _hash_with_guard(executor, _ok, Path("C:/unused"))
    assert digest == "digest"
    assert reason is None


def test_hash_with_guard_reports_timeout_for_a_slow_read() -> None:
    """The hang guard itself: a read blocked past its timeout is abandoned (Python has no
    cross-platform way to preempt a blocked syscall) and reported as a timeout skip rather than
    hanging the caller — this is what the dedup loop relies on to never wedge on one bad file."""

    def _slow(path: Path, *args: object) -> str:
        time.sleep(0.2)
        return "unreachable"

    with ThreadPoolExecutor(max_workers=1) as executor:
        digest, reason = _hash_with_guard(executor, _slow, Path("C:/unused"), timeout_seconds=0.02)
    assert digest is None
    assert reason == "timeout"


def test_hash_with_guard_reports_oserror_reason() -> None:
    def _locked(path: Path, *args: object) -> str:
        raise PermissionError("[WinError 32] file in use by another process")

    with ThreadPoolExecutor(max_workers=1) as executor:
        digest, reason = _hash_with_guard(executor, _locked, Path("C:/unused"))
    assert digest is None
    assert reason == "[WinError 32] file in use by another process"


def _index_record(
    path: str, *, size_bytes: int, mtime: float = 100.0, ctime: float = 100.0
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=False,
        size_bytes=size_bytes,
        attributes=0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=ctime,
    )


def test_find_duplicate_clusters_never_hashes_unique_size_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression fixture for the stall: every file below has a unique size, so the
    size-uniqueness prefilter must exclude all of them before any hashing is attempted — hashing
    files with no possible duplicate is exactly the wasted, silent work that made a 3.1M-file
    scan look hung."""

    def _must_not_be_called(*args: object, **kwargs: object) -> str:
        raise AssertionError("hash function called on a unique-size file")

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", _must_not_be_called)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", _must_not_be_called)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                _index_record("C:/Data/a.bin", size_bytes=100),
                _index_record("C:/Data/b.bin", size_bytes=200),
                _index_record("C:/Data/c.bin", size_bytes=300),
            ],
            scanned_at=1000.0,
        )
        clusters = find_duplicate_clusters(index)

    assert clusters == []


def test_find_duplicate_clusters_skips_locked_file_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size pair where one member raises `PermissionError` on read (simulating a
    locked/in-use file) must be recorded as a skip and excluded from clustering — 'one bad file
    must never wedge the whole scan' — not crash the whole dedup run."""
    locked_path = Path("C:/Data/locked.bin")

    def _partial_hash_with_lock(path: Path, size_bytes: int) -> str:
        if path == locked_path:
            raise PermissionError("[WinError 32] file in use by another process")
        return "ok-digest"

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", _partial_hash_with_lock)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                _index_record("C:/Data/ok.bin", size_bytes=100),
                _index_record(str(locked_path), size_bytes=100),
            ],
            scanned_at=1000.0,
        )
        skips: list[HashSkip] = []
        # min_reclaim_bytes=0: this test is about the hang guard, not materiality gating.
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0, skips=skips)

    # The one surviving (successfully hashed) member has no partner left to cluster with.
    assert clusters == []
    assert len(skips) == 1
    assert skips[0].path == locked_path
    assert skips[0].stage == "partial"
    assert "file in use" in skips[0].reason


def test_find_duplicate_clusters_default_skips_param_collects_nothing(tmp_path: Path) -> None:
    """Callers that don't pass `skips=` (every pre-existing caller) must keep working exactly
    as before — the out-param is additive, not a breaking change to the return contract."""
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [_index_record("C:/Data/a.bin", size_bytes=100)],
            scanned_at=1000.0,
        )
        clusters = find_duplicate_clusters(index)
    assert clusters == []


# --- Materiality gate: 2026-07-17 real-disk finding ------------------------------------------
#
# The 80%-size-collision finding on the real disk was dominated by tiny/near-empty files (333K
# zero-byte, thousands of 2/4/17-byte files) whose full bucket could never reclaim anything
# material even in the best case, and for files under the partial-hash whole-file threshold
# (128KB) a "partial" hash reads the entire file anyway — no cheap-peek advantage at all. These
# tests prove the materiality gate keeps such buckets from ever reaching a hash function.


def test_find_duplicate_clusters_never_hashes_immaterial_buckets_but_finds_real_large_dups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large empty-file cohort, a tiny immaterial duplicate-size bucket, and a real large
    (2MB) duplicate pair in the same index: only the large pair may ever reach a hash
    function."""
    hashed_paths: list[Path] = []

    def _tracking_hash(path: Path, *args: object) -> str:
        hashed_paths.append(path)
        return "shared-digest" if "large" in path.name else f"unique-{path.name}"

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", _tracking_hash)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", _tracking_hash)

    records = [
        # A large empty-file cohort: size=0 is excluded outright (regardless of materiality).
        *(_index_record(f"C:/Data/empty_{i}.bin", size_bytes=0) for i in range(200)),
        # A tiny duplicate-size bucket: (5 - 1) * 8 = 32 bytes theoretical -- immaterial at a
        # 1MB floor even though all 5 genuinely share a size.
        *(_index_record(f"C:/Data/tiny_{i}.bin", size_bytes=8) for i in range(5)),
        # A real duplicate pair large enough to clear the materiality floor.
        _index_record("C:/Data/large_a.bin", size_bytes=2 * 1024 * 1024),
        _index_record("C:/Data/large_b.bin", size_bytes=2 * 1024 * 1024),
    ]
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(records, scanned_at=1000.0)
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=1024 * 1024)

    # Both the partial- and full-hash stages call the (mocked) hash function once per member,
    # so the two large files appear twice each here -- the point is that only they appear at
    # all, never any of the 200 empty files or the 5 tiny immaterial-bucket files.
    assert set(hashed_paths) == {Path("C:/Data/large_a.bin"), Path("C:/Data/large_b.bin")}
    assert len(hashed_paths) == 4
    assert len(clusters) == 1
    assert clusters[0].size_bytes == 2 * 1024 * 1024
    assert len(clusters[0].duplicates) == 1


def test_materiality_exclusion_stats_matches_find_duplicate_clusters_behavior(
    tmp_path: Path,
) -> None:
    """`materiality_exclusion_stats` (the reporting counterpart) must agree with what
    `find_duplicate_clusters` actually excluded, for the same `min_reclaim_bytes`."""
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                *(_index_record(f"C:/Data/tiny_{i}.bin", size_bytes=8) for i in range(5)),
                _index_record("C:/Data/large_a.bin", size_bytes=2 * 1024 * 1024),
                _index_record("C:/Data/large_b.bin", size_bytes=2 * 1024 * 1024),
            ],
            scanned_at=1000.0,
        )
        stats = materiality_exclusion_stats(index, min_reclaim_bytes=1024 * 1024)

    assert stats.excluded_bucket_count == 1
    assert stats.theoretical_bytes == 32  # (5 - 1) * 8 bytes


def test_materiality_exclusion_stats_default_matches_config_default() -> None:
    """The dedup-module default must match `DuplicatesConfig.min_reclaim_bytes`'s default, so a
    caller invoking either without a config (evals, direct API service calls) gets the same
    real-world-tuned floor, not a silent "hash everything" fallback."""
    assert DuplicatesConfig().min_reclaim_bytes == dedup_module._DEFAULT_MIN_RECLAIM_BYTES


# --- ADR-0006: hardlink-aware reclaimable-bytes estimation for exact_duplicate -----------------


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_generate_duplicate_candidates_flags_hardlinked_duplicate_as_zero_reclaimable(
    tmp_path: Path,
) -> None:
    """The exact required scenario: a same-inode 'duplicate' pair must report 0 reclaimable
    bytes, not its full logical size — byte-identical content, the cluster's whole selection
    criterion, is exactly what a hardlink produces, so this is not a rare edge case here."""
    kept_path = tmp_path / "kept.bin"
    kept_path.write_bytes(b"identical-content-" * 10_000)  # clears the default materiality gate
    linked_path = tmp_path / "hardlinked_duplicate.bin"
    os.link(kept_path, linked_path)
    size = kept_path.stat().st_size

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                _index_record(str(kept_path), size_bytes=size, ctime=100.0),
                _index_record(str(linked_path), size_bytes=size, ctime=200.0),  # newer -> not kept
            ],
            scanned_at=1000.0,
        )
        config = Config(
            safety=SafetyConfig(protected_roots=[]),
            categories=CategoriesConfig(duplicates=DuplicatesConfig(min_reclaim_bytes=0)),
        )
        candidates = generate_duplicate_candidates(index, config, SafetyValidator(config))

    assert len(candidates) == 1
    assert candidates[0].path == linked_path
    assert candidates[0].size_bytes == size  # logical size still reported in full
    assert candidates[0].reclaimable_bytes == 0
    assert "hardlink" in candidates[0].rationale.lower()


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_generate_duplicate_candidates_reports_full_reclaimable_for_genuine_copies(
    tmp_path: Path,
) -> None:
    """Contrast case: two genuinely separate files with identical content (not hardlinked) must
    still report the full logical size as reclaimable — the hardlink check must never
    under-credit an ordinary duplicate pair that really is two independent copies on disk."""
    kept_path = tmp_path / "kept.bin"
    kept_path.write_bytes(b"identical-content-" * 10_000)
    other_path = tmp_path / "genuine_copy.bin"
    other_path.write_bytes(kept_path.read_bytes())  # same content, separate inode
    size = kept_path.stat().st_size

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [
                _index_record(str(kept_path), size_bytes=size, ctime=100.0),
                _index_record(str(other_path), size_bytes=size, ctime=200.0),
            ],
            scanned_at=1000.0,
        )
        config = Config(
            safety=SafetyConfig(protected_roots=[]),
            categories=CategoriesConfig(duplicates=DuplicatesConfig(min_reclaim_bytes=0)),
        )
        candidates = generate_duplicate_candidates(index, config, SafetyValidator(config))

    assert len(candidates) == 1
    assert candidates[0].reclaimable_bytes == size
