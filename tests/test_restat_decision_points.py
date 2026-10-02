from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
import reclaim.detectors as detectors_module
import reclaim.scanner as scanner_module
from reclaim.dedup import find_duplicate_clusters
from reclaim.detectors import (
    detect_large_logs,
    detect_old_installers,
    detect_temp_and_browser_caches,
)
from reclaim.dirlist import _FILETIME_UNIX_EPOCH_OFFSET, ListedEntry
from reclaim.freshstat import fresh_stat_signature
from reclaim.index import ScanIndex
from reclaim.models import FileRecord
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

_DAY = 86400.0
_MIN_TEMP_AGE_HOURS = 24.0 * 7


@pytest.fixture
def index(tmp_path: Path) -> Iterator[ScanIndex]:
    idx = ScanIndex(tmp_path / "index.sqlite3")
    try:
        yield idx
    finally:
        idx.close()


def _stale_listing(monkeypatch: pytest.MonkeyPatch, names: set[str], *, age_days: float) -> None:
    """Injects the ADR-0035 hazard through the `_list_directory_or_none` seam: the real
    directory listing is taken, then the named files' last-write time is rewritten to
    `age_days` ago -- what the listing reports for a file open for write whose directory entry
    has not been refreshed. (The real lag could not be reproduced on this machine; see the PR
    report. The detector-side behaviour under that lag is what is under test.)"""
    real = scanner_module._list_directory_or_none
    stale_ft = int((time.time() - age_days * _DAY) * 10_000_000) + _FILETIME_UNIX_EPOCH_OFFSET

    def patched(
        long_dir: str, volume_serial: int | None = None
    ) -> tuple[int, list[ListedEntry]] | None:
        result = real(long_dir, volume_serial)
        if result is None:
            return None
        serial, entries = result
        out: list[ListedEntry] = []
        for e in entries:
            if e[0] in names:
                e = (e[0], e[1], e[2], stale_ft, e[4], e[5], e[6])
            out.append(e)
        return serial, out

    monkeypatch.setattr(scanner_module, "_list_directory_or_none", patched)


def _scan(root: Path, index: ScanIndex) -> None:
    scan_tree(root, index, incremental=False)


def _temp_candidates(index: ScanIndex, root: Path) -> list[str]:
    found = detect_temp_and_browser_caches(
        index,
        [],
        [str(root)],
        min_temp_root_age_hours=_MIN_TEMP_AGE_HOURS,
        now=time.time(),
    )
    return sorted(c.path.name for c in found)


