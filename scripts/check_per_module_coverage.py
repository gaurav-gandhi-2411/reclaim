from __future__ import annotations

# check_per_module_coverage.py -- CI gate: per-module coverage floor for the five safety-critical
# modules (executor.py, safety.py, mode.py, config.py, purge.py).
#
# Why this exists: this repo's aggregate coverage gate (`[tool.coverage.report] fail_under = 80`
# in pyproject.toml) can pass at a healthy 90%+ overall while ONE of these five files -- the
# modules that actually gate SAFE->POWER mode transitions, permanent deletion, and the safe-mode
# boundary -- silently regresses to a much lower number, because the other ~40 modules' coverage
# dilutes it into statistical insignificance. This is the exact same *shape* of failure as
# ADR-0018's "never aggregate precision/recall across distinct tiers" incident (a pooled number
# that looks fine while hiding a real, localized problem) applied to test coverage instead of
# eval metrics: a per-module floor is the metrics-integrity discipline that ADR already
# established, extended to these five files so a gap on any ONE of them can never again hide
# behind a healthy aggregate.
#
# Floors below were set from this file's own post-hardening coverage numbers (see
# tests/test_mode.py, and the safe-mode/OSError-isolation/`re:`-pattern/restore-isolation tests
# added to tests/test_executor.py, tests/test_purge.py, tests/test_safety.py), rounded down a few
# points to leave headroom for minor future changes without being a rubber stamp -- not chosen to
# exactly match today's number, which would make this gate fire on any trivial refactor.
#
# Usage: uv run python scripts/check_per_module_coverage.py
# Must run AFTER a `pytest --cov` invocation has written a `.coverage` data file in the cwd.
import io
import sys

from coverage import Coverage
from coverage.exceptions import CoverageException

# (repo-relative path, floor percent). Order matters only for readability of the printed report.
MODULE_FLOORS: list[tuple[str, float]] = [
    ("src/reclaim/mode.py", 90.0),
    ("src/reclaim/purge.py", 85.0),
    ("src/reclaim/safety.py", 92.0),
    ("src/reclaim/executor.py", 90.0),
    ("src/reclaim/config.py", 90.0),
]


def _module_coverage_percent(cov: Coverage, module_path: str) -> float:
    """Percent covered for exactly this one file -- `morfs=[module_path]` restricts the whole
    report to that single module, so the returned total IS that module's own percentage, never
    diluted by any other file in the aggregate."""
    return cov.report(morfs=[module_path], show_missing=False, file=io.StringIO())


def main(argv: list[str]) -> int:
    if argv:
        print("usage: check_per_module_coverage.py (no arguments)", file=sys.stderr)
        return 2

    cov = Coverage()
    cov.load()

    failures: list[str] = []
    for module_path, floor in MODULE_FLOORS:
        try:
            percent = _module_coverage_percent(cov, module_path)
        except CoverageException as exc:
            failures.append(f"{module_path}: could not measure coverage -- {exc}")
            print(f"FAIL {module_path}: could not measure coverage -- {exc}", file=sys.stderr)
            continue

        if percent < floor:
            failures.append(f"{module_path}: {percent:.2f}% < required {floor:.1f}%")
            print(
                f"FAIL {module_path}: {percent:.2f}% covered, required >= {floor:.1f}%",
                file=sys.stderr,
            )
        else:
            print(f"OK   {module_path}: {percent:.2f}% covered (floor {floor:.1f}%)")

    if failures:
        print(
            f"\nFAIL: {len(failures)} safety-critical module(s) dropped below their individual "
            "coverage floor -- see per-module lines above. An aggregate coverage number, however "
            "healthy, must never hide a gap on ONE of these five files (ADR-0018 precedent).",
            file=sys.stderr,
        )
        return 1

    print(f"\nOK: all {len(MODULE_FLOORS)} safety-critical modules meet their coverage floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
