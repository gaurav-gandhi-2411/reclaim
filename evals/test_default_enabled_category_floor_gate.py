from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reclaim.config import Config, SafetyConfig
from reclaim.detectors import generate_candidates
from reclaim.index import ScanIndex
from reclaim.models import Mode
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# AV4 (2026-08-24 audit): a structural floor against the defect class PR #50 (8.3 short-name
# TEMP resolution), PR #76/AU1 (temp_and_browser_caches never matching when the scan root IS the
# temp directory), and PR #81/AW1 (crash_dumps' WER-report surface, the identical mechanism)
# both belong to -- "a default-enabled category silently finds zero candidates, forever, with no
# error." Three independent instances of this shape have already shipped and been fixed. This is
# not a regression test for any of them specifically (each already has its own dedicated
# regression test in tests/test_detectors.py) -- it is a floor that would catch the NEXT
# mechanism before it ships, by exercising the same boundary every real bug crossed and every
# existing detector unit test does not: real `Config()` DEFAULTS (real env-var resolution via
# `_win_path`/`_resolve_long_path`, not a hand-built path string) against a REAL scanner walk of
# a REAL fixture tree (not a synthetic `ScanIndex.upsert_records` seed, which bypasses the
# scanner entirely and so cannot see a scanner/index-shape bug like AU1's/AW1's).
#
# AX1 (2026-08-24 audit): AW1 exposed a structural gap in this floor itself -- the crash_dumps
# test originally exercised only ONE of that detector's two independent matching surfaces and
# stayed green while the other's bug was live. A sweep of every default-enabled category's
# detector(s) for "more than one independent matching surface" found three more instances of the
# same shape, previously uncovered here:
#
#   - `detect_dev_artifacts`: TWO surfaces. (1) `__pycache__` -- unconditional, no manifest
#     check. (2) every other variant (node_modules, .venv, target/, build/, dist/, .next/,
#     .gradle/) -- gated on an adjacent manifest file also being indexed (`record_exists`), a
#     genuinely different code path. Previously only surface (1) was covered here.
#   - `detect_package_caches`: structurally ONE code path, but fed by TWO independent env-var
#     resolution chains -- `%LOCALAPPDATA%` (pip/npm-cache/uv/Yarn) and `%USERPROFILE%`
#     (.conda/pkgs/.m2/.gradle). Previously only the `LOCALAPPDATA` chain was covered here; a
#     resolution bug scoped to `USERPROFILE` specifically (the exact shape #50's original 8.3
#     bug could have taken, had it hit a different env var) had no floor at all.
#   - `detect_temp_and_browser_caches`: TWO surfaces. (1) `cache_paths` -- whole-directory
#     browser/thumbnail cache match via `files_matching_path_pattern`, no children involved, no
#     self-indexing risk. (2) `temp_roots` -- root + `direct_children`, the surface AU1 actually
#     fixed. Previously only surface (2) was covered here; surface (1) (and its own
#     `LOCALAPPDATA`-derived, glob-pattern resolution chain) had no floor at all.
#   - `detect_crash_dumps`: TWO surfaces, both now covered (AW1, immediately below).
#
# Every category below now asserts EVERY surface its detector has, individually, by `category`
# (not `category_group`) wherever a category_group has more than one — exactly the discipline
# AW1 established for crash_dumps, generalized to the other three.
#
# Coverage: all four categories that default to `enabled=True`
# (`models.REBUILDABLE_CATEGORY_GROUPS`, confirmed via `config.py`'s four `enabled: bool = True`
# class defaults). See each test's own docstring for why its fixture shapes are the realistic
# ones, and the module docstring at the bottom for what's intentionally out of scope.


