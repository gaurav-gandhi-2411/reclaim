"""ADR-0040 addendum: a dedup pass keeps the index WAL bounded, stops before a full disk, and logs
aggregates instead of one line per member.

Incident (2026-10-08, real 7.18 GB index): one long-lived SELECT cursor streamed the candidates
while hashes were flushed in short transactions on the same connection. The cursor pinned the
oldest read snapshot, `wal_autocheckpoint` could not advance past it, and the WAL grew to 13.15 GB.

Everything here is hermetic: synthetic indexes under `tmp_path`, hashing stubbed (no file reads
except in the real-file golden test), no sleeps.
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
import structlog

from reclaim import dedup
from reclaim.index import ScanIndex
from reclaim.models import DuplicateCluster, FileRecord
from reclaim.scanner import scan_tree

_BUCKETS = 3_000
_PER_BUCKET = 15  # 45k rows; sizes stay <= 128 KB so partial hash == full hash (one write each)
_SYNTHETIC_ROW_BYTES = 1_000  # sizes start here


class _Usage(NamedTuple):
    free: int


def _record(path: str, size: int, ino: int = 0, dev: int = 0) -> FileRecord:
    return FileRecord(
        path=Path(path),
        is_dir=False,
        size_bytes=size,
        attributes=0,
        ext=".bin",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=1_700_000_000.0,
        ctime=1_700_000_000.0,
        dev=dev,
        ino=ino,
    )


def _wal_bytes(db: Path) -> int:
    wal = db.with_name(db.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def _fake_partial(path: Path, size: int) -> str:
    """Same-size files hash alike, so every bucket is a cluster and writes happen for each file."""
    return f"{size:064x}"


@pytest.fixture
def synthetic_db(tmp_path: Path) -> Iterator[Path]:
    """~45k rows in 3k size buckets. Deleted with tmp_path at test end."""
    db = tmp_path / "synthetic.sqlite3"
    with ScanIndex(db) as index:
        records = (
            _record(f"C:/syn/d{(bucket * _PER_BUCKET + n) % 97}/f{bucket}_{n}.bin", size)
            for bucket in range(_BUCKETS)
            for n in range(_PER_BUCKET)
            for size in (_SYNTHETIC_ROW_BYTES + bucket,)
        )
        index.upsert_records(records, scanned_at=1.0)
    yield db


class _WalProbe:
    """Wraps both hash-store methods and records the WAL file size after every flush. With
    `check_cursor`, it also proves no read cursor is open at that instant: a TRUNCATE checkpoint
    cannot complete while this connection still has a SELECT open (it raises 'database table is
    locked' or reports busy)."""

    def __init__(self, index: ScanIndex, db: Path, *, check_cursor: bool) -> None:
        self.max_wal = 0
        self.flushes = 0
        self.cursor_open_at_flush = 0
        if check_cursor:
            index._conn.execute("PRAGMA busy_timeout=0")  # a pinned reader must fail fast
        for name in ("store_partial_hashes", "store_full_hashes"):
            setattr(index, name, self._wrap(index, db, getattr(index, name), check_cursor))

    def _wrap(self, index: ScanIndex, db: Path, real, check_cursor: bool):  # type: ignore[no-untyped-def]
        def wrapped(entries):  # type: ignore[no-untyped-def]
            written = real(entries)
            self.flushes += 1
            self.max_wal = max(self.max_wal, _wal_bytes(db))
            if check_cursor:
                try:
                    busy = index._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                except Exception:  # sqlite3.OperationalError: database table is locked
                    busy = 1
                self.cursor_open_at_flush += 1 if busy else 0
            return written

        return wrapped


def test_wal_stays_bounded_over_a_full_pass(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)
    with ScanIndex(synthetic_db) as index:
        probe = _WalProbe(index, synthetic_db, check_cursor=False)
        clusters = dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)

    assert len(clusters) == _BUCKETS
    assert probe.flushes > 10  # the pass really flushed in many short transactions
    # Pre-change the pass's open cursor pinned its snapshot, so the WAL held every frame of the
    # pass (13 GB on the real index); now frames are recycled and it stays near the autocheckpoint
    # size (1000 pages ~ 4 MB) -- a generous 8 MB bound.
    assert probe.max_wal < 8 * 1024 * 1024, (
        f"max WAL bytes over the pass: {probe.max_wal} ({probe.flushes} flushes)"
    )


