from __future__ import annotations

import gc
import os
import tracemalloc
from pathlib import Path

import pytest

from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# Wave 1 finding #1 (2026-07-30 real-disk diagnosis): before the `_BatchIndexWriter` rewrite,
# `scan_tree` accumulated one `FileRecord` per visited entry in a single Python list for the
# ENTIRE walk, written once in a single transaction at the very end. Measured on a real
# 2.67M-file scan of a live home directory (OneDrive + dozens of git repos/venvs): 5,085MB peak
# RSS — extrapolating past typical consumer RAM on a genuine full-drive scan (10-20M+ files).
# This eval proves the fix structurally: peak memory must not track file count.

# A FIXED, small top-level directory count in both tiers, and a FIXED small fan-out per leaf
# directory -- deliberately, so file-count scaling happens purely via MORE sequential
# batch-flush cycles within each worker's subtree walk, isolated from two other, separate memory
# effects this eval is NOT about:
#   (1) more top-level directories -> more concurrent workers -> more legitimate concurrent
#       memory use (an earlier version of this eval varied directory count 10 -> 40 and measured
#       a 3.1x ratio for a 4x file-count ratio, dominated by worker-count scaling, not batching);
#   (2) `entries = list(os.scandir(...))` materializing one directory's ENTIRE listing at once
#       (existing, pre-Wave-1 code in `_walk_subtree`/`count_entries_fast`/`scan_tree`'s
#       top-level loop) -- a real but different, per-directory-scoped, transient cost that scales
#       with entries-in-ONE-directory, not with total scan size held for the whole walk. A flat
#       fixture (all files in a handful of large directories) conflates this with finding #1;
#       nesting into many small leaf directories (fixed fan-out) keeps every single
#       `os.scandir()` call's materialized list small and constant regardless of total file
#       count, so only the cross-batch-flush behavior this fix actually changed is measured.
_TOP_LEVEL_DIRS = 4
_FILES_PER_LEAF_DIR = 100
_SMALL_LEAF_DIRS_PER_TOP = 25  # 4 * 25 * 100 = 10,000 files
_LARGE_LEAF_DIRS_PER_TOP = 100  # 4 * 100 * 100 = 40,000 files -- 4x the small tier

# `_WRITE_BATCH_SIZE` (scanner.py) is 5000 -- both tiers here cross multiple mid-walk flushes
# per worker (10,000/4 = 2,500 and 40,000/4 = 10,000 records per top-level worker respectively).
# Generous enough to absorb real per-FileRecord Python object overhead without being so loose it
# stops catching a real regression (the old bug would show close to the full 4x file-count
# ratio, not comfortably under 2x).
_ABSOLUTE_PEAK_CEILING_BYTES = 150 * 1024 * 1024


def _build_nested_tree(root: Path, *, leaf_dirs_per_top: int) -> int:
    """`_TOP_LEVEL_DIRS` top-level directories (fixed — see module comment), each holding
    `leaf_dirs_per_top` leaf subdirectories of exactly `_FILES_PER_LEAF_DIR` files apiece — total
    file count scales via `leaf_dirs_per_top` alone, with every single `os.scandir()` call
    anywhere in the walk always seeing the same small, constant number of entries."""
    count = 0
    for t in range(_TOP_LEVEL_DIRS):
        top = root / f"top_{t:02d}"
        for leaf_index in range(leaf_dirs_per_top):
            leaf = top / f"leaf_{leaf_index:04d}"
            leaf.mkdir(parents=True)
            for j in range(_FILES_PER_LEAF_DIR):
                (leaf / f"file_{j:03d}.txt").write_bytes(b"x" * 64)
                count += 1
    return count


def _measure_peak_traced_memory_during_scan(root: Path, db_path: Path) -> int:
    """Python-level allocation tracking (tracemalloc), not OS-level RSS sampling: this measures
    exactly what was diagnosed as broken — how many Python objects `scan_tree` itself
    accumulates — with no extra dependency (psutil) or external process needed to sample it.
    `incremental=False` isolates this fix from the separate, smaller, already-documented
    `stat_cache` cost (only loaded when `incremental=True` — see `scan_tree`'s own comment)."""
    gc.collect()
    tracemalloc.start()
    try:
        with ScanIndex(db_path) as index:
            scan_tree(root, index, incremental=False)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_scan_tree_peak_memory_does_not_scale_with_file_count(tmp_path: Path) -> None:
    """The core proof for Wave 1 finding #1: two fixture trees at a 4x file-count ratio (same
    top-level directory/worker count, same per-directory fan-out — see module comment for why
    those are held fixed) must NOT show anywhere near a 4x peak-memory ratio. A generous but
    still meaningfully tight ceiling absorbs real measurement noise while failing hard if the
    old whole-walk-accumulation bug came back, which would show close to the full 4x ratio."""
    small_root = tmp_path / "small"
    small_count = _build_nested_tree(small_root, leaf_dirs_per_top=_SMALL_LEAF_DIRS_PER_TOP)
    small_peak = _measure_peak_traced_memory_during_scan(small_root, tmp_path / "small.sqlite3")

    large_root = tmp_path / "large"
    large_count = _build_nested_tree(large_root, leaf_dirs_per_top=_LARGE_LEAF_DIRS_PER_TOP)
    large_peak = _measure_peak_traced_memory_during_scan(large_root, tmp_path / "large.sqlite3")

    file_count_ratio = large_count / small_count
    peak_memory_ratio = large_peak / small_peak

    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[scanner memory] small={small_count} files, peak={small_peak / 1024 / 1024:.1f}MB | "
        f"large={large_count} files, peak={large_peak / 1024 / 1024:.1f}MB | "
        f"file_count_ratio={file_count_ratio:.1f}x peak_memory_ratio={peak_memory_ratio:.2f}x"
    )

    # ~2x, not ~1x, is the CORRECT expected ratio here (measured: 1.96x) — not slack for noise.
    # The small tier's 2,500 records/worker never reaches `_WRITE_BATCH_SIZE` (5000) at all, so
    # each worker flushes exactly once, holding its full 2,500-record buffer the whole walk; the
    # large tier's 10,000 records/worker crosses the cap twice, so its PEAK per-worker buffer is
    # bounded at 5000 — a real, one-time, EXPECTED step from "under the cap" to "at the cap" as
    # per-worker totals cross _WRITE_BATCH_SIZE, not unbounded growth. A third, even larger tier
    # would show peak staying flat from here, not continuing to climb — that's the actual "does
    # not scale with file count" property; this 2-tier comparison already proves it isn't the
    # OLD unbounded behavior, which would show close to the full 4x file_count_ratio here, not
    # comfortably under half of it.
    assert peak_memory_ratio < 2.5, (
        f"peak traced memory scaled {peak_memory_ratio:.2f}x when file count scaled "
        f"{file_count_ratio:.1f}x with directory/worker count and per-directory fan-out HELD "
        "CONSTANT — expected roughly a one-time step to the _WRITE_BATCH_SIZE cap (~2x), not "
        "unbounded linear growth. This is the exact regression the 2026-07-30 real-disk "
        "diagnosis found (5,085MB peak RSS for 2.67M files, extrapolating to 15-25GB+ on a true "
        "full-drive scan) — see PLAN.md's Wave 1 checkpoint."
    )
    assert small_peak < _ABSOLUTE_PEAK_CEILING_BYTES, (
        f"peak traced memory for a {small_count}-file scan was "
        f"{small_peak / 1024 / 1024:.1f}MB — expected well under the batch-bounded ceiling "
        f"({_ABSOLUTE_PEAK_CEILING_BYTES / 1024 / 1024:.0f}MB)"
    )
