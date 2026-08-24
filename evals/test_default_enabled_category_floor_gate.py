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
# TEMP resolution) and PR #76/AU1 (temp_and_browser_caches never matching when the scan root IS
# the temp directory) both belong to -- "a default-enabled category silently finds zero
# candidates, forever, with no error." Two independent mechanisms have already produced this
# exact shape. This is not a regression test for either bug specifically (both already have their
# own dedicated regression tests in tests/test_detectors.py) -- it is a floor that would catch a
# THIRD, as-yet-unknown mechanism before it ships, by exercising the same boundary both real bugs
# crossed and every existing detector unit test does not: real `Config()` DEFAULTS (real env-var
# resolution via `_win_path`/`_resolve_long_path`, not a hand-built path string) against a REAL
# scanner walk of a REAL fixture tree (not a synthetic `ScanIndex.upsert_records` seed, which
# bypasses the scanner entirely and so cannot see a scanner/index-shape bug like AU1's).
#
# Coverage: all four categories that default to `enabled=True`
# (`models.REBUILDABLE_CATEGORY_GROUPS`, confirmed via `config.py`'s four `enabled: bool = True`
# class defaults) get one test each below. See each test's own docstring for why its specific
# fixture shape is the realistic one, and the module docstring end for what could NOT be given
# this treatment and why.


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


def _group_counts(candidates: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in candidates:
        counts[c.category_group] = counts.get(c.category_group, 0) + 1
    return counts


def test_dev_artifacts_floor(tmp_path: Path) -> None:
    """`__pycache__` needs no manifest (`detect_dev_artifacts`'s one unconditional match, see its
    own docstring) -- the simplest real shape a default install actually produces on its first
    scan of any Python project. No env var involved; matched by name anywhere in the index, not
    root-relative -- so this floor's job here is purely "does a real scanner walk + real detector
    wiring produce this," not an env-resolution check (that half is covered by the temp/package/
    crash-dump tests below, which all depend on real env-var-driven paths)."""
    project = tmp_path / "some_project"
    pycache = project / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "module.cpython-312.pyc").write_bytes(b"\x00" * 1024)

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(project, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _group_counts(candidates)
    assert counts.get("dev_artifacts", 0) > 0, (
        f"dev_artifacts is default-enabled and this fixture is its one unconditional match "
        f"shape (__pycache__) -- zero candidates means the category is silently dead on a real "
        f"scan. Full group counts: {counts}"
    )


def test_package_caches_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`package_caches` is matched purely by configured directory patterns
    (`_default_package_cache_paths`), every one of them built from `%LOCALAPPDATA%`/
    `%USERPROFILE%` via `_win_path` -- the exact env-var-resolution chokepoint PR #50's 8.3
    short-name bug lived in. Monkeypatching `LOCALAPPDATA` before `Config()` construction
    exercises that real resolution path (including `_resolve_long_path`'s `GetLongPathNameW`
    call) rather than bypassing it with a hand-built path string."""
    fake_local_appdata = tmp_path / "AppData" / "Local"
    pip_cache = fake_local_appdata / "pip" / "Cache"
    pip_cache.mkdir(parents=True)
    (pip_cache / "some_wheel.whl").write_bytes(b"\x00" * 4096)
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local_appdata))

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(fake_local_appdata, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _group_counts(candidates)
    assert counts.get("package_caches", 0) > 0, (
        f"package_caches is default-enabled and its own default `LOCALAPPDATA`-derived path "
        f"pattern (pip/Cache) should match this fixture -- zero candidates means the real "
        f"env-var-to-pattern resolution chain is silently broken. Full group counts: {counts}"
    )


def test_temp_and_browser_caches_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AU1's exact real-world shape: the scan root IS the temp directory itself (`%TEMP%`
    monkeypatched, scanned directly -- not a parent directory containing it), which is precisely
    the case every prior unit test avoided (each seeded an index row for the root itself, which a
    real scan of that exact root never produces) and precisely the case a real "scan just my temp
    folder" flow or this project's own AC3 trip's check 1e hits. The fixture file's mtime is
    backdated past `min_temp_root_age_hours`'s default (7 days) -- a file left at "now" would
    correctly be excluded by the age guard and this test would be asserting the wrong thing."""
    fake_temp = tmp_path / "faketemp"
    fake_temp.mkdir(parents=True)
    stale_file = fake_temp / "leftover.tmp"
    stale_file.write_bytes(b"\x00" * 2048)
    ten_days_ago = time.time() - (10 * 24 * 3600)
    os.utime(stale_file, (ten_days_ago, ten_days_ago))
    monkeypatch.setenv("TEMP", str(fake_temp))

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(fake_temp, index, incremental=False)  # scan root == the temp dir itself (AU1)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _group_counts(candidates)
    assert counts.get("temp_and_browser_caches", 0) > 0, (
        f"temp_and_browser_caches is default-enabled and this is AU1's exact real-world shape "
        f"(scan root == %TEMP% itself, a stale direct child inside) -- zero candidates here is "
        f"the specific mechanism PR #76 fixed; a regression would reproduce it silently again. "
        f"Full group counts: {counts}"
    )


def test_crash_dumps_floor(tmp_path: Path) -> None:
    """`.dmp` files match anywhere in the inventory via `files_by_ext` (`detect_crash_dumps`'s
    first, config-independent detection surface) -- no env var or configured root needed for
    this shape, so this floor exercises the real scanner-to-extension-index path rather than an
    env-resolution chain (the WER-report `direct_children` surface lower in the same detector
    does depend on `%LOCALAPPDATA%`/CrashDumps and, per a straight reading of its code, appears
    to have the identical "requires an index row for the root itself" shape AU1 fixed for
    temp_and_browser_caches -- not exercised by this floor, flagged here rather than silently
    left uncovered; see the module-level note at the bottom of this file)."""
    project = tmp_path / "some_project"
    project.mkdir(parents=True)
    (project / "app_crash.dmp").write_bytes(b"\x00" * 8192)

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(project, index, incremental=False)
        config = _config(protected_root=tmp_path)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=time.time())

    counts = _group_counts(candidates)
    assert counts.get("crash_dumps", 0) > 0, (
        f"crash_dumps is default-enabled and a bare .dmp file anywhere in the index is its "
        f"simplest, config-independent match shape -- zero candidates means the real "
        f"scanner-to-extension-index path is silently broken. Full group counts: {counts}"
    )


