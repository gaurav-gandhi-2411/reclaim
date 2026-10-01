from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import reclaim.index as index_module
from reclaim.config import Config
from reclaim.executor import apply_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, FileRecord, Tier, Verdict
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# perf/review-queue-dry-run: on the real 5.86M-row index, `apply_batch(apply=False)` of the
# Advanced-mode review queue ran ~8.8s PER DIRECTORY ITEM (1307s for 149 `subtree_entry_count`
# calls, cProfile). Root cause: the `AND is_cloud_placeholder = 0` term let SQLite's planner pick
# `idx_files_is_cloud_placeholder` (every row has 0) over the primary-key prefix range, so each
# "count one subtree" query walked ~the whole table. The fix is a one-character planner hint (`+`);
# these tests pin (1) identical results, (2) the access path, (3) the safety re-verification that
# rides the same query, and (4) that a dry run does O(1)-per-item work and writes nothing.

_NOW = 1_700_000_000.0


def _record(path: str, *, is_dir: bool = False, placeholder: bool = False) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=0 if is_dir else 10,
        attributes=0x400000 if placeholder else 0,  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
    )


# Names include `%`/`_` (LIKE wildcards), a sibling that merely shares a string prefix
# (`cache2` vs `cache`), and cloud placeholders -- every edge `_prefix_range` must keep right.
_PATHS: list[tuple[str, bool, bool]] = [
    ("C:/D/cache", True, False),
    ("C:/D/cache/a.txt", False, False),
    ("C:/D/cache/sub", True, False),
    ("C:/D/cache/sub/b.txt", False, False),
    ("C:/D/cache/sub/cloud.txt", False, True),
    ("C:/D/cache2", True, False),
    ("C:/D/cache2/x.txt", False, False),
    ("C:/D/100%_done", True, False),
    ("C:/D/100%_done/y.txt", False, False),
    ("C:/D/100Xxdone/z.txt", False, False),
    ("C:/D/solo.txt", False, False),
]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[ScanIndex]:
    idx = ScanIndex(tmp_path / "index.sqlite3")
    idx.upsert_records(
        [_record(p, is_dir=d, placeholder=c) for p, d, c in _PATHS], scanned_at=1000.0
    )
    yield idx
    idx.close()


def _brute_force(under: str) -> list[str]:
    """Pure-Python oracle: non-placeholder rows at or strictly under `under`."""
    return sorted(
        p
        for p, _d, placeholder in _PATHS
        if not placeholder and (p == under or p.startswith(under + "/"))
    )


@pytest.mark.parametrize(
    "under",
    [
        "C:/D/cache",
        "C:/D/cache/sub",
        "C:/D/cache2",
        "C:/D/100%_done",
        "C:/D/solo.txt",
        "C:/D/nonexistent",
        "C:/D",
    ],
)
def test_subtree_queries_return_identical_rows_to_the_oracle(index: ScanIndex, under: str) -> None:
    """Golden: the planner hint must not change WHICH rows match -- same decisions as before."""
    expected = _brute_force(under)
    assert index.subtree_entry_count(Path(under)) == len(expected)
    assert sorted(r.path.as_posix() for r in index.candidate_inventory(under=Path(under))) == (
        expected
    )


