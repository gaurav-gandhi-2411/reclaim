from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import pytest

from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NUM_SUBDIRS = 25
_FILES_PER_SUBDIR = 160  # ~4000 files total
_TRIALS = 5  # median-of-N (house rule 81: fix or delete a flaky test, don't widen the margin
# to paper over it). A single (full, incremental) timing pair is one sample from a noisy
# distribution -- one GC pause, one other process/test borrowing CPU for 100ms, and a real
# 90%-of-full-scan incremental rescan reads as "dramatically slower," a false flake, not a
# real regression. The median of 5 independent trials is robust to exactly that: an outlier
# trial (jitter in either direction) can't move the median unless a MAJORITY of trials are
# affected, which is what an actual regression would look like, not what one bad tick does.

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

    Timing assertions run on the MEDIAN of `_TRIALS` independent (full, incremental) pairs,
    not a single sample — a fresh `ScanIndex` per trial (same on-disk tree, reused — building
    it is the expensive, non-timed setup step) so each trial's "full scan" is genuinely a full
    scan against an empty index, not accidentally incremental against a warm one from a prior
    trial.
    """
    root = tmp_path / "perf_root"
    file_count = _build_tree(root)

    full_elapsed_samples: list[float] = []
    incremental_elapsed_samples: list[float] = []

    for trial in range(_TRIALS):
        db_path = tmp_path / f"index_{trial}.sqlite3"
        with ScanIndex(db_path) as index:
            full_start = time.perf_counter()
            full_stats = scan_tree(root, index, incremental=False)
            full_elapsed = time.perf_counter() - full_start

            incremental_start = time.perf_counter()
            incremental_stats = scan_tree(root, index, incremental=True)
            incremental_elapsed = time.perf_counter() - incremental_start

        assert full_stats.entries_total >= file_count
        # The deterministic proof the incremental-skip mechanism works: zero rows written
        # when nothing on disk changed. Not a timing assertion, checked every trial — it
        # can't flake on CI-runner jitter, so there's no reason to only check it once.
        assert incremental_stats.files_written == 0
        assert incremental_stats.files_unchanged == full_stats.entries_total

        full_elapsed_samples.append(full_elapsed)
        incremental_elapsed_samples.append(incremental_elapsed)

    median_full_elapsed = statistics.median(full_elapsed_samples)
    median_incremental_elapsed = statistics.median(incremental_elapsed_samples)
    median_files_per_second = file_count / median_full_elapsed

    print(  # noqa: T201 -- perf smoke numbers; run with `pytest -s` to see them
        f"\n[scanner perf smoke] files={file_count} trials={_TRIALS} "
        f"full_samples={[f'{s:.3f}' for s in full_elapsed_samples]} "
        f"incremental_samples={[f'{s:.3f}' for s in incremental_elapsed_samples]} "
        f"median_full={median_full_elapsed:.3f}s "
        f"({median_files_per_second:.0f} files/sec, CI-runner smoke number only) "
        f"median_incremental={median_incremental_elapsed:.3f}s "
        f"({median_incremental_elapsed / median_full_elapsed:.2%} of median full)"
    )

    assert median_files_per_second >= _MIN_FILES_PER_SECOND_SMOKE_FLOOR, (
        f"median full-scan rate {median_files_per_second:.0f} files/sec across {_TRIALS} "
        f"trials fell below the conservative CI smoke floor of "
        f"{_MIN_FILES_PER_SECOND_SMOKE_FLOOR} files/sec -- this floor is not the real spec "
        "perf number, just a regression tripwire"
    )
    sanity_bound = median_full_elapsed * _MAX_INCREMENTAL_WALL_TIME_SANITY_MULTIPLE
    assert median_incremental_elapsed < sanity_bound, (
        f"median incremental rescan ({median_incremental_elapsed:.3f}s across {_TRIALS} "
        f"trials) was dramatically slower than the median full scan "
        f"({median_full_elapsed:.3f}s), which shouldn't happen even accounting for jitter -- "
        "the median-of-5 design means this is very unlikely to be a fluke"
    )
