from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from reclaim.api import service
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
from reclaim.detectors import generate_candidates
from reclaim.index import ScanIndex
from reclaim.safety import SafetyValidator

# P0 finding (2026-08-22 real-disk smoke test): a real scan by a non-admin local account
# (ReclaimSmokeTest) on a real multi-project dev machine, run through SIMPLE mode's then-default
# "Clean My Computer" action (a whole-fixed-drive scan starting at the volume root --
# `POST /api/scan/full-drive`, `reclaim.drives.list_fixed_drives`), classified 13,927 of 13,991
# `dev_artifacts` candidate paths as belonging to OTHER users' profile directories -- reachable
# and deletable only because of broad ACLs granting Modify rights at the volume root, not because
# the scanning user had any legitimate claim on that content. Only agent-level discipline (not
# the product itself) prevented an actual deletion during that smoke test.
#
# The fix (`reclaim.api.service.user_scan_roots`, `POST /api/scan/my-files`): SIMPLE mode's
# default action now scans ONLY the invoking user's own profile (`Path.home()`). A whole-drive
# scan remains available (`POST /api/scan/full-drive`), but only as an explicit, separately-
# surfaced opt-in the SIMPLE-mode UI gates behind its own confirmation dialog -- never the
# default a single click reaches.
#
# Registered in `scripts/verify.py`'s `_SAFETY_GATE_FILES` tuple -- a bare `uv run pytest tests/`
# (which silently skips all of `evals/`, see that script's own docstring) must never be the last
# word on whether this scope boundary still holds.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NOW = 1_700_000_000.0


def _config(protected_root: Path) -> Config:
    root_posix = protected_root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30),
        ),
    )


def _write(path: Path, content: bytes, *, mtime: float = _NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def _make_node_project(project_dir: Path) -> None:
    """A real node_modules directory adjacent to a real package.json -- the exact manifest-
    adjacency shape `detectors.detect_dev_artifacts` requires before proposing a candidate (see
    that function's own docstring: "no manifest adjacent means the path is never proposed")."""
    _write(project_dir / "package.json", b"{}")
    _write(project_dir / "node_modules" / "leftpad" / "index.js", b"x" * 4096)


# --- user_scan_roots: the real default-scope boundary -------------------------------------------


def test_user_scan_roots_default_is_the_real_process_home_directory() -> None:
    """No injected override: the real production default must be exactly `[Path.home()]`, never
    a fixed-drive/volume-root enumeration."""
    result = service.user_scan_roots()
    assert result == [Path.home()]


def test_user_scan_roots_injectable_home_for_tests(tmp_path: Path) -> None:
    fixture_home = tmp_path / "Users" / "ReclaimSmokeTest"
    fixture_home.mkdir(parents=True)
    assert service.user_scan_roots(home=fixture_home) == [fixture_home]


# --- Real multi-account fixture: the actual P0 finding, reproduced and closed ------------------


def test_user_scoped_scan_never_surfaces_another_users_profile_as_a_candidate(
    tmp_path: Path,
) -> None:
    """Real teeth-proof for the P0 finding: a fixture tree simulating a multi-account machine
    (`Users/ReclaimSmokeTest` -- this scan's own root -- sitting NEXT TO `Users/OtherRealUser`,
    each with their own real, manifest-adjacent `node_modules`), scanned via the exact primitive
    `POST /api/scan/my-files` uses (`scanner.scan_tree` rooted at ONLY the invoking user's own
    profile). Confirms every dev_artifacts candidate the resulting index can ever produce stays
    inside the scanned user's own profile -- zero paths reach the sibling account's directory,
    closing the finding rather than merely narrowing it."""
    from reclaim.scanner import scan_tree

    users_root = tmp_path / "Users"
    own_profile = users_root / "ReclaimSmokeTest"
    other_profile = users_root / "OtherRealUser"

    _make_node_project(own_profile / "projects" / "myapp")
    _make_node_project(other_profile / "ml-projects" / "otherapp")

    # The scan itself never even visits `other_profile` -- `user_scan_roots` scopes the walk's
    # root to `own_profile` alone, so `other_profile`'s files are never indexed at all, not just
    # filtered out afterward.
    roots = service.user_scan_roots(home=own_profile)
    assert roots == [own_profile]

    db_path = tmp_path / "index.sqlite3"
    config = _config(tmp_path / "windows")
    with ScanIndex(db_path) as index:
        scan_tree(roots[0], index, incremental=False)

        safety = SafetyValidator(config)
        candidates = generate_candidates(index, config, safety)

    assert len(candidates) >= 1, "the own-profile dev-artifact fixture must produce a candidate"
    other_profile_posix = other_profile.as_posix()
    own_profile_posix = own_profile.as_posix()
    for candidate in candidates:
        candidate_posix = candidate.path.as_posix()
        assert other_profile_posix not in candidate_posix, (
            f"candidate {candidate.path} reaches into another user's profile directory -- "
            "exactly the P0 finding this gate exists to catch"
        )
        assert candidate_posix.startswith(own_profile_posix), (
            f"candidate {candidate.path} is outside the scanned root {own_profile}"
        )

    # Structural confirmation, not just an absence-of-candidates check: the sibling account's
    # directory was never even indexed (the walk never reached it), matching the design intent
    # ("scoped to the invoking user's own profile", not "indexed everything then filtered").
    with ScanIndex(db_path) as index:
        inventory = index.full_inventory(under=users_root)
    assert all(other_profile_posix not in record.path.as_posix() for record in inventory), (
        "the other user's profile must never appear in the index at all -- the walk never "
        "visited it"
    )


# --- Structural gate on the SIMPLE-mode UI wiring itself -----------------------------------------
#
# scripts/verify.py's pytest step never runs the JS test suite (tests/frontend/*.test.mjs) -- see
# that script's own docstring for why the Python-side safety gates are the backstop. This is a
# defense-in-depth structural check on the actual shipped app.js source, independent of whether
# the JS suite is ever run: the primary "Clean My Computer" button's click handler must be
# `startSimpleScan` (whose own body calls `/api/scan/my-files`), and that handler must never
# itself call `/api/scan/full-drive` -- a future edit rewiring the primary button back to a
# whole-drive scan (silently reintroducing the P0 finding) fails this test, not just the JS suite.

_APP_JS_PATH = Path(__file__).resolve().parents[1] / "src" / "reclaim" / "api" / "static" / "app.js"


def _extract_js_function_body(source: str, function_name: str) -> str:
    """Minimal, deliberately non-general brace-matching extraction of one top-level
    `(async )?function <name>() { ... }` body from `app.js`'s real source text -- sufficient for
    this file's own small, non-nested function shapes (see the functions this is actually called
    against below); not a JS parser, and not meant to be one."""
    match = re.search(rf"function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{", source)
    assert match is not None, f"could not find function {function_name} in app.js"
    start = match.end()
    depth = 1
    i = start
    while depth > 0:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start : i - 1]


