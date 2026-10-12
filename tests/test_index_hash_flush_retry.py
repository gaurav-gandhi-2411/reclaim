"""`ScanIndex` write-contention hardening (incident 2026-10-08): hash flushes retry a transient
`database is locked`, a failing `close()` never masks the exception already propagating, and a
caller can ask for a longer busy timeout. Hermetic: tmp_path databases, a second connection as
the "other writer", no real sleeping (the backoff is a patched seam)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from reclaim import index as index_module
from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_close_failure_without_another_exception_still_propagates(tmp_path: Path) -> None:
    index = ScanIndex(tmp_path / "i.sqlite3")

    def _failing_close() -> None:
        raise sqlite3.OperationalError("database table is locked")

    index.close = _failing_close  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError, match="table is locked"), index:
        pass


# (vi) ------------------------------------------------------------------------------------------


def _indexed_file(tmp_path: Path) -> tuple[Path, Path, int, float]:
    tree = tmp_path / "tree"
    _write(tree / "a.bin", b"a" * 100)
    db = tmp_path / "i.sqlite3"
    with ScanIndex(db) as index:
        scan_tree(tree, index)
        row = index._conn.execute("SELECT path, size, mtime FROM files WHERE is_dir = 0").fetchone()
    return db, Path(row["path"]), int(row["size"]), float(row["mtime"])


def _hold_write_lock(db: Path) -> sqlite3.Connection:
    blocker = sqlite3.connect(db, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def test_hash_flush_retries_a_transient_lock_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, path, size, mtime = _indexed_file(tmp_path)
    blocker = _hold_write_lock(db)
    pauses: list[float] = []

    def _release_on_first_pause(seconds: float) -> None:
        pauses.append(seconds)
        blocker.execute("ROLLBACK")  # the other writer finishes while we back off

    monkeypatch.setattr(index_module, "_backoff_sleep", _release_on_first_pause)
    with ScanIndex(db, busy_timeout_ms=20) as index:
        assert index.store_partial_hashes([(path, size, mtime, "deadbeef")]) == 1
        stored = index._conn.execute(
            "SELECT partial_hash FROM files WHERE path = ?", (path.as_posix(),)
        )
        assert stored.fetchone()[0] == "deadbeef"
    blocker.close()
    assert pauses == [0.5], "exactly one retry was needed"


def test_hash_flush_gives_up_after_three_tries_with_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, path, size, mtime = _indexed_file(tmp_path)
    blocker = _hold_write_lock(db)
    pauses: list[float] = []
    monkeypatch.setattr(index_module, "_backoff_sleep", pauses.append)

    index = ScanIndex(db, busy_timeout_ms=20)
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        index.store_full_hashes([(path, size, mtime, "deadbeef")])
    assert str(excinfo.value) == "database is locked"
    assert pauses == [0.5, 2.0], "3 tries = 2 backoffs, then the original error"
    blocker.execute("ROLLBACK")
    blocker.close()
    index.close()


def test_busy_timeout_ms_sets_the_connection_wait(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "i.sqlite3", busy_timeout_ms=60_000) as index:
        assert index._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
    with ScanIndex(tmp_path / "j.sqlite3") as index:
        assert index._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000


def test_close_failure_does_not_mask_the_exception_already_propagating(tmp_path: Path) -> None:
    index = ScanIndex(tmp_path / "i.sqlite3")

    def _failing_close() -> None:
        raise sqlite3.OperationalError("database table is locked")

    index.close = _failing_close  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="original"), index:
        raise ValueError("original")
