from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import reclaim.index as index_module
from reclaim.index import ScanIndex, logical_size_bytes, physical_size_bytes
from reclaim.models import FileRecord

# ADR-0042: an opaque dependency-tree root is ONE directory row (`is_dir=1`) carrying aggregates.
# These tests pin the schema migration and every index-level consumer of those aggregates.

_OLD_FILES_DDL = """
CREATE TABLE files (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL, ctime REAL NOT NULL,
    ext TEXT NOT NULL, attributes INTEGER NOT NULL, dev INTEGER NOT NULL, ino INTEGER NOT NULL,
    is_dir INTEGER NOT NULL, is_cloud_placeholder INTEGER NOT NULL,
    is_reparse_point INTEGER NOT NULL, git_repo_root TEXT, git_repo_clean INTEGER NOT NULL,
    last_scanned REAL NOT NULL, hash_size INTEGER, hash_mtime REAL, partial_hash TEXT,
    full_hash TEXT, name TEXT, path_lower TEXT
)
"""


def _file(path: str, size: int, *, mtime: float = 100.0, dev: int = 1, ino: int = 0) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=False,
        size_bytes=size,
        attributes=0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=mtime,
        dev=dev,
        ino=ino,
    )


def _opaque(
    path: str,
    *,
    files: int,
    nbytes: int,
    newest: float | None,
    physical: int | None = None,
    ino: int = 0,
) -> FileRecord:
    return FileRecord(
        path=Path(path),
        is_dir=True,
        size_bytes=0,
        attributes=0x10,
        ext="",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=50.0,
        ctime=50.0,
        dev=1,
        ino=ino,
        opaque=True,
        agg_files=files,
        agg_bytes=nbytes,
        agg_newest_mtime=newest,
        agg_physical_bytes=nbytes if physical is None else physical,
    )


def test_fresh_schema_has_opaque_columns_with_defaults(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records([_file("C:/a/f.txt", 10)], scanned_at=1.0)
        rec = idx.get_record(Path("C:/a/f.txt"))
        assert rec is not None
        assert (rec.opaque, rec.agg_files, rec.agg_bytes, rec.agg_physical_bytes) == (
            False,
            0,
            0,
            0,
        )
        assert rec.agg_newest_mtime is None


def test_legacy_index_is_migrated_in_place_and_old_rows_read_as_not_opaque(
    tmp_path: Path,
) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(_OLD_FILES_DDL)
    conn.execute(
        "INSERT INTO files (path, size, mtime, ctime, ext, attributes, dev, ino, is_dir, "
        "is_cloud_placeholder, is_reparse_point, git_repo_clean, last_scanned, name, path_lower) "
        "VALUES ('C:/old/f.txt', 5, 1.0, 1.0, '.txt', 0, 1, 2, 0, 0, 0, 0, 1.0, 'f.txt', "
        "'c:/old/f.txt')"
    )
    conn.commit()
    conn.close()

    with ScanIndex(db) as idx:
        rec = idx.get_record(Path("C:/old/f.txt"))
        assert rec is not None
        assert rec.opaque is False
        assert rec.agg_files == rec.agg_bytes == rec.agg_physical_bytes == 0
        assert rec.agg_newest_mtime is None
        # Re-opening an already migrated index must be a no-op, not a duplicate-column error.
    with ScanIndex(db) as idx:
        assert idx.record_exists(Path("C:/old/f.txt"))


def test_opaque_record_round_trips_through_the_row_builder(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [_opaque("C:/p/node_modules", files=7, nbytes=700, newest=123.5, physical=650)],
            scanned_at=1.0,
        )
        rec = idx.get_record(Path("C:/p/node_modules"))
        assert rec is not None
        assert rec.is_dir is True
        assert rec.size_bytes == 0
        assert (rec.opaque, rec.agg_files, rec.agg_bytes) == (True, 7, 700)
        assert rec.agg_newest_mtime == 123.5
        assert rec.agg_physical_bytes == 650


def test_subtree_size_adds_agg_bytes_of_opaque_roots_in_range(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/p/src/a.py", 10),
                _file("C:/p/b.py", 5),
                _opaque("C:/p/node_modules", files=3, nbytes=1000, newest=1.0),
                _file("C:/other/c.py", 99),
            ],
            scanned_at=1.0,
        )
        assert idx.subtree_size_bytes(Path("C:/p")) == 10 + 5 + 1000
        # The opaque root queried directly: its own aggregate, nothing else.
        assert idx.subtree_size_bytes(Path("C:/p/node_modules")) == 1000
        assert idx.subtree_size_bytes(Path("C:/other")) == 99


