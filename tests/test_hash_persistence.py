"""Pins the dedup hash cache's persistence contract, from the "installed index had ZERO stored
hashes" investigation (2026-10-02, docs/architecture/adr/0038-hash-persistence-contract.md):

1. A rescan (incremental or full) never wipes stored hashes -- a changed file keeps its row but its
   (size, mtime) key no longer matches, so the cache refuses to serve it.
2. Hashes are durable per completed window: a process killed mid-run (no close, no checkpoint)
   leaves every flushed window's hashes in the index.
3. A restart reuses those hashes instead of recomputing them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import reclaim
from reclaim import dedup
from reclaim.index import ScanIndex, cached_partial_hash
from reclaim.scanner import scan_tree

_SIZES = 12  # 12 size buckets x 2 identical files each = 24 candidate files
_WINDOW = 4  # files per flushed window in the killed run -> 2 buckets per window
_KILL_AT = 17  # the 17th partial hash aborts the process: windows 1-4 (16 files) are flushed


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


def test_rescan_keeps_stored_hashes_and_a_changed_file_is_not_served_stale(tmp_path: Path) -> None:
    """Teeth: an upsert that resets/replaces the hash columns (INSERT OR REPLACE, or hash_* in
    the DO UPDATE set) drops this count to 0 on the first rescan."""
    tree = _make_tree(tmp_path)
    db = tmp_path / "index.sqlite3"
    with ScanIndex(db) as index:
        scan_tree(tree, index)
    with ScanIndex(db) as index:
        dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert _hashed_rows(db) == _SIZES * 2

    with ScanIndex(db) as index:
        scan_tree(tree, index, incremental=True)
    assert _hashed_rows(db) == _SIZES * 2
    with ScanIndex(db) as index:
        scan_tree(tree, index, incremental=False)  # rewrites EVERY row through the upsert
    assert _hashed_rows(db) == _SIZES * 2

    victim = tree / "d0" / "f0.bin"
    victim.write_bytes(b"Z" * 5000)  # new size + mtime
    with ScanIndex(db) as index:
        scan_tree(tree, index)
        record_row = index._conn.execute(
            "SELECT size, mtime FROM files WHERE path = ?", (victim.as_posix(),)
        ).fetchone()
        entry = index.load_hash_cache()[victim.as_posix()]
    stale = cached_partial_hash(
        entry, current_size=record_row["size"], current_mtime=record_row["mtime"]
    )
    assert stale is None
    untouched = tree / "d1" / "f1.bin"
    with ScanIndex(db) as index:
        stat = untouched.stat()
        entry = index.load_hash_cache()[untouched.as_posix()]
    fresh = cached_partial_hash(entry, current_size=stat.st_size, current_mtime=stat.st_mtime)
    assert fresh is not None


_KILLED_RUN = """
import os, sys, threading
from pathlib import Path
from reclaim import dedup
from reclaim.index import ScanIndex

dedup._WINDOW_FILES = {window}
real = dedup._compute_partial_hash
lock = threading.Lock()
calls = [0]

def counting(path, size):
    with lock:
        calls[0] += 1
        n = calls[0]
    if n == {kill_at}:
        os._exit(9)  # no close(), no checkpoint, no atexit: a hard kill
    return real(path, size)

dedup._compute_partial_hash = counting
index = ScanIndex(Path(sys.argv[1]))
dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)
"""


def test_killed_run_keeps_flushed_windows_and_restart_reuses_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teeth: deferring the flush to the end of the run (the 'never reaches its first persist'
    hypothesis) leaves 0 rows after the kill, and the restart then recomputes all 24 files."""
    tree = _make_tree(tmp_path)
    db = tmp_path / "index.sqlite3"
    with ScanIndex(db) as index:
        scan_tree(tree, index)

    src_root = str(Path(reclaim.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src_root}
    code = _KILLED_RUN.format(window=_WINDOW, kill_at=_KILL_AT)
    result = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", code, str(db)],
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 9, result.stderr.decode(errors="replace")[-2000:]

    persisted = _hashed_rows(db)
    assert persisted == (_KILL_AT - 1), persisted  # windows 1-4, flushed before the kill

    recomputed: list[Path] = []
    real = dedup._compute_partial_hash

    def counting(path: Path, size: int) -> str:
        recomputed.append(path)
        return real(path, size)

    monkeypatch.setattr(dedup, "_compute_partial_hash", counting)
    with ScanIndex(db) as index:
        clusters = dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert len(recomputed) == _SIZES * 2 - persisted
    assert len(clusters) == _SIZES
    assert _hashed_rows(db) == _SIZES * 2
