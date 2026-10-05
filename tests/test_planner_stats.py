from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from reclaim.detectors import detect_archive_pairs
from reclaim.index import ScanIndex, _prefix_range
from reclaim.models import FileRecord
from reclaim.scanner import scan_tree

# perf/analyze-after-scan: root cause of the 8.8 s-per-call `subtree_entry_count` was an index
# with NO `sqlite_stat1`, so SQLite treated the one-valued `idx_files_is_cloud_placeholder` as
# selective. These tests pin that a scan leaves statistics behind, that they never change a
# result, that they (alone, without the unary `+` hint) flip the plan to the primary-key range,
# and that the step stays cheap.


def _record(path: str, *, is_dir: bool = False, size: int = 10) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=0 if is_dir else size,
        attributes=0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
    )


def _bulk(index: ScanIndex, n: int) -> None:
    """`n` non-placeholder rows spread over n/50 directories, all `is_cloud_placeholder = 0`."""
    rows = [_record(f"C:/Big/d{i % (n // 50)}/f{i}.bin", size=i % 997) for i in range(n)]
    for start in range(0, n, 20_000):
        index.upsert_records(rows[start : start + 20_000], scanned_at=1000.0)


def _stat1_tables(index: ScanIndex) -> set[str]:
    return {r["tbl"] for r in index._conn.execute("SELECT tbl FROM sqlite_stat1")}


