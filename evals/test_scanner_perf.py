from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NUM_SUBDIRS = 25
_FILES_PER_SUBDIR = 160  # ~4000 files total

# Conservative smoke-test floor: an order of magnitude below the spec's real target
# (>=100K files/min == ~1667 files/sec on GG's actual SSD). This only proves the scanner
# isn't accidentally O(n^2) or worse on a shared/virtualized CI runner -- it is NOT a
# measurement of the real spec perf number, which can only be honestly taken on real hardware
# with a real large tree (see reclaim-spec.md "Perf budget"). Never report this smoke-test
# number as if it were the spec's >=100K files/min figure.
_MIN_FILES_PER_SECOND_SMOKE_FLOOR = 150.0
# Deliberately NOT a wall-clock-ratio assertion (the brief's illustrative "<50%" example):
# correctness requires stat()-ing every file every scan to learn its current (size, mtime)
# before we can even decide it's unchanged (see scanner.py's "no directory-mtime subtree
# skip" note), so an incremental rescan's wall time is dominated by the same scandir+stat
# cost as a full scan -- only the DB-write phase differs. Measured locally across repeated
# runs that difference is real but small and noisy on this smoke-scale tree (roughly
# 85-90% of full-scan wall time, sometimes higher from CI-runner jitter), so a tight ratio
# gate here would be exactly the kind of flaky assertion house rule 81 says to fix or delete
# on sight. The deterministic, non-noisy proof that the skip mechanism actually worked is
# `files_written == 0` below; wall time is reported for visibility only, with just a wide
# non-regression sanity bound (never dramatically slower than a full scan).
_MAX_INCREMENTAL_WALL_TIME_SANITY_MULTIPLE = 1.5


def _build_tree(root: Path) -> int:
    count = 0
    for i in range(_NUM_SUBDIRS):
        subdir = root / f"dir_{i:03d}"
        subdir.mkdir(parents=True)
        for j in range(_FILES_PER_SUBDIR):
            (subdir / f"file_{j:04d}.txt").write_bytes(b"x" * 64)
            count += 1
    return count


def test_full_scan_clears_conservative_floor_and_incremental_skips_all_writes(
    tmp_path: Path,
) -> None:
    """Perf SMOKE test only, not the real perf validation.

    Proves the scanner isn't pathologically slow (regression tripwire) and that an unchanged
    incremental rescan writes zero rows (the deterministic proof the skip mechanism works).
    This is NOT a measurement of the spec's real >=100K files/min target — that number can
    only be honestly taken on GG's actual SSD with a real large tree, never on a
    shared/virtualized CI runner (see reclaim-spec.md "Perf budget" and house rule 65b on
    metric provenance).
    """
    root = tmp_path / "perf_root"
    file_count = _build_tree(root)

    db_path = tmp_path / "index.sqlite3"
    with ScanIndex(db_path) as index:
        full_start = time.perf_counter()
        full_stats = scan_tree(root, index, incremental=False)
        full_elapsed = time.perf_counter() - full_start

        incremental_start = time.perf_counter()
        incremental_stats = scan_tree(root, index, incremental=True)
        incremental_elapsed = time.perf_counter() - incremental_start

    files_per_second = file_count / full_elapsed
    print(  # noqa: T201 -- perf smoke numbers; run with `pytest -s` to see them
        f"\n[scanner perf smoke] files={file_count} full={full_elapsed:.3f}s "
        f"({files_per_second:.0f} files/sec, CI-runner smoke number only) "
        f"incremental={incremental_elapsed:.3f}s "
        f"({incremental_elapsed / full_elapsed:.2%} of full)"
    )

    assert full_stats.entries_total >= file_count
    # The deterministic proof the incremental-skip mechanism works: zero rows written when
    # nothing on disk changed. Not a timing assertion, so it can't flake on CI-runner jitter.
    assert incremental_stats.files_written == 0
    assert incremental_stats.files_unchanged == full_stats.entries_total
    assert files_per_second >= _MIN_FILES_PER_SECOND_SMOKE_FLOOR, (
        f"full scan rate {files_per_second:.0f} files/sec fell below the conservative CI "
        f"smoke floor of {_MIN_FILES_PER_SECOND_SMOKE_FLOOR} files/sec -- this floor is not "
        "the real spec perf number, just a regression tripwire"
    )
    assert incremental_elapsed < full_elapsed * _MAX_INCREMENTAL_WALL_TIME_SANITY_MULTIPLE, (
        f"incremental rescan ({incremental_elapsed:.3f}s) was dramatically slower than the "
        f"full scan ({full_elapsed:.3f}s), which shouldn't happen even accounting for jitter"
    )
