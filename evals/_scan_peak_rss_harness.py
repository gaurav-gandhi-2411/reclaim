from __future__ import annotations

# Standalone subprocess harness for `evals/test_scanner_peak_rss_budget.py` -- run as
# `python evals/_scan_peak_rss_harness.py <root> <file_count>`, never imported. Mirrors
# `tests/_recovery_crash_harness.py`'s own subprocess-harness convention (see
# `evals/test_e2e_scale.py::_run_crash_harness`'s docstring), but for a different reason here:
# Windows' `GetProcessMemoryInfo` reports `PeakWorkingSetSize` as a monotonically-non-decreasing
# high-water mark for the CALLING process that never resets on its own -- measuring two fixture
# scales inside one long-lived pytest process would let the larger tier's peak absorb (or be
# contaminated by) whatever the smaller tier already pushed the peak to. One fresh subprocess per
# scale keeps every measurement isolated.
#
# Builds a plain, nested tree of trivial small files (mirrors `evals/test_scanner_memory.py`'s own
# `_build_nested_tree` -- deliberately NOT the full `evals/fixtures/build_scale_tree.py` fixture,
# whose near-dup image/document generation would itself dominate the "before scan_tree" baseline
# and defeat the isolation this harness exists for), captures the OS-level peak working set
# immediately before calling `scan_tree()` (a stable baseline already past every fixture-build
# allocation, after an explicit `gc.collect()`) and again immediately after, and prints exactly one
# JSON line to stdout: `{"file_count": int, "entries_total": int, "baseline_bytes": int,
# "peak_after_bytes": int}`. The CALLER (the pytest test) computes `peak_after_bytes -
# baseline_bytes` as the scan-isolated memory delta -- this harness reports raw numbers only, no
# budget/threshold logic, so the actual regression assertion always lives visibly in the test file,
# not buried in a subprocess.
import ctypes
import gc
import json
import sys
from ctypes import wintypes
from pathlib import Path

from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

# Fixed top-level directory count / per-leaf-directory fan-out, exactly mirroring
# `evals/test_scanner_memory.py`'s own `_build_nested_tree` -- see that module's comment for why
# both are held constant regardless of total file count (isolates the batched-write cross-flush
# behavior this harness measures from unrelated per-directory/worker-count memory effects).
_TOP_LEVEL_DIRS = 4
_FILES_PER_LEAF_DIR = 100


def _build_nested_tree(root: Path, *, file_count: int) -> int:
    """Same fixed-fan-out construction as `evals/test_scanner_memory.py`'s `_build_nested_tree`
    -- duplicated rather than imported, since this file runs as a standalone subprocess entry
    point, never as part of the pytest collection tree that module belongs to."""
    leaf_dirs_per_top = -(-file_count // (_TOP_LEVEL_DIRS * _FILES_PER_LEAF_DIR))  # ceil div
    count = 0
    for t in range(_TOP_LEVEL_DIRS):
        top = root / f"top_{t:02d}"
        for leaf_index in range(leaf_dirs_per_top):
            if count >= file_count:
                break
            leaf = top / f"leaf_{leaf_index:04d}"
            leaf.mkdir(parents=True)
            for j in range(_FILES_PER_LEAF_DIR):
                if count >= file_count:
                    break
                (leaf / f"file_{j:03d}.txt").write_bytes(b"x" * 64)
                count += 1
        if count >= file_count:
            break
    return count


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_working_set_bytes() -> int:
    """Real OS-level peak RSS for THIS process via `psapi.GetProcessMemoryInfo` -- identical
    implementation to `evals/test_e2e_scale.py`'s own `_peak_working_set_bytes` (duplicated, not
    imported, since this module is a standalone subprocess entry point). Requires explicit
    `argtypes`/`restype` on both Win32 calls -- confirmed empirically while writing that original
    helper that ctypes' default `c_int` treatment of every argument silently breaks the call
    (returns success but a bogus zero value) without `HANDLE`/`DWORD`-typed `argtypes`."""
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    ok = get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb)
    if not ok:
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def main() -> int:
    root = Path(sys.argv[1])
    file_count = int(sys.argv[2])
    root.mkdir(parents=True, exist_ok=True)
    actual_count = _build_nested_tree(root, file_count=file_count)

    # gc.collect() before the baseline sample: peak working set is a high-water mark, so this
    # doesn't shrink it, but it does ensure fixture-build garbage isn't still live (and thus
    # doesn't inflate scan_tree's own delta) at the moment scan_tree starts.
    gc.collect()
    baseline_bytes = _peak_working_set_bytes()

    db_path = root.parent / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        stats = scan_tree(root, index, incremental=False)
    peak_after_bytes = _peak_working_set_bytes()

    print(  # noqa: T201 -- this IS the harness's output contract, not a debug print
        json.dumps(
            {
                "file_count": actual_count,
                "entries_total": stats.entries_total,
                "baseline_bytes": baseline_bytes,
                "peak_after_bytes": peak_after_bytes,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