def _config(*, protected_root: Path) -> Config:
    """Real `Config()` defaults for every category-relevant field (cache paths, temp roots,
    retention, age guard) -- the only override is `safety.protected_roots`, scoped to the
    fixture's own tmp_path so this test never risks matching a real `C:\\Windows`-shaped pattern
    on the actual machine it runs on (same discipline as `evals/test_candidate_generation.py`'s
    `golden_tree_config`/`_config` helpers). `mode=Mode.POWER`: `generate_candidates` always
    includes every generated candidate regardless of mode (SAFE only forces `tier`, never drops a
    candidate -- confirmed by reading `generate_candidates`), so this isn't load-bearing for the
    count assertions below, but matches this repo's established eval convention for clarity.
    """
    return Config(
        safety=SafetyConfig(protected_roots=[f"{protected_root.as_posix()}/__never_matches__"]),
        mode=Mode.POWER,
    )


def _category_counts(candidates: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in candidates:
        counts[c.category] = counts.get(c.category, 0) + 1
    return counts


def test_dev_artifacts_floor(tmp_path: Path) -> None:
    """Both of `detect_dev_artifacts`' two independent surfaces (AX1):

    1. `__pycache__` -- unconditional, no manifest check, matched by name anywhere in the index.
    2. `node_modules` (+ adjacent `package.json`) -- gated on the manifest-adjacency check
       (`record_exists`), a genuinely different code path than surface 1.

    No env var involved in either -- both matched by name/adjacency anywhere in the index, not
    root-relative -- so this floor's job here is purely "does a real scanner walk + real detector
    wiring produce this," not an env-resolution check (that half is covered by the package/temp/
    crash-dump tests below, which all depend on real env-var-driven paths)."""
    project = tmp_path / "some_project"
    pycache = project / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "module.cpython-312.pyc").write_bytes(b"\x00" * 1024)

    node_project = tmp_path / "node_project"
    node_modules = node_project / "node_modules"
    node_modules.mkdir(parents=True)
    (node_project / "package.json").write_text("{}")
    (node_modules / "some_pkg" / "index.js").parent.mkdir(parents=True)
    (node_modules / "some_pkg" / "index.js").write_text("module.exports = {};")

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(project, index, incremental=False)
        scan_tree(node_project, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _category_counts(candidates)
    assert counts.get("dev_artifact_pycache", 0) > 0, (
        f"dev_artifacts is default-enabled and this fixture is its one unconditional match "
        f"shape (__pycache__) -- zero candidates means the category is silently dead on a real "
        f"scan. Full category counts: {counts}"
    )
    assert counts.get("dev_artifact_node_modules", 0) > 0, (
        f"dev_artifacts is default-enabled and this is its manifest-gated surface (node_modules "
        f"+ adjacent package.json) -- zero candidates here means the manifest-adjacency check is "
        f"silently broken on a real scan, independent of whether the pycache surface still "
        f"works. Full category counts: {counts}"
    )


def test_package_caches_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`package_caches` is matched purely by configured directory patterns
    (`_default_package_cache_paths`), fed by TWO independent env-var resolution chains (AX1):
    `%LOCALAPPDATA%` (pip/npm-cache/uv/Yarn) and `%USERPROFILE%` (.conda/pkgs/.m2/.gradle) -- both
    built via `_win_path`, the exact env-var-resolution chokepoint PR #50's 8.3 short-name bug
    lived in. A resolution bug scoped to only ONE of the two env vars (plausible: #50's actual bug
    was specific to how `%TEMP%` happened to resolve on one real account, not `%LOCALAPPDATA%`/
    `%USERPROFILE%` generically) would have no floor if only one chain were exercised.
    Monkeypatching both before `Config()` construction exercises the real resolution path
    (including `_resolve_long_path`'s `GetLongPathNameW` call) for each, rather than bypassing it
    with a hand-built path string."""
    fake_local_appdata = tmp_path / "AppData" / "Local"
    pip_cache = fake_local_appdata / "pip" / "Cache"
    pip_cache.mkdir(parents=True)
    (pip_cache / "some_wheel.whl").write_bytes(b"\x00" * 4096)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    fake_userprofile = tmp_path / "UserProfile"
    m2_repo = fake_userprofile / ".m2" / "repository"
    m2_repo.mkdir(parents=True)
    (m2_repo / "some-artifact.jar").write_bytes(b"\x00" * 2048)
    monkeypatch.setenv("USERPROFILE", str(fake_userprofile))

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(fake_local_appdata, index, incremental=False)
        scan_tree(fake_userprofile, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    paths = {c.path for c in candidates if c.category == "package_cache"}
    assert pip_cache in paths, (
        f"package_caches is default-enabled and its %LOCALAPPDATA%-derived surface (pip/Cache) "
        f"should match this fixture -- zero candidates here means that env-var-to-pattern "
        f"resolution chain is silently broken. package_cache paths found: {paths}"
    )
    assert m2_repo in paths, (
        f"package_caches is default-enabled and its %USERPROFILE%-derived surface (.m2/"
        f"repository) should match this fixture -- zero candidates here means that env-var-to-"
        f"pattern resolution chain is silently broken, independent of whether the LOCALAPPDATA "
        f"chain still works. package_cache paths found: {paths}"
    )


def test_temp_and_browser_caches_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both of `detect_temp_and_browser_caches`' two independent surfaces (AX1), asserted by
    CATEGORY:

    1. `cache_paths` -- whole-directory browser cache match via `files_matching_path_pattern`
       (glob pattern, `%LOCALAPPDATA%`-derived, no children involved, no self-indexing risk) --
       `browser_cache`.
    2. `temp_roots` -- root + `direct_children`, gated by `min_temp_root_age_hours` --
       `windows_temp`. AU1's exact real-world shape: the scan root IS the temp directory itself
       (`%TEMP%` monkeypatched, scanned directly -- not a parent directory containing it), which
       is precisely the case every prior unit test avoided (each seeded an index row for the root
       itself, which a real scan of that exact root never produces) and precisely the case a real
       "scan just my temp folder" flow or this project's own AC3 trip's check 1e hits. Kept
       structurally separate from surface 1's scan (a parent-rooted walk of `%LOCALAPPDATA%`) so
       surface 2's self-indexing shape stays exact -- if `%TEMP%` were scanned via a parent walk
       instead, this test would no longer be evidence for the specific mechanism AU1 fixed.

    The fixture file's mtime for surface 2 is backdated past `min_temp_root_age_hours`'s default
    (7 days) -- a file left at "now" would correctly be excluded by the age guard and this test
    would be asserting the wrong thing.

    Not asserted separately: `thumbnail_cache` shares surface 1's exact code path (same
    `cache_paths` loop, differing only by an `is_thumbnail` filename check) -- not an
    independently-reachable mechanism, so it isn't treated as a third surface here."""
    fake_local_appdata = tmp_path / "AppData" / "Local"
    chrome_cache = fake_local_appdata / "Google" / "Chrome" / "User Data" / "Default" / "Cache"
    chrome_cache.mkdir(parents=True)
    (chrome_cache / "data_0").write_bytes(b"\x00" * 4096)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    fake_temp = tmp_path / "faketemp"
    fake_temp.mkdir(parents=True)
    stale_file = fake_temp / "leftover.tmp"
    stale_file.write_bytes(b"\x00" * 2048)
    ten_days_ago = time.time() - (10 * 24 * 3600)
    os.utime(stale_file, (ten_days_ago, ten_days_ago))
    monkeypatch.setenv("TEMP", str(fake_temp))

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        # Parent-rooted walk so `chrome_cache` gets its own index row (surface 1 needs no
        # self-indexing shape); a SEPARATE walk rooted exactly AT fake_temp for surface 2 (AU1's
        # shape -- see this test's own docstring for why these must stay structurally distinct).
        scan_tree(fake_local_appdata, index, incremental=False)
        scan_tree(fake_temp, index, incremental=False)  # scan root == the temp dir itself (AU1)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _category_counts(candidates)
    assert counts.get("browser_cache", 0) > 0, (
        f"temp_and_browser_caches is default-enabled and this is its cache_paths/whole-directory "
        f"surface (a real Chrome Cache dir under the real %LOCALAPPDATA%-derived glob pattern) "
        f"-- zero candidates here means that surface is silently broken, independent of whether "
        f"the temp_roots surface still works. Full category counts: {counts}"
    )
    assert counts.get("windows_temp", 0) > 0, (
        f"temp_and_browser_caches is default-enabled and this is AU1's exact real-world shape "
        f"(scan root == %TEMP% itself, a stale direct child inside) -- zero candidates here is "
        f"the specific mechanism PR #76 fixed; a regression would reproduce it silently again. "
        f"Full category counts: {counts}"
    )


def test_crash_dumps_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both of `detect_crash_dumps`' two independent detection surfaces, in one test, asserted
    by CATEGORY (not just category_group) so a bug in either alone still fails this test even if
    the other keeps the group count non-zero:

    1. `.dmp` files anywhere (`files_by_ext`, config-independent) -- `crash_dump_file`.
    2. The WER-report surface (`root_paths` + `direct_children`, gated on
       `%LOCALAPPDATA%\\CrashDumps`) -- `crash_dump_wer_report`. AW1 (2026-08-24 audit):
       live-reproduced this session -- this surface carried the IDENTICAL self-indexing bug AU1
       fixed for `temp_and_browser_caches` (`files_matching_path_pattern` requiring an index row
       for the root itself before ever looking at `direct_children`, which a scan rooted exactly
       AT that directory never produces). Fixed alongside this test. Scanning the CrashDumps
       directory itself as the root (not a parent) is the exact shape that caught it -- an
       earlier version of this test used only surface 1 and passed while surface 2's bug was
       still live, which is itself the finding AW1 asked to record: a green floor test that only
       exercises one of two surfaces under the same category_group provides no evidence about
       the other. Asserting per-category, not per-group, is what closes that gap -- and is the
       pattern AX1 generalized to the other three categories above.
    """
    fake_local_appdata = tmp_path / "AppData" / "Local"
    crashdumps_root = fake_local_appdata / "CrashDumps"
    crashdumps_root.mkdir(parents=True)
    (crashdumps_root / "Report.wer").write_bytes(b"\x00" * 512)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    project = tmp_path / "some_project"
    project.mkdir(parents=True)
    (project / "app_crash.dmp").write_bytes(b"\x00" * 8192)

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        # Scan root == CrashDumps itself (AU1/AW1's exact self-indexing shape) for surface 2;
        # a separate scan of `project` for surface 1's unrelated .dmp fixture.
        scan_tree(crashdumps_root, index, incremental=False)
        scan_tree(project, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _category_counts(candidates)
    assert counts.get("crash_dump_file", 0) > 0, (
        f"crash_dumps is default-enabled and a bare .dmp file anywhere in the index is its "
        f"simplest, config-independent match shape -- zero candidates means the real "
        f"scanner-to-extension-index path is silently broken. Full category counts: {counts}"
    )
    assert counts.get("crash_dump_wer_report", 0) > 0, (
        f"crash_dumps is default-enabled and this is AU1's exact self-indexing shape applied to "
        f"the WER-report surface (scan root == %LOCALAPPDATA%\\CrashDumps itself) -- zero "
        f"candidates here is the AW1 mechanism, live-reproduced and fixed this session; a "
        f"regression would reproduce it silently again. Full category counts: {counts}"
    )


# --- What this floor does NOT cover, and why -------------------------------------------------
#
# All four default-enabled categories (dev_artifacts, package_caches, temp_and_browser_caches,
# crash_dumps) ARE covered above -- every category in `models.REBUILDABLE_CATEGORY_GROUPS` -- and
# every independent matching surface within each (AX1's sweep) is asserted individually by
# `category`, not the coarser `category_group`.
#
# One residual, intentional exclusion: `model_caches`, `old_installers`, `archive_pairs`,
# `large_logs`, `duplicates` are excluded on purpose -- none of them default to `enabled=True`
# (confirmed via `config.py`'s five remaining `enabled: bool = False` class defaults), so
# "silently finds zero on a default install" isn't a meaningful failure mode for them -- zero is
# the correct, expected result until a user explicitly opts in. This floor is scoped to what a
# user can hit with zero configuration, which is exactly the shape every real bug it guards
# against had.
