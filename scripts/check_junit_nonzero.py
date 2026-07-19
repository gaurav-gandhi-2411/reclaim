from __future__ import annotations

# check_junit_nonzero.py — CI meta-check: fail loudly if a pytest run's own JUnit report says
# it collected/ran zero tests.
#
# Why this exists: a pytest invocation that hits a collection error part-way through a
# directory sweep aborts the WHOLE session ("Interrupted: N errors during collection") with a
# nonzero exit code today, but that's incidental to pytest's own default behavior, not a
# guarantee this repo enforces. A future flag change (e.g. --continue-on-collection-errors), a
# typo'd path that matches no files, or a marker/deselect expression that filters out
# everything could all leave a CI step exiting 0 while having proven nothing — a job silently
# green because it ran nothing, not because it passed. This script is the explicit backstop:
# every CI pytest step that matters writes --junitxml=<path>, then this script asserts the
# report's own <testsuite tests="N"> is nonzero before the job is allowed to succeed.
#
# Usage: uv run python scripts/check_junit_nonzero.py <path-to-junit-xml>
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_junit_nonzero.py <path-to-junit-xml>", file=sys.stderr)
        return 2

    xml_path = Path(argv[0])
    if not xml_path.exists():
        print(f"FAIL: {xml_path} does not exist -- pytest never wrote a report", file=sys.stderr)
        return 1

    # S314: the report parsed here is generated moments earlier by pytest in this same CI job,
    # never external/untrusted input -- defusedxml would be the right call for a report coming
    # from anywhere else.
    root = ET.parse(xml_path).getroot()  # noqa: S314

    # Sum every <testsuite> child rather than reading just the first (or trusting a "tests"
    # attribute on the <testsuites> root, which pytest's own writer does not emit -- verified
    # directly against real output) -- pytest-xdist parallelism (-n) is a real dependency of
    # this project and the JUnit XML schema permits multiple <testsuite> siblings under one
    # <testsuites> root; reading only the first would silently under-count in that shape.
    testsuites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    if not testsuites:
        print(f"FAIL: no <testsuite> element found in {xml_path}", file=sys.stderr)
        return 1

    tests = sum(int(suite.get("tests", "0")) for suite in testsuites)
    if tests == 0:
        print(
            f"FAIL: {xml_path} reports 0 tests collected/run. A CI job that runs zero tests "
            "must never pass silently -- this is exactly the collection-abort-as-green failure "
            "mode this check exists to catch.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {tests} tests ran ({xml_path}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
