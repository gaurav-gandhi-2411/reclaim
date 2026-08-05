from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# --- CI regression budgets for scan_tree()'s peak RSS -------------------------------------------
#
# Premise check (rule 99 -- verified against the actual code, not assumed): PLAN.md's 2026-07-30
# checkpoint documents `_BatchIndexWriter` (scanner.py) as ALREADY replacing the old whole-walk
# `all_records` list -- every worker now flushes in `_WRITE_BATCH_SIZE=5000`-record batches, so
# peak memory is bounded by `worker_count * _WRITE_BATCH_SIZE`, a fixed ceiling, NOT by total file
# count. This is a real, already-shipped fix, not an open gap -- confirmed by reading
# `scanner.py::_BatchIndexWriter`'s own docstring and the surrounding `_walk_subtree`/`scan_tree`
# code directly. So this module's job is NOT to newly discover "flat vs. linear" from scratch --
# it's to put a CI-enforced number on the bounded-but-not-instantaneously-flat behavior that
# design actually produces, measured cleanly (isolated from fixture-generation memory, in a fresh
# subprocess per scale -- see `_scan_peak_rss_harness.py`'s own module docstring for why).
#
# MEASURED (this session, worktree HEAD at the time of writing, this machine) via
# `_scan_peak_rss_harness.py` run twice, once per scale, each in its own fresh subprocess:
#
#   | tier              | file_count | baseline      | peak_after    | delta         | bytes/entry |
#   |-------------------|-----------:|--------------:|--------------:|--------------:|------------:|
#   | fast (3,000)      |      3,034 |  28,524,544 B |  36,179,968 B |   7,655,424 B |      2,524  |
#   | scale (100,000)   |    101,004 |  28,876,800 B |  61,931,520 B |  33,054,720 B |        327  |
#
# file_count_ratio = 101,004 / 3,034 = 33.3x; peak_delta_ratio = 33,054,720 / 7,655,424 = 4.32x.
#
# This is the HONEST finding, not fabricated in either direction: peak delta does NOT stay flat
# between these two scales (4.32x growth is real, not noise) -- but it is dramatically
# SUB-LINEAR versus the 33.3x file-count growth, consistent with the architecturally-expected
# "unsaturated per-worker buffer growing toward, then plateauing at, the
# `worker_count * _WRITE_BATCH_SIZE` ceiling" shape (this fixture's fixed `_TOP_LEVEL_DIRS=4`
# means each worker holds ~750 records/worker at the fast tier -- under the 5,000-record batch
# cap, so it never flushes mid-walk -- versus ~25,000 records/worker at the scale tier, which
# crosses that cap 5x, so its PEAK per-worker buffer is capped at 5,000 either way). A real
# reversion to the OLD whole-walk-list bug would show peak_delta_ratio close to the FULL 33.3x
# file_count_ratio, not a number under 5x -- see `test_scan_tree_peak_rss_growth_characterization_
# scale_tier` below for the actual regression gate on that specific historical bug.
_FAST_TIER_FILE_COUNT = 3_000
_SCALE_TIER_FILE_COUNT = 100_000

_HARNESS_PATH = Path(__file__).parent / "_scan_peak_rss_harness.py"


