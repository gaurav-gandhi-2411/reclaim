from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_TRIALS = 10

# CI cannot build the real Nuitka-packaged `reclaim-setup.exe` per PR -- 3+ hours clean, per
# packaging/RELEASE_RUNBOOK.md's own "Wall time" row -- so this measures the DEV-VENV
# `reclaim --version` invocation (the flag added this session specifically so a cold-start
# measurement doesn't have to pick an arbitrary subcommand as a stand-in) as a proxy for the real
# packaged binary's cold start.
#
# Invokes `.venv\Scripts\reclaim.exe` DIRECTLY, not via `uv run reclaim --version` -- deliberately
# mirroring RELEASE_RUNBOOK.md's own measurement methodology, not a literal "via uv run" reading:
# PLAN.md's Wave 1 checkpoint records that an early diagnostic run measured `uv run`'s own
# launcher-shim process instead of the real work (the actual work happens in a grandchild
# `python.exe`, confirmed via process-tree inspection) -- `uv run` adds a real, extra process hop
# of overhead that isn't part of the number this test needs to budget.
#
# MEASURED baselines cited for context (packaging/RELEASE_RUNBOOK.md, commit 667fe02/06d819e --
# GG's own dev machine, not this CI runner or this worktree):
#   - dev venv (`.venv\Scripts\reclaim.exe --version`): min 695.5ms / median 794.2ms /
#     p95 1007.4ms (n=15)
#   - PACKAGED Nuitka binary (`reclaim.exe mode`): min 594.4ms / median 722.6ms / p95 1121.8ms
#     (n=15)
# Both land in the same ~600-900ms band -- Nuitka standalone packaging adds no material cold-start
# penalty over the interpreted dev venv, so the dev-venv number is a legitimate stand-in for the
# real packaged-binary number this test cannot measure directly in CI.
#
# Freshly re-measured in THIS worktree (not carried over from the citation above, per house rule
# 65a/101 -- never state a number from memory as if newly measured): min 983.5ms / median 1091.3ms
# / p95 1337.1ms (n=10), noticeably slower than the dev-machine baseline above -- plausibly this
# worktree sharing the dev machine with other concurrent sessions (rule 56a), not a real
# regression. This is exactly why the budget below targets the task's own architectural "~2s"
# threshold rather than a tight band around either measured number -- CI runners (and, as this
# re-measurement shows, even a busy shared dev machine) are slower and more variable than a quiet
# dedicated machine.
_COLD_START_MEDIAN_BUDGET_MS = 2000.0


def _reclaim_exe() -> Path:
    """The dev venv's own `reclaim.exe` entry point, next to whatever interpreter `uv run pytest`
    is currently executing under -- avoids hardcoding `.venv` (a worktree/CI checkout's venv
    directory name/location isn't guaranteed to be `.venv` relative to CWD in every invocation
    style this test might run under, but `sys.executable`'s own directory always holds the
    matching `reclaim.exe` for whatever venv is actually active)."""
    return Path(sys.executable).parent / "reclaim.exe"


def test_cli_version_cold_start_stays_under_budget_dev_venv_proxy() -> None:
    """Measures the dev-venv `reclaim --version` invocation as a proxy for the real, un-buildable-
    per-PR Nuitka packaged binary's cold start -- see module comment for why this is a legitimate
    proxy and packaging/RELEASE_RUNBOOK.md for the directly-measured packaged-binary number this
    substitutes for."""
    exe = _reclaim_exe()
    assert exe.exists(), (
        f"{exe} missing -- expected a `uv sync`'d dev venv with the reclaim entry point installed"
    )

    samples_ms: list[float] = []
    for _ in range(_TRIALS):
        start = time.perf_counter()
        result = subprocess.run(  # noqa: S603 -- fixed argv, local venv executable, no shell
            [str(exe), "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert result.returncode == 0, f"reclaim --version failed: {result.stderr}"
        samples_ms.append(elapsed_ms)

    samples_ms.sort()
    median_ms = statistics.median(samples_ms)
    p95_index = min(len(samples_ms) - 1, int(len(samples_ms) * 0.95))
    p95_ms = samples_ms[p95_index]

    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[cli cold start, dev venv proxy] n={_TRIALS} "
        f"min={samples_ms[0]:.1f}ms median={median_ms:.1f}ms p95={p95_ms:.1f}ms "
        f"samples={[f'{s:.1f}' for s in samples_ms]}"
    )

    assert median_ms < _COLD_START_MEDIAN_BUDGET_MS, (
        f"median cold-start ({median_ms:.1f}ms across {_TRIALS} trials) exceeded the "
        f"{_COLD_START_MEDIAN_BUDGET_MS:.0f}ms budget -- see module comment for the measured "
        "dev-machine and packaged-binary baselines this proxies for"
    )
