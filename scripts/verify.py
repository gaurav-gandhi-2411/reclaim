from __future__ import annotations

# verify.py -- the ONE canonical pre-push/pre-PR verification command for this repo.
#
# Why this exists: a real security regression (see docs/architecture/adr/0027-schema-versioning-
# for-durable-state.md's "A real regression this ADR caused" section) reached PR review
# undetected because every branch was verified with `uv run pytest tests/ -q` before pushing --
# and `pyproject.toml`'s `testpaths = ["tests"]` means that command NEVER discovers anything
# under `evals/`, including the AI recommend-only safety gate (`evals/test_ai_safety_gate.py`)
# and the SafetyValidator hard gate (`evals/test_safety_gate.py`). A second gap found in the same
# session: `evals/test_safe_mode_gate.py` (the Stage 2 safe-mode structural boundary, 18 tests)
# wasn't referenced by EITHER CI workflow at all -- dead weight, never exercised anywhere.
#
# This script is the single source of truth for "did I actually check everything that matters"
# -- run it before every push, not a hand-picked subset of it. `.github/workflows/ci.yml` and
# `eval.yml` mirror these same checks in CI as the real enforcement backstop (a local run can be
# skipped by mistake; CI cannot) -- this script exists so the FIRST time a regression is caught
# is locally, in seconds, not minutes later in a CI run against a pushed branch.
#
# Usage: uv run python scripts/verify.py
import subprocess
from collections.abc import Sequence

_JUNIT_PATH = "pytest-results.xml"

# Every one of these four eval files is fast and deterministic (no [ai] extra, no network, no
# real model download -- verified: all four collect and pass with zero AI dependencies
# installed) specifically so there is no excuse to skip them in a quick local pre-push check.
# The rest of evals/ (gold-set/operating-point measurements) is deliberately NOT here -- those
# need the [ai] extra and real fixture datasets, and run in CI's ai-layer-with-extras job
# instead; bundling them into every pre-push run would make this script slow enough that people
# stop running it, which defeats the point.
#
# test_safety_adversarial.py added 2026-08-05: proves the SafetyValidator hard gate resists
# path-obfuscation bypass attempts (8.3 names, ..-traversal, subst, junctions, case) -- a
# distinct property from test_safety_gate.py's golden-tree-fixture-match check, and exactly the
# kind of file this script's own docstring warns gets silently skipped when it's left out.
#
# test_scanner_peak_rss_budget.py / test_cli_cold_start_budget.py added 2026-08-05: CI-enforced
# perf regression budgets (scan peak-RSS growth ratio, CLI cold start). Neither needs the [ai]
# extra (confirmed: no ai-layer imports, collect and pass in a bare `uv sync`) -- same reasoning
# as the three files above, no excuse to leave them for CI-only discovery. IMPORTANT:
# test_scanner_peak_rss_budget.py also has a `scale`-marked 100k-file test -- pyproject.toml
# registers that marker but does NOT set addopts to deselect it by default (verified: no
# `-m "not scale"` anywhere in [tool.pytest.ini_options]), so the `-m "not scale"` filter below
# on the pytest invocation itself is load-bearing, not decorative -- remove it and this "fast"
# pre-push gate silently starts generating a 100k-file tree on every run.
_SAFETY_GATE_FILES: tuple[str, ...] = (
    "evals/test_safety_gate.py",
    "evals/test_safety_adversarial.py",
    "evals/test_ai_safety_gate.py",
    "evals/test_safe_mode_gate.py",
    "evals/test_scanner_peak_rss_budget.py",
    "evals/test_cli_cold_start_budget.py",
)

_STEPS: tuple[tuple[str, Sequence[str]], ...] = (
    ("ruff check", ["uv", "run", "ruff", "check", "."]),
    ("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."]),
    ("mypy", ["uv", "run", "mypy"]),
    (
        "pytest (tests/ + safety-gate evals -- NEVER report this project's tests as fully "
        "verified without this exact file list; testpaths=['tests'] alone silently skips all "
        "of evals/)",
        [
            "uv",
            "run",
            "pytest",
            "tests/",
            *_SAFETY_GATE_FILES,
            "-m",
            "not scale",
            "--cov",
            "--cov-report=term-missing",
            f"--junitxml={_JUNIT_PATH}",
        ],
    ),
    (
        "fail if zero tests ran",
        ["uv", "run", "python", "scripts/check_junit_nonzero.py", _JUNIT_PATH],
    ),
    (
        "per-module coverage floor (safety-critical modules)",
        ["uv", "run", "python", "scripts/check_per_module_coverage.py"],
    ),
)


def main() -> int:
    for name, cmd in _STEPS:
        print(f"\n=== {name} ===", flush=True)  # flush: interleaves with the child's own stdout
        result = subprocess.run(cmd, check=False)  # noqa: S603 -- fixed argv, no shell, no user input
        if result.returncode != 0:
            print(
                f"\nFAIL: '{name}' exited {result.returncode} -- stopping here, later steps not "
                "run. Fix this, then re-run scripts/verify.py from the top."
            )
            return result.returncode
    print(
        "\nOK: all checks passed -- ruff, mypy, tests/ + safety-gate evals, and the "
        "per-module coverage floor."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
