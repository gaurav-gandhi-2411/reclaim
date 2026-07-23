from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from reclaim.index import (
    ScanIndex,
    StoredStat,
    is_unchanged,
    logical_size_bytes,
    physical_size_bytes,
)
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FileRecord


def _record(
    path: str,
    *,
    is_dir: bool = False,
    size_bytes: int = 1024,
    attributes: int = 0,
    dev: int = 0,
    ino: int = 0,
    mtime: float = 100.0,
    ctime: float = 100.0,
    git_repo_root: Path | None = None,
    git_repo_clean: bool = False,
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=attributes,
        ext=p.suffix.lower(),
        git_repo_root=git_repo_root,
        git_repo_clean=git_repo_clean,
        mtime=mtime,
        ctime=ctime,
        dev=dev,
        ino=ino,
    )


@pytest.fixture
def index(tmp_path: Path) -> Iterator[ScanIndex]:
    idx = ScanIndex(tmp_path / "index.sqlite3")
    yield idx
    idx.close()


def test_upsert_and_full_inventory_roundtrip(index: ScanIndex) -> None:
    records = [_record("C:/Data/a.txt"), _record("C:/Data/b.txt", is_dir=True)]
    written = index.upsert_records(records, scanned_at=1000.0)
    assert written == 2

    inventory = index.full_inventory()
    assert {r.path for r in inventory} == {Path("C:/Data/a.txt"), Path("C:/Data/b.txt")}
    by_path = {r.path: r for r in inventory}
    assert by_path[Path("C:/Data/b.txt")].is_dir is True


