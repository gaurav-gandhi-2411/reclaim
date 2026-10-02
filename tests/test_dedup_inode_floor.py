"""The dedup materiality floor counts DISTINCT files (`(dev, ino)`), not path names: a size
bucket made only of hardlink names of one file has nothing to reclaim and must never be hashed,
while a bucket that still qualifies keeps EVERY name of every inode (hardlink-aware accounting
needs them)."""

from __future__ import annotations

from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
from reclaim.dedup import find_duplicate_clusters, materiality_exclusion_stats
from reclaim.index import ScanIndex, physical_size_bytes
from reclaim.models import FileRecord

_MIB = 1024 * 1024


def _rec(path: str, size: int, ino: int, dev: int = 7) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=False,
        size_bytes=size,
        attributes=0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
        dev=dev,
        ino=ino,
    )


def _index(tmp_path: Path, records: list[FileRecord]) -> ScanIndex:
    index = ScanIndex(tmp_path / "index.sqlite3")
    index.upsert_records(records, scanned_at=1000.0)
    return index


def _paths(index: ScanIndex, floor: int) -> set[str]:
    return {r.path.as_posix() for r in index.duplicate_size_candidates(min_reclaim_bytes=floor)}


@pytest.fixture
def hashed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every file shares one digest (byte-identical by construction); records paths read."""
    seen: list[Path] = []

    def _fake(path: Path, *_args: object) -> str:
        seen.append(path)
        return "same-content"

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", _fake)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", _fake)
    return seen


def test_a_hardlink_only_bucket_is_not_a_candidate_and_is_never_hashed(
    tmp_path: Path, hashed: list[Path]
) -> None:
    """Three names of ONE 2 MB inode: old row-count floor said (3-1)*2MB = 4MB, real reclaim 0."""
    names = [_rec(f"C:/d/link{i}.bin", 2 * _MIB, ino=111) for i in range(3)]
    with _index(tmp_path, names) as index:
        assert _paths(index, _MIB) == set()
        assert index.duplicate_size_candidate_count(min_reclaim_bytes=_MIB) == 0
        assert find_duplicate_clusters(index, min_reclaim_bytes=_MIB) == []
    assert hashed == []


def test_b_two_inodes_with_extra_names_stay_one_hardlink_aware_cluster(
    tmp_path: Path, hashed: list[Path]
) -> None:
    """Two distinct 600 KB inodes, each with two names, at a 512 KiB floor: (2-1)*600KB clears
    it, all four names stay members, and the real reclaim is exactly one inode's worth."""
    size = 600 * 1024
    records = [
        _rec("C:/d/a1.bin", size, ino=1),
        _rec("C:/d/a2.bin", size, ino=1),
        _rec("C:/d/b1.bin", size, ino=2),
        _rec("C:/d/b2.bin", size, ino=2),
    ]
    with _index(tmp_path, records) as index:
        assert len(_paths(index, 512 * 1024)) == 4
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=512 * 1024)
        # Same bucket under the 1 MiB floor: one inode of reclaim (600 KB) is below it, so the
        # bucket drops out although its row count (4 names -> 1.8 MB) used to clear it.
        assert _paths(index, _MIB) == set()
    assert len(clusters) == 1
    members = [clusters[0].keep, *clusters[0].duplicates]
    assert len(members) == 4
    assert physical_size_bytes(members) - size == size  # 2 inodes; reclaim = 1 inode


def test_c_mixed_bucket_keeps_every_name_and_only_pure_hardlink_buckets_drop(
    tmp_path: Path, hashed: list[Path]
) -> None:
    mixed = [
        *(_rec(f"C:/d/m_a{i}.bin", 2 * _MIB, ino=10) for i in range(3)),
        _rec("C:/d/m_b.bin", 2 * _MIB, ino=11),
        _rec("C:/d/m_c.bin", 2 * _MIB, ino=12),
    ]
    pure = [_rec(f"C:/d/p{i}.bin", 3 * _MIB, ino=20) for i in range(4)]
    with _index(tmp_path, [*mixed, *pure]) as index:
        got = _paths(index, _MIB)
        assert got == {r.path.as_posix() for r in mixed}
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=_MIB)
    assert len(clusters) == 1 and 1 + len(clusters[0].duplicates) == 5
    assert not any(p.name.startswith("p") for p in hashed)  # no pure-hardlink name was read


def test_d_empty_files_and_unknown_identity_rows_are_unaffected(tmp_path: Path) -> None:
    # Zero-byte files never were candidates, hardlinked or not.
    empties = [_rec(f"C:/d/e{i}.bin", 0, ino=5) for i in range(4)]
    # ino == 0 means "identity unknown": each row is its own file, never collapsed together.
    unknown = [_rec(f"C:/d/u{i}.bin", 2 * _MIB, ino=0, dev=0) for i in range(2)]
    with _index(tmp_path, [*empties, *unknown]) as index:
        assert _paths(index, _MIB) == {r.path.as_posix() for r in unknown}


@pytest.mark.parametrize(
    ("size", "expected"), [(_MIB, 2), (_MIB - 1, 0), (_MIB + 1, 2)], ids=["exact", "below", "above"]
)
def test_e_floor_boundary_is_inclusive_at_distinct_inode_level(
    tmp_path: Path, size: int, expected: int
) -> None:
    """Two distinct inodes: reclaim = (2-1)*size, kept iff >= 1 MiB (same `>=` as before)."""
    records = [_rec("C:/d/x.bin", size, ino=1), _rec("C:/d/y.bin", size, ino=2)]
    with _index(tmp_path, records) as index:
        assert len(_paths(index, _MIB)) == expected


def test_materiality_stats_do_not_count_a_hardlink_only_bucket_as_a_collision(
    tmp_path: Path,
) -> None:
    records = [
        *(_rec(f"C:/d/h{i}.bin", 10, ino=1) for i in range(3)),  # one file: not a collision
        _rec("C:/d/s1.bin", 8, ino=2),
        _rec("C:/d/s2.bin", 8, ino=3),  # two distinct 8-byte files: immaterial collision
    ]
    with _index(tmp_path, records) as index:
        stats = materiality_exclusion_stats(index, min_reclaim_bytes=_MIB)
    assert stats.excluded_bucket_count == 1
    assert stats.theoretical_bytes == 8
