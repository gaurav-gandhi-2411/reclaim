from __future__ import annotations

from collections.abc import Iterator
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