def test_no_read_cursor_is_open_at_any_flush(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)
    with ScanIndex(synthetic_db) as index:
        probe = _WalProbe(index, synthetic_db, check_cursor=True)
        dedup.find_duplicate_clusters(index, min_reclaim_bytes=0)
    assert probe.flushes > 10
    assert probe.cursor_open_at_flush == 0


def test_wal_above_the_ceiling_is_truncated_at_the_next_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ceiling.sqlite3"
    with ScanIndex(db) as index:
        index.upsert_records([_record(f"C:/c/f{i}.bin", 5_000) for i in range(50)], scanned_at=1.0)
        # Hold the WAL open and large by disabling auto-checkpoint, then write a lot.
        index._conn.execute("PRAGMA wal_autocheckpoint=0")
        for round_ in range(40):
            index.store_partial_hashes(
                [
                    (Path(f"C:/c/f{i}.bin"), 5_000, 1_700_000_000.0, f"{round_:064x}")
                    for i in range(50)
                ]
            )
        assert _wal_bytes(db) > 64 * 1024
        after = index.checkpoint_wal(truncate_above_bytes=64 * 1024)
        assert after == 0
        # Below the ceiling nothing is truncated (PASSIVE only): the file keeps its size.
        for round_ in range(3):
            index.store_partial_hashes(
                [
                    (Path(f"C:/c/f{i}.bin"), 5_000, 1_700_000_000.0, f"{round_:064x}")
                    for i in range(50)
                ]
            )
        before = _wal_bytes(db)
        assert index.checkpoint_wal(truncate_above_bytes=10**9) == before > 0


def test_candidate_rows_equal_the_old_streaming_query_on_randomized_fixtures(
    tmp_path: Path,
) -> None:
    """Row-for-row, in order: chunked short SELECTs == the old single streaming SELECT, with
    hardlinked names, zero-ino rows, cloud placeholders, directories, zero-byte files and buckets
    below the materiality floor all present."""
    rng = random.Random(42)  # noqa: S311 -- deterministic fixture data, not security
    db = tmp_path / "rand.sqlite3"
    records: list[FileRecord] = []
    for n in range(4_000):
        size = rng.choice([0, 1, 7, 100, 4_096, 70_000, 300_000, 1_500_000]) + rng.randrange(60)
        ino = rng.choice([0, 0, rng.randrange(1, 400)])
        record = _record(f"C:/r/d{n % 31}/f{n}.bin", size, ino=ino, dev=rng.choice([1, 2]))
        records.append(record)
    with ScanIndex(db) as index:
        index.upsert_records(records, scanned_at=1.0)
        index._conn.execute("UPDATE files SET is_dir = 1 WHERE rowid % 53 = 0")
        index._conn.execute("UPDATE files SET is_cloud_placeholder = 1 WHERE rowid % 47 = 0")
        index._conn.commit()
        for floor in (0, 1, 50_000, 1_000_000):
            old = list(index.duplicate_size_candidates(min_reclaim_bytes=floor))
            sizes = index.duplicate_qualifying_sizes(min_reclaim_bytes=floor)
            for chunk in (1, 3, 256, 10**6):
                new = [
                    row
                    for start in range(0, len(sizes), chunk)
                    for row in index.duplicate_candidates_for_sizes(sizes[start : start + chunk])
                ]
                assert new == old, (floor, chunk)
        assert len(old) > 100  # sanity: the lowest-floor fixture really selects rows
        assert index.duplicate_qualifying_sizes(min_reclaim_bytes=10**12) == []


def _cluster_shape(clusters: list[DuplicateCluster]) -> list[tuple[object, ...]]:
    return [
        (c.full_hash, c.size_bytes, c.keep.path, tuple(d.path for d in c.duplicates))
        for c in clusters
    ]