def _run_harness(root: Path, file_count: int, *, timeout: float) -> dict[str, int]:
    result = subprocess.run(  # noqa: S603 -- fixed argv, local harness script, no shell
        [sys.executable, str(_HARNESS_PATH), str(root), str(file_count)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return dict(json.loads(result.stdout.strip().splitlines()[-1]))


# Budget for the FAST tier alone (runs on every PR -- the 100k+ tier is too slow to build+scan
# per-PR, see .github/workflows/ci.yml's split). Measured delta: 7,655,424 bytes (~7.30 MiB).
# Budget set at 40 MiB (~5.5x the measured value) -- generous headroom for OS-level RSS's known
# extra noise sources versus a Python-level tracemalloc measurement (page-granularity rounding,
# allocator arena behavior, CI-runner-specific working-set quirks) while still catching a real,
# order-of-magnitude blowup.
#
# IMPORTANT, disclosed honestly (rule 85a -- name a control's actual reach): at 3,000 files, this
# fixture's 4 top-level workers each hold well under the 5,000-record `_WRITE_BATCH_SIZE` cap, so
# the CURRENT bounded-batch design and the OLD whole-walk-list bug behave almost identically at
# THIS scale (the old bug would cost roughly the same ~7-8 MiB here too) -- a regression back to
# the old bug would NOT reliably blow this budget. This test still legitimately catches a
# DIFFERENT kind of regression (e.g. an unrelated unbounded cache introduced elsewhere in the
# walk) at everyday PR scale; the specific historical whole-walk-accumulation bug's real
# regression gate is `test_scan_tree_peak_rss_growth_characterization_scale_tier` below, which
# only becomes reliably discriminating once per-worker batches actually saturate their cap --
# hence why that one needs the (scale-gated, not per-PR) 100k tier.
_FAST_TIER_PEAK_DELTA_BUDGET_BYTES = 40 * 1024 * 1024


def test_scan_tree_peak_rss_delta_stays_bounded_fast_tier(tmp_path: Path) -> None:
    """Per-PR regression budget: a clean, scan-isolated peak-RSS delta for a 3,000-file scan must
    stay under `_FAST_TIER_PEAK_DELTA_BUDGET_BYTES`. See module comment for the measured baseline,
    the chosen tolerance, and this specific test's honestly-disclosed limits."""
    result = _run_harness(tmp_path / "rss_fast", _FAST_TIER_FILE_COUNT, timeout=120)
    delta_bytes = result["peak_after_bytes"] - result["baseline_bytes"]

    baseline_mb = result["baseline_bytes"] / 1024 / 1024
    peak_after_mb = result["peak_after_bytes"] / 1024 / 1024
    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[peak rss budget, fast tier] file_count={result['file_count']} "
        f"entries_total={result['entries_total']} baseline={baseline_mb:.2f}MB "
        f"peak_after={peak_after_mb:.2f}MB delta={delta_bytes / 1024 / 1024:.2f}MB "
        f"({delta_bytes / result['entries_total']:.0f} bytes/entry)"
    )

    assert delta_bytes < _FAST_TIER_PEAK_DELTA_BUDGET_BYTES, (
        f"scan-isolated peak RSS delta ({delta_bytes / 1024 / 1024:.2f}MB) for a "
        f"{_FAST_TIER_FILE_COUNT}-file scan exceeded the "
        f"{_FAST_TIER_PEAK_DELTA_BUDGET_BYTES / 1024 / 1024:.0f}MB budget"
    )


# Budget for the growth-RATIO check between the two tiers (scale-gated -- see
# .github/workflows/ci.yml's scale-nightly job, never per-PR). Measured ratio: 4.32x delta growth
# for a 33.3x file-count growth. A real reversion to the OLD whole-walk-list bug would push this
# close to the full 33.3x file_count_ratio (every record held in memory means delta scales
# ~linearly with file count), not a number comfortably under half of it. Budget set at 10x --
# meaningfully below the 33.3x "old bug" fingerprint, with ~2.3x real headroom over the 4.32x
# measured value for run-to-run noise.
_SCALE_TIER_DELTA_RATIO_BUDGET = 10.0
# Absolute ceiling on the scale tier's own delta, independent of the ratio check -- measured
# 33,054,720 bytes (~31.53 MiB); budget at 100 MiB (~3.2x measured) for the same OS-RSS-noise
# reasoning as the fast-tier budget above.
_SCALE_TIER_PEAK_DELTA_BUDGET_BYTES = 100 * 1024 * 1024


@pytest.mark.scale
def test_scan_tree_peak_rss_growth_characterization_scale_tier(tmp_path: Path) -> None:
    """The real regression gate for the specific historical whole-walk-accumulation bug (see
    module comment) -- only discriminating at a scale where per-worker batches actually saturate
    the `_WRITE_BATCH_SIZE` cap, hence gated to the nightly/main-push scale job, not every PR."""
    fast = _run_harness(tmp_path / "rss_fast", _FAST_TIER_FILE_COUNT, timeout=120)
    scale = _run_harness(tmp_path / "rss_scale", _SCALE_TIER_FILE_COUNT, timeout=900)

    fast_delta = fast["peak_after_bytes"] - fast["baseline_bytes"]
    scale_delta = scale["peak_after_bytes"] - scale["baseline_bytes"]
    file_count_ratio = scale["entries_total"] / fast["entries_total"]
    delta_ratio = scale_delta / fast_delta

    print(  # noqa: T201
        f"\n[peak rss budget, growth characterization] fast_entries={fast['entries_total']} "
        f"fast_delta={fast_delta / 1024 / 1024:.2f}MB scale_entries={scale['entries_total']} "
        f"scale_delta={scale_delta / 1024 / 1024:.2f}MB file_count_ratio={file_count_ratio:.1f}x "
        f"peak_delta_ratio={delta_ratio:.2f}x"
    )

    assert delta_ratio < _SCALE_TIER_DELTA_RATIO_BUDGET, (
        f"peak RSS delta scaled {delta_ratio:.2f}x when file count scaled "
        f"{file_count_ratio:.1f}x -- expected well under the old whole-walk-list bug's ~linear "
        f"fingerprint (close to {file_count_ratio:.1f}x), matching the bounded "
        "worker_count * _WRITE_BATCH_SIZE ceiling this architecture is supposed to guarantee"
    )
    assert scale_delta < _SCALE_TIER_PEAK_DELTA_BUDGET_BYTES, (
        f"scan-isolated peak RSS delta ({scale_delta / 1024 / 1024:.2f}MB) for a "
        f"{_SCALE_TIER_FILE_COUNT}-file scan exceeded the "
        f"{_SCALE_TIER_PEAK_DELTA_BUDGET_BYTES / 1024 / 1024:.0f}MB absolute budget"
    )