@pytest.mark.parametrize(
    "run",
    [
        lambda idx: idx.subtree_entry_count(Path("C:/D/cache")),
        lambda idx: idx.candidate_inventory(under=Path("C:/D/cache")),
    ],
    ids=["subtree_entry_count", "candidate_inventory_under"],
)
def test_prefix_scoped_queries_use_the_primary_key_not_the_placeholder_index(
    index: ScanIndex, run: object
) -> None:
    """Teeth for the root cause. `tests/test_query_plan_coverage.py` only rejects a bare
    `SCAN files`, which this bug never produced (it was a SEARCH over a useless low-cardinality
    index), so the access path itself is pinned here."""
    captured: list[str] = []
    index._conn.set_trace_callback(captured.append)
    try:
        run(index)  # type: ignore[operator]
    finally:
        index._conn.set_trace_callback(None)
    sql = captured[-1]
    details = [r["detail"] for r in index._conn.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("sqlite_autoindex_files_1" in d for d in details), details
    assert not any("idx_files_is_cloud_placeholder" in d for d in details), details


def test_planner_hint_is_what_prevents_the_placeholder_index(index: ScanIndex) -> None:
    """Control: documents that the un-hinted form really does choose the bad index on this
    SQLite, so the test above would fail if the `+` were dropped."""
    prefix = "C:/D/cache"
    lower, upper = index_module._prefix_range(prefix)
    sql = (
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM files "
        "WHERE (path = ? OR (path >= ? AND path < ?)) AND is_cloud_placeholder = 0"
    )
    details = [r["detail"] for r in index._conn.execute(sql, (prefix, lower, upper))]
    assert any("idx_files_is_cloud_placeholder" in d for d in details), details


# --- teeth: the re-verification that rides the optimized query still catches changes -----------

pytestmark_nt = pytest.mark.skipif(os.name != "nt", reason="scanner/executor target Windows/NTFS")


def _dir_candidate(path: Path, index: ScanIndex, *, retention_days: int | None) -> Candidate:
    record = index.get_record(path)
    assert record is not None
    return Candidate(
        path=path,
        is_dir=True,
        category="dev_artifacts",
        category_group="dev_artifacts",
        size_bytes=20,
        tier=Tier.B,
        rationale="r",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="T",
        retention_days=retention_days,
        dev=record.dev,
        ino=record.ino,
        mtime=record.mtime,
    )


@pytestmark_nt
@pytest.mark.parametrize("mutation", ["swap_nested_file", "add_new_file"])
def test_file_changed_after_scan_is_still_skipped_on_a_real_apply(
    tmp_path: Path, mutation: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "l1").mkdir(parents=True)
    (cache / "l1" / "f.bin").write_bytes(b"original")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        scan_tree(tmp_path, idx)
        candidate = _dir_candidate(cache, idx, retention_days=None)
        if mutation == "swap_nested_file":
            (cache / "l1" / "f.bin").unlink()
            (cache / "l1" / "f.bin").write_bytes(b"swapped!")  # new inode
        else:
            (cache / "l1" / "new.bin").write_bytes(b"added after scan")
        report = apply_batch(
            [candidate],
            safety=SafetyValidator(Config()),
            apply=True,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=idx,
        )
    (item,) = report.items
    assert item.succeeded is False
    assert item.skip_reason == "identity_changed_since_scan"
    assert (cache / "l1").exists()  # nothing deleted


# --- budget: a dry run is O(1) per item and durably writes nothing ----------------------------


@pytestmark_nt
def test_dry_run_per_batch_work_is_bounded_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n_dirs = 25
    for i in range(n_dirs):
        d = tmp_path / f"proj{i}" / "node_modules"
        d.mkdir(parents=True)
        (d / "m.js").write_bytes(b"x")
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        scan_tree(tmp_path, idx)
        candidates = [
            _dir_candidate(tmp_path / f"proj{i}" / "node_modules", idx, retention_days=None)
            for i in range(n_dirs)
        ]
        statements: list[str] = []
        idx._conn.set_trace_callback(statements.append)

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("a dry run must not spawn a subprocess (e.g. git status)")

        def _forbidden_open(*args: object, **kwargs: object) -> None:
            raise AssertionError("a dry run must not open another ScanIndex per item")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(ScanIndex, "__init__", _forbidden_open)
        report = apply_batch(
            candidates,
            safety=SafetyValidator(Config()),
            apply=False,
            method="vault",
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            now=_NOW,
            scan_index=idx,
        )
        idx._conn.set_trace_callback(None)

    assert len(report.items) == n_dirs and all(i.succeeded for i in report.items)
    # Exactly one cheap indexed COUNT per directory item, nothing else: O(items), not O(table).
    assert len(statements) == n_dirs
    assert all(s.lstrip().upper().startswith("SELECT COUNT(*)") for s in statements)
    assert not (tmp_path / "manifest.jsonl").exists()
    assert not (tmp_path / "vault").exists()