def test_clusters_are_identical_for_any_fetch_chunk_size_with_hardlinks_and_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real files: twins in separate dirs, a hardlink pair, a below-floor bucket and an excluded
    tree. The chunk size is a pure performance knob; it must never change the answer."""
    tree = tmp_path / "tree"
    for n in range(40):
        for copy in range(3):
            d = tree / f"d{copy}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"f{n}.bin").write_bytes(bytes([n]) * (3_000 + n * 11))
    (tree / "excluded").mkdir()
    for n in range(5):
        (tree / "excluded" / f"x{n}.bin").write_bytes(bytes([n]) * (3_000 + n * 11))
    (tree / "tiny").mkdir()
    (tree / "tiny" / "a").write_bytes(b"x")
    (tree / "tiny" / "b").write_bytes(b"x")
    (tree / "d0" / "hl_a.bin").write_bytes(b"h" * 9_999)
    try:
        os.link(tree / "d0" / "hl_a.bin", tree / "d1" / "hl_b.bin")
    except OSError:
        pytest.skip("hardlinks unsupported here")
    (tree / "d2" / "hl_c.bin").write_bytes(b"h" * 9_999)  # a real third file of that size
    db = tmp_path / "golden.sqlite3"
    with ScanIndex(db) as index:
        scan_tree(tree, index)

    shapes = {}
    for chunk in (1, 2, 7, 256):
        monkeypatch.setattr(dedup, "_SIZES_PER_FETCH", chunk)
        monkeypatch.setattr(dedup, "_WINDOW_FILES", 9)
        with ScanIndex(db) as index:
            index._conn.execute("UPDATE files SET hash_size = NULL, partial_hash = NULL")
            index._conn.commit()
            shapes[chunk] = _cluster_shape(
                dedup.find_duplicate_clusters(
                    index, min_reclaim_bytes=2_000, exclusion_patterns=("*/excluded/*",)
                )
            )
    assert shapes[1] == shapes[2] == shapes[7] == shapes[256]
    clusters = shapes[256]
    assert len(clusters) == 41  # 40 triples + the hardlink pair/sibling bucket
    flat = " ".join(str(c) for c in clusters)
    assert "excluded" not in flat  # excluded tree never a member (nor keeper)
    assert "tiny" not in flat  # below the materiality floor
    hl = next(c for c in clusters if c[1] == 9_999)
    assert len({hl[2], *hl[3]}) == 3  # all three names stay in the cluster, as before


def test_cancel_mid_pass_leaves_hashes_committed_and_the_wal_small(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)
    state = {"calls": 0}

    def checkpoint() -> None:
        state["calls"] += 1
        if state["calls"] > 60:
            raise dedup.DedupCancelled

    with ScanIndex(synthetic_db) as index, pytest.raises(dedup.DedupCancelled):
        dedup.find_duplicate_clusters(index, min_reclaim_bytes=0, checkpoint=checkpoint)
    with ScanIndex(synthetic_db) as index:
        hashed = index._conn.execute(
            "SELECT COUNT(*) FROM files WHERE partial_hash IS NOT NULL"
        ).fetchone()[0]
        index._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    assert hashed > 0  # committed, not lost with the cancelled pass
    assert hashed < _BUCKETS * _PER_BUCKET  # and it really stopped early
    assert _wal_bytes(synthetic_db) < 4 * 1024 * 1024


def test_disk_guard_stops_cleanly_with_a_readable_error_and_keeps_what_is_hashed(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)
    reads = {"n": 0}

    def disk_usage(path: Path) -> _Usage:
        reads["n"] += 1
        assert path == synthetic_db.parent  # the INDEX's volume, not the cwd or the scan root
        # Plenty of space for the start check and the first 3 windows, then the disk "fills".
        return _Usage(free=50 * 1024**3 if reads["n"] <= 4 else 1 * 1024**3)

    with ScanIndex(synthetic_db) as index:
        with pytest.raises(dedup.DedupAborted, match="not enough free disk space") as err:
            dedup.find_duplicate_clusters(index, min_reclaim_bytes=0, disk_usage=disk_usage)
        hashed = index._conn.execute(
            "SELECT COUNT(*) FROM files WHERE partial_hash IS NOT NULL"
        ).fetchone()[0]
    assert "GB free" in str(err.value) and "Free up space" in str(err.value)
    assert hashed > 0  # flushed before stopping: a resume keeps it
    assert hashed < _BUCKETS * _PER_BUCKET


def test_disk_guard_refuses_before_hashing_anything_when_already_below_the_floor(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        dedup, "_compute_partial_hash", lambda p, s: calls.append(p) or _fake_partial(p, s)
    )
    with ScanIndex(synthetic_db) as index, pytest.raises(dedup.DedupAborted):
        dedup.find_duplicate_clusters(
            index, min_reclaim_bytes=0, disk_usage=lambda _p: _Usage(free=1024)
        )
    assert calls == []


def test_disk_guard_counts_the_current_wal_towards_the_floor(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)
    monkeypatch.setattr(dedup, "_MIN_FREE_DISK_BYTES", 1_000)
    with ScanIndex(synthetic_db) as index:
        monkeypatch.setattr(ScanIndex, "wal_size_bytes", lambda self: 10_000)
        with pytest.raises(dedup.DedupAborted):
            dedup.find_duplicate_clusters(
                index, min_reclaim_bytes=0, disk_usage=lambda _p: _Usage(free=5_000)
            )


def test_unmeasurable_volume_does_not_block_the_pass(
    synthetic_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedup, "_compute_partial_hash", _fake_partial)

    def boom(_path: Path) -> _Usage:
        raise OSError("no such volume")

    with ScanIndex(synthetic_db) as index:
        clusters = dedup.find_duplicate_clusters(index, min_reclaim_bytes=0, disk_usage=boom)
    assert len(clusters) == _BUCKETS


def test_excluded_members_log_one_info_summary_not_one_line_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dedup, "_dedup_ineligibility_reason", lambda *a, **k: "model_cache")
    keep = _record("C:/keep/f.bin", 10)
    clusters = [
        DuplicateCluster(
            full_hash="h",
            size_bytes=10,
            keep=keep,
            duplicates=tuple(_record(f"C:/dup/f{i}.bin", 10) for i in range(10_000)),
        )
    ]
    with structlog.testing.capture_logs() as logs:
        out = dedup.generate_duplicate_candidates(
            None,  # type: ignore[arg-type]  # clusters are supplied: the index is never touched
            _Config(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            clusters=clusters,
        )
    assert out == []
    info = [e for e in logs if e["log_level"] == "info"]
    assert len(info) <= 3
    summary = [e for e in info if e["event"] == "dedup.members_excluded"]
    assert len(summary) == 1
    assert summary[0]["count"] == 10_000
    assert summary[0]["reasons"] == {"model_cache": 10_000}
    assert len(summary[0]["sample"]) == 5
    debug = [e for e in logs if e["event"] == "dedup.member_excluded"]
    assert len(debug) == 10_000 and {e["log_level"] for e in debug} == {"debug"}


class _Config:
    """Only what `generate_duplicate_candidates` reads before the (never reached) safety step."""

    class categories:
        class duplicates:
            min_reclaim_bytes = 0

        class model_caches:
            paths: tuple[str, ...] = ()


def test_unreadable_files_log_one_summary_with_a_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "unreadable.sqlite3"
    with ScanIndex(db) as index:
        index.upsert_records(
            [_record(f"C:/gone/b{b}/f{n}.bin", 2_000 + b) for b in range(20) for n in range(2)],
            scanned_at=1.0,
        )

    def unreadable(path: Path, size: int) -> str:
        raise OSError(f"cannot read {path.name}")

    monkeypatch.setattr(dedup, "_compute_partial_hash", unreadable)
    with ScanIndex(db) as index, structlog.testing.capture_logs() as logs:
        assert dedup.find_duplicate_clusters(index, min_reclaim_bytes=0) == []
    summary = [e for e in logs if e["event"] == "dedup.hash_unreadable"]
    assert len(summary) == 1 and summary[0]["log_level"] == "info"
    assert summary[0]["count"] == 40
    assert len(summary[0]["sample"]) == 5
    per_file = [e for e in logs if e["event"] == "dedup.hash_unreadable_file"]
    assert len(per_file) == 40 and {e["log_level"] for e in per_file} == {"debug"}
    assert len([e for e in logs if e["log_level"] in ("info", "warning")]) <= 5
