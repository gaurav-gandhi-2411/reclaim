from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tests_run_as_a_normal_unelevated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions' Windows runners execute as `runneradmin` — a genuinely ELEVATED
    Administrator process — so `reclaim.elevation.assert_not_elevated` correctly refused every
    mutating CLI command and failed every `tests/test_cli.py` case that exercises one (first
    observed the day the repo went public: these workflows had never run on a real runner
    before). The guard is doing exactly its job; it's the test environment that violates the
    "ordinary developer terminal" assumption the suite was written under.

    This autouse fixture normalizes that assumption for the unit suite: the raw Win32 check is
    forced to report non-elevated, everywhere, deterministically. Tests that specifically
    exercise the elevated-refusal behavior (tests/test_elevation.py, test_cli.py's
    `*_refuses_to_run_elevated` cases) monkeypatch `is_elevated`/`_raw_is_admin` themselves
    inside the test body — a later patch on the same attribute simply wins, so those tests are
    unaffected. The guard's real-world behavior in a packaged, genuinely-elevated process is
    covered by those explicit simulations, not by depending on what CI's process token happens
    to be.
    """
    monkeypatch.setattr("reclaim.elevation._raw_is_admin", lambda: False)