def test_scan_tree_leaves_statistics_behind(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    for i in range(5):
        (root / "sub" / f"f{i}.txt").write_text("x")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        assert not idx.has_planner_stats()
        scan_tree(root, idx)
        assert idx.has_planner_stats()
        assert "files" in _stat1_tables(idx)


def test_failed_scan_still_fills_in_missing_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:

        def boom(_root: Path) -> int:
            raise sqlite3.OperationalError("simulated failure")

        monkeypatch.setattr(idx, "prune_unseen_under_root", boom)
        with pytest.raises(sqlite3.OperationalError, match="simulated"):
            scan_tree(root, idx)
        assert idx.has_planner_stats()


def test_only_if_missing_skips_existing_statistics(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        scan_tree(root, idx)
        assert idx.refresh_planner_stats(only_if_missing=True) == 0.0
        assert idx.refresh_planner_stats() > 0.0


def test_statistics_failure_never_masks_a_completed_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f.txt").write_text("x")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:

        def disk_full(*_a: object, **_k: object) -> float:
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(idx, "refresh_planner_stats", disk_full)
        stats = scan_tree(root, idx)
        assert stats.files_written >= 1


_QUERIES = {
    "entry_count": lambda i, p: i.subtree_entry_count(p),
    "inventory_under": lambda i, p: sorted(r.path.as_posix() for r in i.candidate_inventory(p)),
    "inventory_all": lambda i, _p: sorted(r.path.as_posix() for r in i.candidate_inventory()),
    "size": lambda i, p: i.subtree_size_bytes(p),
    "newest": lambda i, p: i.subtree_newest_mtime(p),
    "larger": lambda i, _p: sorted(r.path.as_posix() for r in i.files_larger_than(500)),
    "by_ext": lambda i, _p: sorted(r.path.as_posix() for r in i.files_by_ext([".bin"])),
    "dups": lambda i, _p: sorted(
        r.path.as_posix() for r in i.duplicate_size_candidates(min_reclaim_bytes=0)
    ),
    "immaterial": lambda i, _p: i.immaterial_duplicate_bucket_stats(min_reclaim_bytes=10**9),
}


@pytest.mark.parametrize("how", ["analyze", "optimize", "optimize_limit"])
def test_statistics_never_change_any_query_result(tmp_path: Path, how: str) -> None:
    """Golden: every statistics-sensitive query returns identical rows before and after."""
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        _bulk(idx, 3_000)
        idx.upsert_records(
            [_record("C:/Big/d3", is_dir=True), _record("C:/Big/d3x/z.bin", size=5)],
            scanned_at=1000.0,
        )
        under = Path("C:/Big/d3")
        before = {k: q(idx, under) for k, q in _QUERIES.items()}
        if how == "analyze":
            idx.refresh_planner_stats()
        elif how == "optimize":
            idx._conn.execute("PRAGMA optimize")
        else:
            idx._conn.execute("PRAGMA analysis_limit=1000")
            idx._conn.execute("PRAGMA optimize")
        assert before["entry_count"] > 0
        after = {k: q(idx, under) for k, q in _QUERIES.items()}
        assert after == before


def _plan(index: ScanIndex, sql: str, params: tuple[object, ...]) -> list[str]:
    return [r["detail"] for r in index._conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def test_statistics_alone_make_the_planner_pick_the_primary_key_range(tmp_path: Path) -> None:
    """Root-cause proof on a skewed table (every row is_cloud_placeholder = 0): the UN-hinted
    query walks the placeholder index with no stats, and uses the PK path range once the full
    ANALYZE has run -- no unary `+` involved."""
    prefix = "C:/Big/d3"
    lower, upper = _prefix_range(prefix)
    sql = (
        "SELECT COUNT(*) FROM files WHERE (path = ? OR (path >= ? AND path < ?)) "
        "AND is_cloud_placeholder = 0"
    )
    params = (prefix, lower, upper)
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        _bulk(idx, 60_000)
        before = _plan(idx, sql, params)
        assert any("idx_files_is_cloud_placeholder" in d for d in before), before
        idx.refresh_planner_stats()
        after = _plan(idx, sql, params)
        assert any("sqlite_autoindex_files_1" in d for d in after), after
        assert not any("idx_files_is_cloud_placeholder" in d for d in after), after


def test_bounded_analysis_is_not_enough(tmp_path: Path) -> None:
    """Why `refresh_planner_stats` is a FULL ANALYZE: `analysis_limit=1000` samples only the
    first entries of the one-valued index and keeps the bad plan (measured on the real index)."""
    prefix = "C:/Big/d3"
    lower, upper = _prefix_range(prefix)
    sql = (
        "SELECT COUNT(*) FROM files WHERE (path = ? OR (path >= ? AND path < ?)) "
        "AND is_cloud_placeholder = 0"
    )
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        _bulk(idx, 60_000)
        idx._conn.execute("PRAGMA analysis_limit=1000")
        idx._conn.execute("PRAGMA optimize")
        details = _plan(idx, sql, (prefix, lower, upper))
        assert any("idx_files_is_cloud_placeholder" in d for d in details), details


def test_dedup_bucket_scan_is_pinned_to_a_table_scan_with_or_without_statistics(
    tmp_path: Path,
) -> None:
    """Regression for the post-ANALYZE slowdown of the dedup candidate queries: once `ANALYZE`
    ran, the planner swapped the distinct-inode GROUP BY from a rowid-order scan to the
    non-covering `idx_files_size` (a random row lookup per entry; measured ~3x CPU on the real
    index). The inner scan is `NOT INDEXED`, so its plan must not depend on statistics."""
    from reclaim.index import _QUALIFYING_SIZES_SQL

    unpinned = _QUALIFYING_SIZES_SQL.replace("FROM files NOT INDEXED", "FROM files")
    assert unpinned != _QUALIFYING_SIZES_SQL  # the pin really is in the production SQL
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        _bulk(idx, 60_000)
        for stage in ("no_stats", "analyzed"):
            if stage == "analyzed":
                idx.refresh_planner_stats()
            pinned_plan = _plan(idx, _QUALIFYING_SIZES_SQL, (1,))
            assert any(d == "SCAN files" for d in pinned_plan), (stage, pinned_plan)
            assert not any("idx_files_size" in d for d in pinned_plan), (stage, pinned_plan)
        # Control: the same SQL without the pin does take the index once statistics exist, so
        # the assertions above are proven able to fail (planner behaviour of this SQLite build).
        control = _plan(idx, unpinned, (1,))
        assert any("idx_files_size" in d for d in control), control


def test_stats_step_is_cheap_on_a_200k_row_index(tmp_path: Path) -> None:
    """Budget: generous bound (real 5.86M-row index measured at 11-17 s; this is 1/29th)."""
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        _bulk(idx, 200_000)
        start = time.perf_counter()
        reported = idx.refresh_planner_stats()
        elapsed = time.perf_counter() - start
        assert idx.has_planner_stats()
        assert reported <= elapsed + 0.05
        assert elapsed < 10.0, elapsed


# --- detect_archive_pairs: per-parent memo is behaviour-preserving -----------------------------


def _reference_archive_pairs(index: ScanIndex) -> list[tuple[str, str]]:
    """The pre-memo algorithm, verbatim in structure: one `direct_children` per archive."""
    from difflib import SequenceMatcher

    from reclaim import detectors

    out: list[tuple[str, str]] = []
    for record in index.files_by_ext(detectors._ARCHIVE_EXTS_FOR_PREFILTER, is_dir=False):
        stem = detectors._archive_stem(record.path.name)
        if stem is None:
            continue
        best: str | None = None
        best_ratio = 0.0
        for sibling in index.direct_children(record.path.parent):
            if not sibling.is_dir:
                continue
            ratio = SequenceMatcher(None, stem.lower(), sibling.path.name.lower()).ratio()
            if ratio >= detectors._ARCHIVE_OVERLAP_THRESHOLD and ratio > best_ratio:
                best_ratio, best = ratio, sibling.path.name
        if best is not None:
            out.append((record.path.as_posix(), best))
    return out


def test_archive_pairs_memo_matches_reference_and_queries_once_per_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = []
    for parent in ("C:/A", "C:/B"):
        for n in ("photos", "docs", "photos2", "other"):
            records.append(_record(f"{parent}/{n}.zip"))
        records.append(_record(f"{parent}/photos", is_dir=True))
        records.append(_record(f"{parent}/photos2", is_dir=True))  # tie-break vs 'photos'
        records.append(_record(f"{parent}/docs", is_dir=True))
        records.append(_record(f"{parent}/photos/inner.jpg"))
    records.append(_record("C:/A/plain.gz"))  # no stem -> must not trigger a query
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        idx.upsert_records(records, scanned_at=1000.0)
        expected = _reference_archive_pairs(idx)
        assert expected  # the fixture really produces pairs

        calls: list[Path] = []
        original = idx.direct_children

        def counting(parent: Path) -> list[FileRecord]:
            calls.append(parent)
            return original(parent)

        monkeypatch.setattr(idx, "direct_children", counting)
        got = [(c.path.as_posix(), c.rationale.split("'")[3]) for c in detect_archive_pairs(idx)]
        assert got == expected
        assert sorted(calls) == [Path("C:/A"), Path("C:/B")]
