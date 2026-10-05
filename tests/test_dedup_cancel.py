"""ADR-0040: `find_duplicate_clusters` is cancellable at batch boundaries and resumable.

Hash calls are COUNTED through a fake `_compute_partial_hash` (no wall-clock, no sleeps). Every
file has an identical twin of the same size <= 128 KB, so each file costs exactly one partial
hash and no separate full-hash read: total hash calls on a cold pass == number of files.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from reclaim import dedup
from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

_SIZES = 12  # 12 size buckets x 2 identical files = 24 files
_FILES = _SIZES * 2
_WINDOW = 4  # files per window -> 2 buckets per window -> 6 windows


def _make_tree(root: Path) -> Path:
    tree = root / "tree"
    for size_index in range(_SIZES):
        for copy in range(2):
            directory = tree / f"d{copy}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"f{size_index}.bin").write_bytes(
                bytes([65 + size_index]) * (1000 + size_index)
            )
    return tree


def _hashed_rows(db: Path) -> int:
    with ScanIndex(db) as index:
        row = index._conn.execute("SELECT COUNT(*) FROM files WHERE hash_size IS NOT NULL")
        return int(row.fetchone()[0])


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(dedup, "_WINDOW_FILES", _WINDOW)
    path = tmp_path / "index.sqlite3"
    with ScanIndex(path) as index:
        scan_tree(_make_tree(tmp_path), index)
    return path


@pytest.fixture
def hash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    lock = threading.Lock()
    real = dedup._compute_partial_hash

    def counting(path: Path, size: int) -> str:
        with lock:
            calls.append(path)
        return real(path, size)

    monkeypatch.setattr(dedup, "_compute_partial_hash", counting)
    return calls


def _cancel_on_nth_checkpoint(n: int) -> Callable[[], None]:
    seen = [0]

    def checkpoint() -> None:
        seen[0] += 1
        if seen[0] >= n:
            raise dedup.DedupCancelled

    return checkpoint


def test_cancel_mid_pass_stops_early_and_keeps_committed_hashes(
    db: Path, hash_calls: list[Path]
) -> None:
    # Checkpoints per window: before it, and after its partial stage. #5 = before window 3, so
    # windows 1-2 (8 files) are fully hashed and flushed.
    with ScanIndex(db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(5)
        )
    assert len(hash_calls) == 2 * _WINDOW
    assert _hashed_rows(db) == 2 * _WINDOW  # durable, not lost with the cancelled pass


def test_cancel_between_stages_flushes_the_partial_hashes_already_computed(
    db: Path, hash_calls: list[Path]
) -> None:
    # #2 = after window 1's partial stage, BEFORE that window's own end-of-window flush.
    with ScanIndex(db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(2)
        )
    assert len(hash_calls) == _WINDOW
    assert _hashed_rows(db) == _WINDOW


def test_second_pass_after_cancel_hashes_exactly_the_remainder(
    db: Path, hash_calls: list[Path]
) -> None:
    with ScanIndex(db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(5)
        )
    already_hashed = _hashed_rows(db)
    hash_calls.clear()

    with ScanIndex(db) as index:
        clusters = dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)

    assert len(hash_calls) == _FILES - already_hashed
    assert len(set(hash_calls)) == len(hash_calls)  # no file hashed twice
    assert len(clusters) == _SIZES  # and the resumed pass still finds every duplicate pair


def test_restart_on_the_same_db_file_resumes_the_same_way(db: Path, hash_calls: list[Path]) -> None:
    """A brand-new ScanIndex (as after a process restart) sees the hashes the cancelled pass
    committed -- nothing lives only in memory."""
    with ScanIndex(db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(7)
        )
    committed = len(hash_calls)
    assert committed == 3 * _WINDOW
    hash_calls.clear()

    fresh = ScanIndex(db)  # not the object the cancelled pass used
    try:
        dedup.find_duplicate_clusters(fresh, min_reclaim_bytes=0)
    finally:
        fresh.close()
    assert len(hash_calls) == _FILES - committed


def test_a_file_changed_after_cancel_is_rehashed_not_served_stale(
    db: Path, hash_calls: list[Path], tmp_path: Path
) -> None:
    """ADR-0038 key stamping survives cancellation: a committed hash is only valid for its
    (size, mtime); a same-size rewrite (new mtime, new content) is hashed again on resume."""
    with ScanIndex(db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(5)
        )
    already_hashed = _hashed_rows(db)
    victim = tmp_path / "tree" / "d0" / "f0.bin"  # bucket 0: inside the flushed windows
    size = victim.stat().st_size
    victim.write_bytes(b"Z" * size)
    os.utime(victim, (victim.stat().st_atime, victim.stat().st_mtime + 100))
    with ScanIndex(db) as index:
        scan_tree(tmp_path / "tree", index)  # the rescan records the new mtime
    hash_calls.clear()

    with ScanIndex(db) as index:
        clusters = dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)

    assert victim in hash_calls
    assert len(hash_calls) == _FILES - already_hashed + 1  # remainder + the changed file only
    assert len(clusters) == _SIZES - 1  # the rewritten file no longer matches its old twin


def test_worker_initializer_runs_in_pool_threads_not_the_calling_thread(
    db: Path, hash_calls: list[Path]
) -> None:
    thread_ids: list[int] = []
    lock = threading.Lock()

    def initializer() -> None:
        with lock:
            thread_ids.append(threading.get_ident())

    with ScanIndex(db) as index:
        dedup.find_duplicate_clusters(index, min_reclaim_bytes=0, worker_initializer=initializer)
    assert thread_ids
    assert threading.get_ident() not in thread_ids


def test_index_can_be_closed_while_the_cancel_traceback_is_still_alive(
    db: Path, hash_calls: list[Path]
) -> None:
    """Teeth: without closing the candidates cursor in a `finally`, the unwinding exception's
    traceback keeps the SELECT generator alive and `ScanIndex.close()`'s WAL checkpoint raises
    "database table is locked" (found by the API-level cancel test)."""
    index = ScanIndex(db)
    caught: dedup.DedupCancelled | None = None
    try:
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, checkpoint=_cancel_on_nth_checkpoint(5)
        )
    except dedup.DedupCancelled as exc:
        caught = exc  # keeps exc.__traceback__ (and its frames) referenced past the except block
    assert caught is not None
    index.close()  # must not raise