def test_clean_my_computer_primary_button_is_wired_to_start_simple_scan() -> None:
    source = _APP_JS_PATH.read_text(encoding="utf-8")
    assert 'scanBtn.textContent = "Clean My Computer";' in source
    assert 'scanBtn.addEventListener("click", startSimpleScan);' in source, (
        "the primary 'Clean My Computer' button must be wired to startSimpleScan -- if this "
        "changed, confirm it was NOT rewired back to a whole-drive scan by default"
    )


def test_start_simple_scan_calls_my_files_endpoint_never_full_drive() -> None:
    source = _APP_JS_PATH.read_text(encoding="utf-8")
    body = _extract_js_function_body(source, "startSimpleScan")
    assert "/api/scan/my-files" in body
    assert "/api/scan/full-drive" not in body, (
        "startSimpleScan (the default 'Clean My Computer' action) must never call "
        "/api/scan/full-drive directly -- that P0 finding's exact regression shape"
    )


def test_full_drive_scan_is_reachable_only_through_its_own_explicit_confirm_flow() -> None:
    """`/api/scan/full-drive` must still exist as a real, working opt-in (see
    `test_api.py`'s full-drive orchestration tests) -- but the only call site in app.js must be
    the confirmed handler behind the whole-drive warning dialog, never a second, un-gated path."""
    source = _APP_JS_PATH.read_text(encoding="utf-8")
    call_sites = [line for line in source.splitlines() if '"/api/scan/full-drive"' in line]
    assert len(call_sites) == 1, (
        f"expected exactly one /api/scan/full-drive call site (inside the confirmed handler), "
        f"found {len(call_sites)}: {call_sites}"
    )
    body = _extract_js_function_body(source, "startFullDriveScanConfirmed")
    assert "/api/scan/full-drive" in body