def test_subtree_newest_mtime_takes_the_max_with_agg_newest_mtime(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/p/a.py", 1, mtime=100.0),
                _opaque("C:/p/node_modules", files=1, nbytes=1, newest=900.0),
                _opaque("C:/q/node_modules", files=1, nbytes=1, newest=None),
            ],
            scanned_at=1.0,
        )
        assert idx.subtree_newest_mtime(Path("C:/p")) == 900.0
        # A deep file newer than everything on a row: the aggregate wins even though the root
        # directory row's own mtime (50.0) is old -- the staleness the ADR is about.
        assert idx.subtree_newest_mtime(Path("C:/p/node_modules")) == 900.0
        # No agg value (NULL): falls back to the row's own mtime, never raises.
        assert idx.subtree_newest_mtime(Path("C:/q")) == 50.0
        assert idx.subtree_newest_mtime(Path("C:/nothing")) is None


def test_subtree_entry_count_adds_agg_files(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/p/a.py", 1),
                _opaque("C:/p/node_modules", files=250, nbytes=1, newest=1.0),
            ],
            scanned_at=1.0,
        )
        # 2 rows + 250 files below the opaque root: the apply-time entry-count guard sees them.
        assert idx.subtree_entry_count(Path("C:/p")) == 2 + 250


def test_newest_files_under_never_lists_files_below_an_opaque_root(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/p/a.py", 1, mtime=5.0),
                _opaque("C:/p/node_modules", files=9, nbytes=9, newest=999.0),
            ],
            scanned_at=1.0,
        )
        assert idx.newest_files_under(Path("C:/p"), limit=10) == [Path("C:/p/a.py")]
        assert [r.path for r in idx.opaque_roots_under(Path("C:/p"))] == [Path("C:/p/node_modules")]
        assert [r.path for r in idx.opaque_roots_under(Path("C:/p/node_modules"))] == [
            Path("C:/p/node_modules")
        ]
        assert idx.opaque_roots_under(Path("C:/elsewhere")) == []


def test_physical_total_adds_agg_physical_bytes_and_matches_the_python_twin(
    tmp_path: Path,
) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/p/a.py", 100, dev=1, ino=10),
                _file("C:/p/a_link.py", 100, dev=1, ino=10),  # hardlink: counted once
                _opaque(
                    "C:/p/node_modules", files=3, nbytes=3000, newest=1.0, physical=2500, ino=7
                ),
                _opaque("C:/q/.venv", files=1, nbytes=40, newest=1.0, physical=40, ino=8),
            ],
            scanned_at=1.0,
        )
        assert idx.physical_size_bytes_total() == 100 + 2500 + 40
        assert idx.physical_size_bytes_total() == physical_size_bytes(idx.full_inventory())
        assert idx.physical_size_bytes_total(Path("C:/p")) == 100 + 2500
        assert idx.physical_size_bytes_total(Path("C:/p")) == physical_size_bytes(
            idx.full_inventory(under=Path("C:/p"))
        )
        assert idx.physical_size_bytes_total(Path("C:/q")) == 40
        assert logical_size_bytes(idx.full_inventory()) == 100 + 100 + 3000 + 40


def test_load_stat_cache_omits_opaque_roots_so_they_are_always_rewritten(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [_file("C:/p/a.py", 1), _opaque("C:/p/node_modules", files=1, nbytes=1, newest=1.0)],
            scanned_at=1.0,
        )
        assert set(idx.load_stat_cache(Path("C:/p"))) == {"C:/p/a.py"}
        assert set(idx.load_stat_cache()) == {"C:/p/a.py"}


def test_delete_descendants_removes_rows_strictly_below_only(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _opaque("C:/p/node_modules", files=1, nbytes=1, newest=1.0),
                _file("C:/p/node_modules/x/y.js", 1),
                _file("C:/p/node_modules/z.js", 1),
                _file("C:/p/node_modules_extra/keep.js", 1),  # sibling sharing the name prefix
                _file("C:/p/a.py", 1),
            ],
            scanned_at=1.0,
        )
        assert idx.delete_descendants(["C:/p/node_modules"]) == 2
        assert idx.record_exists(Path("C:/p/node_modules"))
        assert idx.record_exists(Path("C:/p/node_modules_extra/keep.js"))
        assert idx.record_exists(Path("C:/p/a.py"))
        assert not idx.record_exists(Path("C:/p/node_modules/z.js"))


def test_page_rows_after_reports_the_aggregate_as_the_size_of_an_opaque_root(
    tmp_path: Path,
) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [_file("C:/p/a.py", 7), _opaque("C:/p/node_modules", files=1, nbytes=900, newest=1.0)],
            scanned_at=1.0,
        )
        rows = {path: (size, is_dir) for path, size, is_dir in idx.page_rows_after("", limit=10)}
        assert rows["C:/p/a.py"] == (7, False)
        assert rows["C:/p/node_modules"] == (900, True)