@pytest.fixture
def no_restat(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Mutation switch: disables the helper in the detectors module (as if the fix were
    reverted). `None` is the helper's documented 'could not stat' result."""

    def disable() -> None:
        monkeypatch.setattr(detectors_module, "fresh_stat_signature", lambda _p: None)

    return disable


# --- the helper --------------------------------------------------------------------------


def test_fresh_stat_signature_matches_os_stat_and_is_none_when_unreadable(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 123)
    fresh = fresh_stat_signature(f)
    st = f.stat()
    assert fresh is not None
    assert (fresh.size, fresh.mtime, fresh.mtime_ns) == (st.st_size, st.st_mtime, st.st_mtime_ns)
    assert fresh_stat_signature(tmp_path / "missing.bin") is None


# --- (A) temp min-age: file child --------------------------------------------------------


def test_temp_file_open_for_write_with_stale_listing_mtime_is_not_proposed(
    tmp_path: Path,
    index: ScanIndex,
    monkeypatch: pytest.MonkeyPatch,
    no_restat: Callable[[], None],
) -> None:
    root = tmp_path / "scan" / "Temp"
    root.mkdir(parents=True)
    live = root / "live.tmp"
    done = root / "done.tmp"
    live.write_bytes(b"a")
    done.write_bytes(b"b")
    old = time.time() - 30 * _DAY
    os.utime(done, (old, old))
    _stale_listing(monkeypatch, {"live.tmp"}, age_days=30)

    with live.open("ab") as writer:  # held open for write across the scan and the detector
        writer.write(b"more")
        writer.flush()
        _scan(root.parent, index)
        indexed = index.get_record(live)
        assert indexed is not None
        assert time.time() - indexed.mtime > 29 * _DAY  # the index lags...
        assert time.time() - live.stat().st_mtime < 3600  # ...the live stat does not

        # With the re-stat: the live file is kept, the genuinely old one still proposed.
        assert _temp_candidates(index, root) == ["done.tmp"]

        # Teeth: with the helper disabled the stale listing wrongly proposes the live file.
        no_restat()
        assert _temp_candidates(index, root) == ["done.tmp", "live.tmp"]


# --- (C) temp min-age: newest file in a subtree is the open-for-write one ------------------


def test_temp_dir_whose_newest_file_is_open_for_write_is_not_proposed(
    tmp_path: Path,
    index: ScanIndex,
    monkeypatch: pytest.MonkeyPatch,
    no_restat: Callable[[], None],
) -> None:
    root = tmp_path / "scan" / "Temp"
    busy = root / "busy-cache" / "v1"
    idle = root / "idle-cache"
    busy.mkdir(parents=True)
    idle.mkdir()
    (busy / "settled.bin").write_bytes(b"s")
    live = busy / "journal.bin"
    live.write_bytes(b"j")
    (idle / "x.bin").write_bytes(b"x")
    old = time.time() - 30 * _DAY
    for p in (busy / "settled.bin", idle / "x.bin", busy, busy.parent, idle):
        os.utime(p, (old, old))
    _stale_listing(monkeypatch, {"journal.bin"}, age_days=30)

    with live.open("ab") as writer:
        writer.write(b"more")
        writer.flush()
        _scan(root.parent, index)
        assert index.subtree_newest_mtime(busy.parent) < time.time() - 29 * _DAY
        assert _temp_candidates(index, root) == ["idle-cache"]
        no_restat()
        assert _temp_candidates(index, root) == ["busy-cache", "idle-cache"]


# --- old_installers / large_logs -----------------------------------------------------------


def test_old_installer_open_for_write_with_stale_listing_is_not_proposed(
    tmp_path: Path,
    index: ScanIndex,
    monkeypatch: pytest.MonkeyPatch,
    no_restat: Callable[[], None],
) -> None:
    root = tmp_path / "scan" / "Downloads"
    root.mkdir(parents=True)
    live = root / "partial.exe"
    live.write_bytes(b"MZ")
    _stale_listing(monkeypatch, {"partial.exe"}, age_days=200)
    with live.open("ab") as writer:  # a download still in progress
        writer.write(b"more")
        writer.flush()
        _scan(root.parent, index)
        assert detect_old_installers(index, max_age_days=90, now=time.time()) == []
        no_restat()
        found = detect_old_installers(index, max_age_days=90, now=time.time())
        assert [c.path.name for c in found] == ["partial.exe"]


def test_stale_log_open_for_write_is_not_proposed_and_idle_one_is(
    tmp_path: Path,
    index: ScanIndex,
    monkeypatch: pytest.MonkeyPatch,
    no_restat: Callable[[], None],
) -> None:
    root = tmp_path / "scan" / "logs"
    root.mkdir(parents=True)
    live = root / "svc.log"
    idle = root / "idle.log"
    live.write_bytes(b"l" * 500)
    idle.write_bytes(b"i" * 500)
    old = time.time() - 60 * _DAY
    os.utime(idle, (old, old))
    _stale_listing(monkeypatch, {"svc.log"}, age_days=60)
    with live.open("ab") as writer:
        writer.write(b"x")
        writer.flush()
        _scan(root.parent, index)
        kwargs = {"min_size_bytes": 100, "stale_days": 30, "now": time.time()}
        assert [c.path.name for c in detect_large_logs(index, **kwargs)] == ["idle.log"]
        no_restat()
        names = sorted(c.path.name for c in detect_large_logs(index, **kwargs))
        assert names == ["idle.log", "svc.log"]


def test_large_log_that_shrank_below_threshold_is_not_proposed(
    tmp_path: Path, index: ScanIndex
) -> None:
    root = tmp_path / "scan" / "logs"
    root.mkdir(parents=True)
    log = root / "app.log"
    log.write_bytes(b"l" * 500)
    old = time.time() - 60 * _DAY
    os.utime(log, (old, old))
    _scan(root.parent, index)
    log.write_bytes(b"l" * 10)  # truncated after the scan
    os.utime(log, (old, old))
    got = detect_large_logs(index, min_size_bytes=100, stale_days=30, now=time.time())
    assert got == []


# --- (B) dedup hash-cache reuse -------------------------------------------------------------


def test_hash_cache_entry_is_reused_only_when_fresh_stat_matches(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(parents=True)
    a, b = root / "a.bin", root / "b.bin"
    a.write_bytes(b"same-content-0123456789")
    b.write_bytes(b"same-content-0123456789")
    _scan(root, index)

    calls = {"partial": 0, "full": 0}
    real_partial, real_full = dedup_module._compute_partial_hash, dedup_module._compute_full_hash

    def partial(*args: object, **kwargs: object) -> str:
        calls["partial"] += 1
        return real_partial(*args, **kwargs)  # type: ignore[arg-type]

    def full(*args: object, **kwargs: object) -> str:
        calls["full"] += 1
        return real_full(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", partial)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", full)

    assert len(find_duplicate_clusters(index, min_reclaim_bytes=0)) == 1
    first = dict(calls)
    # perf/dedup-warmup: a file this small is hashed whole by the partial stage, so its partial
    # digest is reused as the full digest instead of being read a second time.
    assert first == {"partial": 2, "full": 0}

    # Unchanged on disk: every digest is reused, nothing rehashed (no needless work).
    assert len(find_duplicate_clusters(index, min_reclaim_bytes=0)) == 1
    assert calls == first

    # b changes on disk AFTER the scan (same size, newer mtime) -- the index still holds the
    # old (size, mtime), exactly as for a file the listing showed stale. The cache entry's key
    # matches the index row, so only the live stat can reveal it: b is rehashed, and the
    # clusters reflect the new content (no longer a duplicate of a).
    b.write_bytes(b"DIFF-content-0123456789")
    os.utime(b, (time.time() + 5, time.time() + 5))
    assert find_duplicate_clusters(index, min_reclaim_bytes=0) == []
    assert calls["partial"] == first["partial"] + 1  # only b; a's entry was still valid


def test_stale_hash_cache_would_be_reused_without_the_restat(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teeth for (B): with the helper disabled the same scenario wrongly reports the changed
    file as still a duplicate."""
    root = tmp_path / "data"
    root.mkdir(parents=True)
    a, b = root / "a.bin", root / "b.bin"
    a.write_bytes(b"same-content-0123456789")
    b.write_bytes(b"same-content-0123456789")
    _scan(root, index)
    assert len(find_duplicate_clusters(index, min_reclaim_bytes=0)) == 1
    b.write_bytes(b"DIFF-content-0123456789")
    os.utime(b, (time.time() + 5, time.time() + 5))
    monkeypatch.setattr(dedup_module, "fresh_stat_signature", lambda _p: None)
    assert len(find_duplicate_clusters(index, min_reclaim_bytes=0)) == 1  # the stale answer


# --- (D) cost budget: O(candidate set) -------------------------------------------------------


def _synthetic(path: str, mtime: float, *, is_dir: bool = False) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=0 if is_dir else 10,
        attributes=0,
        ext="" if is_dir else p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=mtime,
    )


def _count_stats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = os.stat

    def counting(path: object, *a: object, **k: object) -> os.stat_result:
        seen.append(str(path))
        return real(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", counting)
    return seen


def test_restat_count_is_proportional_to_candidates_not_to_index_size(
    index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    old, young = now - 30 * _DAY, now - 3600
    root = "C:/Restat/Temp"
    records = [_synthetic(f"{root}/young{i}.tmp", young) for i in range(500)]
    records += [_synthetic(f"{root}/old{i}.tmp", old) for i in range(3)]  # 3 candidate files
    records += [_synthetic(f"{root}/oldcache{j}", old, is_dir=True) for j in range(2)]
    records += [_synthetic(f"{root}/oldcache{j}/f{i}.bin", old) for j in range(2) for i in range(4)]
    # 5,000 unrelated rows elsewhere in the index: must never be stat'ed.
    records += [_synthetic(f"C:/Elsewhere/f{i}.dat", old) for i in range(5000)]
    index.upsert_records(records, scanned_at=now)

    seen = _count_stats(monkeypatch)
    found = detect_temp_and_browser_caches(
        index, [], [root], min_temp_root_age_hours=_MIN_TEMP_AGE_HOURS, now=now
    )
    assert len(found) == 5  # 3 old files + 2 old cache dirs (stat failures keep the listing)
    assert len(seen) == 3 + 2 * 4  # exactly the candidates' files; 500 young + 5,000 other: 0
    assert not any("Elsewhere" in s or "young" in s for s in seen)


def test_temp_child_restat_is_capped_per_directory(
    index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    old = now - 30 * _DAY
    root = "C:/Restat/Temp"
    records = [_synthetic(f"{root}/big", old, is_dir=True)]
    records += [_synthetic(f"{root}/big/f{i}.bin", old - i) for i in range(50)]
    index.upsert_records(records, scanned_at=now)
    monkeypatch.setattr(detectors_module, "_TEMP_CHILD_RESTAT_CAP", 7)
    seen = _count_stats(monkeypatch)
    detect_temp_and_browser_caches(
        index, [], [root], min_temp_root_age_hours=_MIN_TEMP_AGE_HOURS, now=now
    )
    assert len(seen) == 7
    assert all(s.endswith(tuple(f"f{i}.bin" for i in range(7))) for s in seen)  # newest 7