# --- What this floor does NOT cover, and why -------------------------------------------------
#
# All four default-enabled categories (dev_artifacts, package_caches, temp_and_browser_caches,
# crash_dumps) ARE covered above -- every category in `models.REBUILDABLE_CATEGORY_GROUPS`. Two
# residual gaps, disclosed rather than silently left out:
#
# 1. `detect_crash_dumps`'s WER-report surface (`root_paths` + `direct_children`, the second half
#    of that detector, gated on `%LOCALAPPDATA%\CrashDumps`/`C:\ProgramData\...\WER`) is not
#    exercised here -- the `.dmp`-anywhere surface above already gives crash_dumps a non-zero
#    floor, so adding a second fixture wasn't necessary to satisfy this task's requirement, but a
#    straight reading of that surface's code (`files_matching_path_pattern(pattern, is_dir=True)`
#    before `direct_children`, structurally identical to what AU1 fixed) suggests it may carry
#    the same latent self-indexing bug if `root_paths` ever equals a scan root exactly. Not
#    reproduced or fixed here -- this task is the floor test, not a new investigation; flagged for
#    a follow-up.
# 2. `model_caches`, `old_installers`, `archive_pairs`, `large_logs`, `duplicates` are excluded on
#    purpose: none of them default to `enabled=True` (confirmed via `config.py`'s five remaining
#    `enabled: bool = False` class defaults), so "silently finds zero on a default install" isn't
#    a meaningful failure mode for them -- zero is the correct, expected result until a user
#    explicitly opts in. This floor is scoped to what a user can hit with zero configuration, which
#    is exactly the shape both real bugs it guards against had.