def test_prune_dead_rows_counts_the_aggregate_bytes_of_a_dead_opaque_root(tmp_path: Path) -> None:
    from reclaim.index_prune import _Dir, prune_dead_rows

    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records(
            [
                _file("C:/gone/a.txt", 11),
                _opaque("C:/gone/node_modules", files=4, nbytes=900, newest=1.0),
            ],
            scanned_at=1.0,
        )
        report = prune_dead_rows(
            idx, apply=False, probe_dir=lambda p: _Dir.EXISTS if p == "C:" else _Dir.MISSING
        )
        assert report.dead_rows == 2
        assert report.dead_bytes == 11 + 900


_OPAQUE_TAIL = [
    "opaque",
    "agg_files",
    "agg_bytes",
    "agg_newest_mtime",
    "agg_physical_bytes",
    "agg_newest_dir_mtime",
]


def _column_names(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(files)")]
    finally:
        conn.close()


def test_opaque_columns_have_the_same_order_in_a_fresh_and_a_migrated_index(
    tmp_path: Path,
) -> None:
    """Schema pin: new databases and in-place migrations end with the same six columns in the
    same order, matching `_COLUMNS`' tail."""
    fresh = tmp_path / "fresh.sqlite3"
    ScanIndex(fresh).close()
    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.execute(_OLD_FILES_DDL)
    conn.commit()
    conn.close()
    ScanIndex(legacy).close()
    assert _column_names(fresh)[-6:] == _OPAQUE_TAIL
    assert _column_names(legacy)[-6:] == _OPAQUE_TAIL
    assert _column_names(fresh)[-6:] == list(index_module._COLUMNS[-6:])


def test_newest_dir_mtime_round_trips_and_feeds_subtree_newest_mtime(tmp_path: Path) -> None:
    root = replace(
        _opaque("C:/p/node_modules", files=1, nbytes=1, newest=1.0), agg_newest_dir_mtime=777.0
    )
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records([root], scanned_at=1.0)
        stored = idx.get_record(Path("C:/p/node_modules"))
        assert stored is not None and stored.agg_newest_dir_mtime == 777.0
        # a renamed/added directory entry moves a directory mtime, which counts as "recently used"
        assert idx.subtree_newest_mtime(Path("C:/p")) == 777.0


def test_drop_opaque_rows_removes_only_opaque_rows_and_matches_case_insensitively(
    tmp_path: Path,
) -> None:
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        plain_dir = replace(
            _opaque("C:/p/plain/node_modules", files=0, nbytes=0, newest=None),
            opaque=False,
            agg_bytes=0,
            agg_physical_bytes=0,
        )
        idx.upsert_records(
            [
                _opaque("C:/p/Node_Modules", files=2, nbytes=900, newest=1.0),
                _opaque("C:/q/node_modules", files=2, nbytes=500, newest=1.0),
                plain_dir,  # an ORDINARY directory row at a matching path
                _file("C:/p/Node_Modules_extra/x.js", 5),
            ],
            scanned_at=1.0,
        )
        # lower-case request finds the mixed-case stored row; the ordinary row survives a drop
        # even though its path is requested too.
        assert idx.drop_opaque_rows(["C:/p/node_modules", "C:/p/plain/node_modules"]) == 1
        assert not idx.record_exists(Path("C:/p/Node_Modules"))
        assert idx.record_exists(Path("C:/p/plain/node_modules"))
        assert idx.record_exists(Path("C:/q/node_modules"))  # other opaque row untouched
        assert idx.record_exists(Path("C:/p/Node_Modules_extra/x.js"))  # prefix sibling untouched
        # upper-case request finds a lower-case stored row
        assert idx.drop_opaque_rows(["C:/Q/NODE_MODULES"]) == 1
        assert not idx.record_exists(Path("C:/q/node_modules"))


def test_directory_rows_are_excluded_from_the_file_candidate_pools(tmp_path: Path) -> None:
    """An opaque root is a directory row; dedup / large-file / extension pools filter on
    `is_dir = 0` and so never see it, however large its recorded size or aggregates."""
    big_a = replace(
        _opaque("C:/a/node_modules", files=9, nbytes=10**9, newest=1.0), size_bytes=10**9
    )
    big_b = replace(
        _opaque("C:/b/node_modules", files=9, nbytes=10**9, newest=1.0), size_bytes=10**9
    )
    with ScanIndex(tmp_path / "i.sqlite3") as idx:
        idx.upsert_records([big_a, big_b, _file("C:/c/f.bin", 5)], scanned_at=1.0)
        assert [r.path for r in idx.files_larger_than(1)] == [Path("C:/c/f.bin")]
        assert list(idx.duplicate_size_candidates(min_reclaim_bytes=0)) == []
        assert idx.duplicate_qualifying_sizes(min_reclaim_bytes=0) == []
