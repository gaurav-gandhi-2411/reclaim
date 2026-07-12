from __future__ import annotations

from pathlib import Path

from reclaim.dedup import (
    _PARTIAL_HASH_CHUNK_BYTES,
    _PARTIAL_HASH_WHOLE_FILE_THRESHOLD,
    _compute_full_hash,
    _compute_partial_hash,
    _is_downloads_or_temp,
    _size_buckets,
    select_keep,
)
from reclaim.models import FileRecord

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


# --- Size bucketing -------------------------------------------------------------------------


def test_size_buckets_drops_singleton_buckets() -> None:
    records = [
        _record("C:/Data/a.txt", size_bytes=100),
        _record("C:/Data/b.txt", size_bytes=200),
    ]
    assert _size_buckets(records) == {}


def test_size_buckets_keeps_buckets_with_two_or_more_members() -> None:
    records = [
        _record("C:/Data/a.txt", size_bytes=100),
        _record("C:/Data/b.txt", size_bytes=100),
        _record("C:/Data/c.txt", size_bytes=200),
    ]
    buckets = _size_buckets(records)
    assert set(buckets.keys()) == {100}
    assert {r.path for r in buckets[100]} == {Path("C:/Data/a.txt"), Path("C:/Data/b.txt")}


def test_size_buckets_skips_zero_byte_files() -> None:
    records = [
        _record("C:/Data/a.txt", size_bytes=0),
        _record("C:/Data/b.txt", size_bytes=0),
    ]
    assert _size_buckets(records) == {}


def test_size_buckets_skips_directories() -> None:
    records = [
        _record("C:/Data/a", is_dir=True, size_bytes=100),
        _record("C:/Data/b", is_dir=True, size_bytes=100),
    ]
    assert _size_buckets(records) == {}


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
