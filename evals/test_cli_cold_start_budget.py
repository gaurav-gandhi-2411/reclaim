from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
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
#
# D4 (2026-10-24): the budget is now on CPU time, not wall time. Wall time measured the machine:
# under another session's load the median read 6,217.6 ms against this 2,000 ms budget on
# unchanged code. Reproduced on purpose: 16 busy-loop processes moved the wall median of the
# same import graph 801 -> 5,361 ms (6.7x) while the whole process tree's CPU time moved
# 633 -> 766 ms (1.21x). The CPU time comes from a Job Object (`_job_cpu.py`), because the
# console-script launcher runs the real interpreter as a child. The number is unchanged: what a
# cold start costs Reclaim itself stays under 2 s of CPU. Wall time is still printed.
_COLD_START_MEDIAN_BUDGET_MS = 2000.0
# CPU time cannot see a start-up that blocks (a network call, a lock wait). This wall-clock
# tripwire catches that and nothing finer: the worst load-inflated wall time measured was a
# 6,599 ms single run (median 5,023 ms) under 16 busy-loop processes, so 15 s leaves room for a
# loaded runner while a blocking call with any usual timeout still trips it.
_WALL_HANG_TRIPWIRE_MS = 15_000.0
_JOB_CPU = Path(__file__).with_name("_job_cpu.py")


def _reclaim_exe() -> Path:
    """The dev venv's own `reclaim.exe` entry point, next to whatever interpreter `uv run pytest`
    is currently executing under -- avoids hardcoding `.venv` (a worktree/CI checkout's venv
    directory name/location isn't guaranteed to be `.venv` relative to CWD in every invocation
    style this test might run under, but `sys.executable`'s own directory always holds the
    matching `reclaim.exe` for whatever venv is actually active)."""
    return Path(sys.executable).parent / "reclaim.exe"


def test_cli_version_cold_start_stays_under_budget_dev_venv_proxy(tmp_path: Path) -> None:
    """Measures the dev-venv `reclaim --version` invocation as a proxy for the real, un-buildable-
    per-PR Nuitka packaged binary's cold start -- see module comment for why this is a legitimate
    proxy and packaging/RELEASE_RUNBOOK.md for the directly-measured packaged-binary number this
    substitutes for."""
    exe = _reclaim_exe()
    assert exe.exists(), (
        f"{exe} missing -- expected a `uv sync`'d dev venv with the reclaim entry point installed"
    )

    result = subprocess.run(  # noqa: S603 -- fixed argv, local venv executables, no shell
        [sys.executable, str(_JOB_CPU), str(_TRIALS), str(exe), "--version"],
        capture_output=True,
        text=True,
        timeout=30 * _TRIALS,
        check=False,
        # cwd-relative `data/logs/` must land in tmp, never in the real checkout
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"reclaim --version (or the job helper) failed: {result.stderr}"
    runs = json.loads(result.stdout)["runs"]
    cpu_ms = sorted(r["cpu_ms"] for r in runs)
    wall_ms = sorted(r["wall_ms"] for r in runs)
    median_cpu_ms = statistics.median(cpu_ms)

    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[cli cold start, dev venv proxy] n={_TRIALS} "
        f"cpu min={cpu_ms[0]:.1f}ms median={median_cpu_ms:.1f}ms max={cpu_ms[-1]:.1f}ms; "
        f"wall min={wall_ms[0]:.1f}ms median={statistics.median(wall_ms):.1f}ms "
        f"max={wall_ms[-1]:.1f}ms (wall reported, not budgeted -- see the module comment)"
    )

    assert median_cpu_ms < _COLD_START_MEDIAN_BUDGET_MS, (
        f"median cold-start CPU time ({median_cpu_ms:.1f}ms across {_TRIALS} trials) exceeded "
        f"the {_COLD_START_MEDIAN_BUDGET_MS:.0f}ms budget -- see module comment for the measured "
        "dev-machine and packaged-binary baselines this proxies for"
    )
    assert statistics.median(wall_ms) < _WALL_HANG_TRIPWIRE_MS, (
        f"median cold-start wall time ({statistics.median(wall_ms):.1f}ms) exceeded the "
        f"{_WALL_HANG_TRIPWIRE_MS:.0f}ms hang tripwire: start-up is blocking on something"
    )


# Third-party packages `reclaim --version` must never import: each is what a heavy subcommand
# (dashboard, AI, MCP, dedup embeddings) needs, and a top-level import of one in the CLI module
# graph is the regression this budget exists to catch. Deterministic, so it holds on any machine
# load; the CPU budget above bounds what this list does not name.
_HEAVY_MODULES = (
    "anthropic",
    "fastapi",
    "httpx",
    "mcp",
    "numpy",
    "onnxruntime",
    "PIL",
    "starlette",
    "tokenizers",
    "torch",
    "uvicorn",
)


def test_cli_version_imports_no_heavy_module(tmp_path: Path) -> None:
    code = (
        "import json, sys\n"
        "from reclaim.cli import main\n"
        "try:\n"
        "    main(['--version'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    imported = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert "reclaim" in imported  # the probe really ran the CLI
    assert not imported & set(_HEAVY_MODULES), sorted(imported & set(_HEAVY_MODULES))