def test_upsert_overwrites_on_conflict(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/a.txt", size_bytes=10)], scanned_at=1.0)
    index.upsert_records([_record("C:/Data/a.txt", size_bytes=999)], scanned_at=2.0)
    inventory = index.full_inventory()
    assert len(inventory) == 1
    assert inventory[0].size_bytes == 999


def test_candidate_inventory_excludes_cloud_placeholders_full_inventory_includes_them(
    index: ScanIndex,
) -> None:
    """The unit test required by the brief: placeholders must never appear in the
    candidate-eligible query, but must still appear in the full (treemap/total-usage)
    inventory since they occupy no local space but still count toward cloud-footprint size.
    """
    placeholder = _record("C:/OneDrive/photo.jpg", attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    normal = _record("C:/OneDrive/notes.txt")
    index.upsert_records([placeholder, normal], scanned_at=1.0)

    candidates = index.candidate_inventory()
    full = index.full_inventory()

    assert {r.path for r in full} == {placeholder.path, normal.path}
    assert {r.path for r in candidates} == {normal.path}
    assert all(not r.is_cloud_placeholder for r in candidates)


def test_inventory_under_filters_by_prefix(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/keep/a.txt"), _record("C:/Other/b.txt")], scanned_at=1.0)
    scoped = index.full_inventory(under=Path("C:/Data"))
    assert {r.path for r in scoped} == {Path("C:/Data/keep/a.txt")}


def test_prune_missing_removes_rows_not_in_seen_paths(index: ScanIndex) -> None:
    index.upsert_records(
        [_record("C:/Data/stale.txt"), _record("C:/Data/kept.txt")], scanned_at=1.0
    )
    # Simulate a rescan of C:/Data where stale.txt no longer exists on disk: it never makes
    # it into `seen_paths` (what the scan actually walked this time).
    indexed = index.load_stat_cache(Path("C:/Data")).keys()
    pruned = index.prune_missing(indexed, seen_paths=["C:/Data/kept.txt"])
    assert pruned == 1
    remaining = {r.path for r in index.full_inventory()}
    assert remaining == {Path("C:/Data/kept.txt")}


def test_prune_missing_no_op_when_nothing_stale(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/a.txt")], scanned_at=1.0)
    indexed = index.load_stat_cache(Path("C:/Data")).keys()
    pruned = index.prune_missing(indexed, seen_paths=["C:/Data/a.txt"])
    assert pruned == 0
    assert {r.path for r in index.full_inventory()} == {Path("C:/Data/a.txt")}


@pytest.mark.parametrize(
    ("stored", "size", "mtime", "expected"),
    [
        (None, 10, 5.0, False),
        (StoredStat(size=10, mtime=5.0), 10, 5.0, True),
        (StoredStat(size=10, mtime=5.0), 11, 5.0, False),
        (StoredStat(size=10, mtime=5.0), 10, 5.1, False),
    ],
)
def test_is_unchanged(stored: StoredStat | None, size: int, mtime: float, expected: bool) -> None:
    assert is_unchanged(stored, current_size=size, current_mtime=mtime) is expected


def test_logical_size_double_counts_hardlinks() -> None:
    records = [
        _record("C:/Data/a.txt", size_bytes=100, dev=1, ino=5),
        _record("C:/Data/b_hardlink.txt", size_bytes=100, dev=1, ino=5),
    ]
    assert logical_size_bytes(records) == 200


def test_physical_size_counts_hardlink_pair_once() -> None:
    records = [
        _record("C:/Data/a.txt", size_bytes=100, dev=1, ino=5),
        _record("C:/Data/b_hardlink.txt", size_bytes=100, dev=1, ino=5),
        _record("C:/Data/unrelated.txt", size_bytes=50, dev=1, ino=6),
    ]
    assert physical_size_bytes(records) == 150


def test_physical_size_ignores_directories() -> None:
    records = [
        _record("C:/Data", is_dir=True, size_bytes=4096, dev=1, ino=1),
        _record("C:/Data/a.txt", size_bytes=100, dev=1, ino=5),
    ]
    assert physical_size_bytes(records) == 100
    assert logical_size_bytes(records) == 100


def test_physical_size_does_not_dedup_zero_sentinel_dev_ino() -> None:
    """Records without real dev/ino (dev=ino=0, the FileRecord default) must never be treated
    as sharing a hardlink allocation just because they share the unset sentinel."""
    records = [
        _record("C:/Data/a.txt", size_bytes=100),
        _record("C:/Data/b.txt", size_bytes=200),
    ]
    assert physical_size_bytes(records) == 300


# --- Windows unsigned 64-bit dev/ino overflow (ReFS volumes, dev drives, GitHub's own Windows
# CI runners): `os.stat().st_ino`/`st_dev` can exceed SQLite's signed 64-bit INTEGER max
# (2**63 - 1), which used to raise `OverflowError: Python int too large to convert to SQLite
# INTEGER` and abort the entire scan. `_to_db_int64`/`_from_db_int64` (index.py) wrap/unwrap via
# two's-complement at the DB boundary; these tests go through the real `ScanIndex` store path,
# never calling the private helpers directly.


def test_huge_unsigned_ino_and_dev_roundtrip_through_real_store_without_overflow(
    index: ScanIndex,
) -> None:
    """A file identity at the very top of the unsigned 64-bit range (`2**64 - 1`, the maximum
    `st_ino`/`st_dev` a real Windows volume can report) must store and read back without
    raising, and must round-trip to the exact original value."""
    huge = 2**64 - 1
    index.upsert_records([_record("C:/Data/a.txt", dev=huge, ino=huge)], scanned_at=1000.0)

    record = index.get_record(Path("C:/Data/a.txt"))

    assert record is not None
    assert record.dev == huge
    assert record.ino == huge


def test_ino_at_signed_int64_max_boundary_roundtrips_unchanged(index: ScanIndex) -> None:
    """`2**63 - 1` is SQLite's signed INTEGER max — the exact boundary where no wraparound is
    needed at all. Confirms the boundary itself (not just values above it) round-trips."""
    boundary = 2**63 - 1
    index.upsert_records([_record("C:/Data/a.txt", dev=1, ino=boundary)], scanned_at=1000.0)

    record = index.get_record(Path("C:/Data/a.txt"))

    assert record is not None
    assert record.ino == boundary


def test_ino_one_past_signed_int64_max_wraps_and_roundtrips(index: ScanIndex) -> None:
    """`2**63` is the first unsigned value that does NOT fit in a signed 64-bit INTEGER and
    must be wrapped — the boundary case on the other side of `test_ino_at_signed_int64_max_
    boundary_roundtrips_unchanged`."""
    just_over = 2**63
    index.upsert_records([_record("C:/Data/a.txt", dev=1, ino=just_over)], scanned_at=1000.0)

    record = index.get_record(Path("C:/Data/a.txt"))

    assert record is not None
    assert record.ino == just_over


def test_hardlink_dedup_groups_records_sharing_a_huge_dev_ino_pair(index: ScanIndex) -> None:
    """Two records sharing an out-of-signed-range `(dev, ino)` pair (a real hardlink pair on a
    volume with huge file IDs) must still be deduped to a single physical allocation — proves
    the wraparound preserves EQUALITY end-to-end (store -> read -> compare), not just successful
    storage."""
    huge_dev, huge_ino = 2**64 - 1, 2**64 - 2
    index.upsert_records(
        [
            _record("C:/Data/a.txt", size_bytes=100, dev=huge_dev, ino=huge_ino),
            _record("C:/Data/b_hardlink.txt", size_bytes=100, dev=huge_dev, ino=huge_ino),
        ],
        scanned_at=1000.0,
    )

    assert physical_size_bytes(index.full_inventory()) == 100


def test_hardlink_dedup_separates_records_with_different_huge_ino_values(
    index: ScanIndex,
) -> None:
    """Two records with distinct huge `ino` values (same huge `dev`) must NOT be collapsed
    together — the wraparound must preserve DISTINCTNESS as well as equality."""
    huge_dev = 2**64 - 1
    index.upsert_records(
        [
            _record("C:/Data/a.txt", size_bytes=100, dev=huge_dev, ino=2**64 - 2),
            _record("C:/Data/b.txt", size_bytes=100, dev=huge_dev, ino=2**64 - 3),
        ],
        scanned_at=1000.0,
    )

    assert physical_size_bytes(index.full_inventory()) == 200


# --- Stage 6 additions: has_any_records / direct_children -----------------------------------


def test_has_any_records_false_on_empty_index(index: ScanIndex) -> None:
    assert index.has_any_records() is False


def test_has_any_records_true_after_upsert(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/a.txt")], scanned_at=1000.0)
    assert index.has_any_records() is True


def test_direct_children_returns_only_one_level_down(index: ScanIndex) -> None:
    records = [
        _record("C:/Data", is_dir=True),
        _record("C:/Data/a.txt"),
        _record("C:/Data/Sub", is_dir=True),
        _record("C:/Data/Sub/nested.txt"),
        _record("C:/Other/b.txt"),
    ]
    index.upsert_records(records, scanned_at=1000.0)

    children = index.direct_children(Path("C:/Data"))
    assert {r.path for r in children} == {Path("C:/Data/a.txt"), Path("C:/Data/Sub")}


def test_direct_children_empty_for_leaf_directory(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data", is_dir=True)], scanned_at=1000.0)
    assert index.direct_children(Path("C:/Data")) == []


# --- SQL-pushdown query methods: correctness --------------------------------------------------
#
# These replaced `detectors.py`/`dedup.py` calling `candidate_inventory()` (a full-table load)
# and filtering in Python. Correctness first, EXPLAIN QUERY PLAN second (below) — a query that
# uses an index but returns the wrong rows is not a fix.


def test_get_record_returns_the_record_at_an_exact_path(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/a.txt", size_bytes=42)], scanned_at=1000.0)
    record = index.get_record(Path("C:/Data/a.txt"))
    assert record is not None
    assert record.size_bytes == 42


def test_get_record_returns_none_for_a_missing_path(index: ScanIndex) -> None:
    assert index.get_record(Path("C:/Data/missing.txt")) is None


def test_record_exists_true_and_false(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Data/a.txt")], scanned_at=1000.0)
    assert index.record_exists(Path("C:/Data/a.txt")) is True
    assert index.record_exists(Path("C:/Data/missing.txt")) is False


def test_files_by_name_matches_case_insensitively(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Proj/Node_Modules", is_dir=True),
            _record("C:/Proj/other", is_dir=True),
        ],
        scanned_at=1000.0,
    )
    matches = {r.path for r in index.files_by_name(["node_modules"])}
    assert matches == {Path("C:/Proj/Node_Modules")}


def test_files_by_name_respects_is_dir_filter(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Proj/build", is_dir=True),
            _record("C:/Proj/other/build"),  # a *file* named "build"
        ],
        scanned_at=1000.0,
    )
    matches = {r.path for r in index.files_by_name(["build"], is_dir=True)}
    assert matches == {Path("C:/Proj/build")}


def test_files_by_name_empty_names_yields_nothing(index: ScanIndex) -> None:
    index.upsert_records([_record("C:/Proj/build", is_dir=True)], scanned_at=1000.0)
    assert list(index.files_by_name([])) == []


def test_files_by_ext_matches_case_insensitively(index: ScanIndex) -> None:
    index.upsert_records(
        [_record("C:/App/app.DMP"), _record("C:/App/notes.txt")], scanned_at=1000.0
    )
    matches = {r.path for r in index.files_by_ext([".dmp"])}
    assert matches == {Path("C:/App/app.DMP")}


def test_files_larger_than_uses_at_least_semantics(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Data/small.bin", size_bytes=99),
            _record("C:/Data/exact.bin", size_bytes=100),
            _record("C:/Data/big.bin", size_bytes=101),
        ],
        scanned_at=1000.0,
    )
    matches = {r.path for r in index.files_larger_than(100)}
    assert matches == {Path("C:/Data/exact.bin"), Path("C:/Data/big.bin")}


def test_files_matching_path_pattern_translates_glob_wildcards(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache"),
            _record("C:/Users/gg/Documents/notes.txt"),
        ],
        scanned_at=1000.0,
    )
    matches = {
        r.path
        for r in index.files_matching_path_pattern(
            "C:/Users/gg/AppData/Local/Google/Chrome/User Data/*/Cache"
        )
    }
    assert matches == {Path("C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache")}


def test_duplicate_size_candidates_excludes_unique_sizes_dirs_zero_and_placeholders(
    index: ScanIndex,
) -> None:
    index.upsert_records(
        [
            _record("C:/Data/dup_a.bin", size_bytes=100),
            _record("C:/Data/dup_b.bin", size_bytes=100),
            _record("C:/Data/unique.bin", size_bytes=999),
            _record("C:/Data/emptydir", is_dir=True, size_bytes=100),
            _record("C:/Data/zero_a.bin", size_bytes=0),
            _record("C:/Data/zero_b.bin", size_bytes=0),
            _record(
                "C:/OneDrive/placeholder.bin",
                size_bytes=100,
                attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            ),
        ],
        scanned_at=1000.0,
    )
    # min_reclaim_bytes=0: this test is about the unique-size/dir/zero/placeholder filters,
    # not the materiality gate (covered separately below) — disable it here.
    matches = {r.path for r in index.duplicate_size_candidates(min_reclaim_bytes=0)}
    assert matches == {Path("C:/Data/dup_a.bin"), Path("C:/Data/dup_b.bin")}


def test_duplicate_size_candidates_materiality_gate_excludes_low_value_buckets(
    index: ScanIndex,
) -> None:
    """A bucket whose theoretical best-case reclaim — (member_count - 1) * size — falls below
    `min_reclaim_bytes` is excluded entirely, even though it has >= 2 same-size members and
    would otherwise qualify. Regression test for the real-disk finding: the collision list was
    dominated by tiny/near-empty files (333K zero-byte, thousands of 2/4/17-byte files) whose
    full bucket could never reclaim anything material."""
    index.upsert_records(
        [
            # 3 members * 100 bytes: (3-1)*100 = 200 bytes theoretical -- immaterial at a 1MB floor.
            _record("C:/Data/tiny_a.bin", size_bytes=100),
            _record("C:/Data/tiny_b.bin", size_bytes=100),
            _record("C:/Data/tiny_c.bin", size_bytes=100),
            # 2 members * 2MB: (2-1)*2MB = 2MB theoretical -- material at a 1MB floor.
            _record("C:/Data/large_a.bin", size_bytes=2 * 1024 * 1024),
            _record("C:/Data/large_b.bin", size_bytes=2 * 1024 * 1024),
        ],
        scanned_at=1000.0,
    )
    matches = {r.path for r in index.duplicate_size_candidates(min_reclaim_bytes=1024 * 1024)}
    assert matches == {Path("C:/Data/large_a.bin"), Path("C:/Data/large_b.bin")}


def test_immaterial_duplicate_bucket_stats_reports_excluded_buckets(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Data/tiny_a.bin", size_bytes=100),
            _record("C:/Data/tiny_b.bin", size_bytes=100),
            _record("C:/Data/tiny_c.bin", size_bytes=100),
            _record("C:/Data/large_a.bin", size_bytes=2 * 1024 * 1024),
            _record("C:/Data/large_b.bin", size_bytes=2 * 1024 * 1024),
        ],
        scanned_at=1000.0,
    )
    bucket_count, theoretical_bytes = index.immaterial_duplicate_bucket_stats(
        min_reclaim_bytes=1024 * 1024
    )
    assert bucket_count == 1  # only the tiny_* bucket is excluded
    assert theoretical_bytes == 200  # (3 - 1) * 100 bytes


def test_immaterial_duplicate_bucket_stats_empty_when_nothing_excluded(index: ScanIndex) -> None:
    index.upsert_records(
        [
            _record("C:/Data/large_a.bin", size_bytes=2 * 1024 * 1024),
            _record("C:/Data/large_b.bin", size_bytes=2 * 1024 * 1024),
        ],
        scanned_at=1000.0,
    )
    bucket_count, theoretical_bytes = index.immaterial_duplicate_bucket_stats(
        min_reclaim_bytes=1024 * 1024
    )
    assert bucket_count == 0
    assert theoretical_bytes == 0


# --- Migration: name/path_lower backfill for a pre-existing (pre-schema-change) index --------


def test_pre_existing_index_without_name_columns_gets_migrated_and_backfilled(
    tmp_path: Path,
) -> None:
    """Simulates opening an index created before `name`/`path_lower` existed: the old-shape
    `CREATE TABLE` is built by hand here (mirroring the pre-migration schema), then `ScanIndex`
    is opened against it and must add the columns and backfill every row without needing a
    `--full` rescan."""
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL,
            ctime REAL NOT NULL, ext TEXT NOT NULL, attributes INTEGER NOT NULL,
            dev INTEGER NOT NULL, ino INTEGER NOT NULL, is_dir INTEGER NOT NULL,
            is_cloud_placeholder INTEGER NOT NULL, is_reparse_point INTEGER NOT NULL,
            git_repo_root TEXT, git_repo_clean INTEGER NOT NULL, last_scanned REAL NOT NULL,
            hash_size INTEGER, hash_mtime REAL, partial_hash TEXT, full_hash TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO files (path,size,mtime,ctime,ext,attributes,dev,ino,is_dir,"
        "is_cloud_placeholder,is_reparse_point,git_repo_root,git_repo_clean,last_scanned) "
        "VALUES ('C:/Proj/Node_Modules',0,1.0,1.0,'',0,0,0,1,0,0,NULL,0,1.0)"
    )
    conn.commit()
    conn.close()

    with ScanIndex(db_path) as index:
        cols = {row["name"] for row in index._conn.execute("PRAGMA table_info(files)")}
        assert "name" in cols
        assert "path_lower" in cols
        matches = {r.path for r in index.files_by_name(["node_modules"], is_dir=True)}
        assert matches == {Path("C:/Proj/Node_Modules")}


# --- EXPLAIN QUERY PLAN: hot detector/dedup queries must hit an index, never a full scan -----
#
# Uses `sqlite3.Connection.set_trace_callback` to capture the *exact*, fully-expanded SQL text
# a real `ScanIndex` method issues, then re-runs that same text prefixed with
# `EXPLAIN QUERY PLAN`. This can never drift from production: it inspects the literal query the
# method actually sent to SQLite, not a hand-copied duplicate of it.


def _captured_sql(index: ScanIndex, action: Callable[[], None]) -> str:
    captured: list[str] = []
    index._conn.set_trace_callback(captured.append)
    try:
        action()
    finally:
        index._conn.set_trace_callback(None)
    assert captured, "action executed no SQL to capture"
    return captured[-1]


def _assert_query_uses_index(index: ScanIndex, sql: str) -> None:
    plan_rows = index._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    plan_text = " | ".join(str(tuple(row)) for row in plan_rows)
    assert "SCAN" not in plan_text, f"expected no full scan, got: {plan_text}"
    assert "SEARCH" in plan_text and "USING" in plan_text and "INDEX" in plan_text, plan_text


@pytest.fixture
def indexed_bulk(tmp_path: Path) -> Iterator[ScanIndex]:
    """A few thousand rows so SQLite's query planner has a real reason to prefer an index over
    a scan (on a near-empty table the planner may reasonably pick either)."""
    idx = ScanIndex(tmp_path / "bulk.sqlite3")
    records = [
        _record(f"C:/Data/dir{i % 50}/file{i}.bin", size_bytes=100 if i % 37 == 0 else i + 1000)
        for i in range(3000)
    ]
    idx.upsert_records(records, scanned_at=1000.0)
    yield idx
    idx.close()


def test_get_record_query_plan_uses_primary_key_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: indexed_bulk.get_record(Path("C:/Data/dir1/file1.bin"))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_record_exists_query_plan_uses_primary_key_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: indexed_bulk.record_exists(Path("C:/Data/dir1/file1.bin"))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_files_by_name_query_plan_uses_name_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(indexed_bulk, lambda: list(indexed_bulk.files_by_name(["file1.bin"])))
    _assert_query_uses_index(indexed_bulk, sql)


def test_files_by_ext_query_plan_uses_ext_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(indexed_bulk, lambda: list(indexed_bulk.files_by_ext([".bin"])))
    _assert_query_uses_index(indexed_bulk, sql)


def test_files_larger_than_query_plan_uses_size_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(indexed_bulk, lambda: list(indexed_bulk.files_larger_than(500)))
    _assert_query_uses_index(indexed_bulk, sql)


def test_files_matching_path_pattern_query_plan_uses_path_lower_index(
    indexed_bulk: ScanIndex,
) -> None:
    sql = _captured_sql(
        indexed_bulk,
        lambda: list(indexed_bulk.files_matching_path_pattern("C:/Data/dir1/*")),
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_duplicate_size_candidates_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: list(indexed_bulk.duplicate_size_candidates(min_reclaim_bytes=0))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_duplicate_size_candidates_materiality_gated_query_plan_uses_an_index(
    indexed_bulk: ScanIndex,
) -> None:
    """Same query shape with the materiality `HAVING` arithmetic present (the actual production
    default) — confirmed separately since an added `HAVING` clause is exactly the kind of
    change that could silently defeat an index (verified empirically it doesn't, but this test
    is the tripwire if a future edit changes that)."""
    sql = _captured_sql(
        indexed_bulk,
        lambda: list(indexed_bulk.duplicate_size_candidates(min_reclaim_bytes=1024 * 1024)),
    )
    _assert_query_uses_index(indexed_bulk, sql)


# --- Prefix-range queries: the real-disk regression -------------------------------------------
#
# `direct_children`/`subtree_size_bytes`/`_query_inventory`/`load_stat_cache`/`load_hash_cache`
# all used `path LIKE 'prefix/%' ESCAPE '\'` for "everything under this directory" — and an
# ESCAPE clause defeats SQLite's LIKE-to-index-range-scan optimization unconditionally
# (confirmed empirically for `files_matching_path_pattern` earlier, and it turned out to apply
# here too). On the real disk, `direct_children` alone measured ~1.5 *seconds* per call (a full
# 3.1M-row scan every time) — and `detect_archive_pairs` calls it once per archive file
# (thousands on a real disk with `.tar.gz` sdist caches), which is what turned "candidate
# generation is fast" into a second real 20+ minute stall, measured directly: 1309s for
# `detect_archive_pairs` alone before this fix, 3.78s after. These queries were rewritten to use
# a plain indexed range scan (`path >= lower AND path < upper`, see `_prefix_range`) for the
# primary bound instead.


def test_direct_children_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(indexed_bulk, lambda: indexed_bulk.direct_children(Path("C:/Data/dir1")))
    _assert_query_uses_index(indexed_bulk, sql)


def test_subtree_size_bytes_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(indexed_bulk, lambda: indexed_bulk.subtree_size_bytes(Path("C:/Data/dir1")))
    _assert_query_uses_index(indexed_bulk, sql)


def test_full_inventory_under_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: indexed_bulk.full_inventory(under=Path("C:/Data/dir1"))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_load_stat_cache_scoped_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: indexed_bulk.load_stat_cache(root=Path("C:/Data/dir1"))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_load_hash_cache_scoped_query_plan_uses_an_index(indexed_bulk: ScanIndex) -> None:
    sql = _captured_sql(
        indexed_bulk, lambda: indexed_bulk.load_hash_cache(root=Path("C:/Data/dir1"))
    )
    _assert_query_uses_index(indexed_bulk, sql)


def test_direct_children_handles_literal_underscore_in_parent_name(index: ScanIndex) -> None:
    """Regression fixture matching a real path found on the real disk
    (`.../immutable/_app`): a parent directory name containing a literal `_` must not have that
    character misinterpreted as a LIKE single-character wildcard — the range-based primary
    bound needs no escaping at all (unlike the old LIKE-based query), so this is correct by
    construction, not by a special-cased escape."""
    index.upsert_records(
        [
            _record("C:/Data/target/_app", is_dir=True),
            _record("C:/Data/target/_app/child1.txt"),
            _record("C:/Data/target/_app/child2.txt"),
            _record("C:/Data/target/_app/nested", is_dir=True),
            _record("C:/Data/target/_app/nested/deep.txt"),
            # A sibling name that an unescaped '_' wildcard could spuriously match against if
            # querying children of "_app" ever degraded back to a naive LIKE pattern.
            _record("C:/Data/target/Xapp/should_not_match.txt"),
        ],
        scanned_at=1000.0,
    )
    children = index.direct_children(Path("C:/Data/target/_app"))
    assert {r.path for r in children} == {
        Path("C:/Data/target/_app/child1.txt"),
        Path("C:/Data/target/_app/child2.txt"),
        Path("C:/Data/target/_app/nested"),
    }
