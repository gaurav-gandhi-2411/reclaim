from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from reclaim.config import (
    ArchivePairsConfig,
    CategoriesConfig,
    Config,
    CrashDumpsConfig,
    DevArtifactsConfig,
    ModelCachesConfig,
    PackageCachesConfig,
    SafetyConfig,
)
from reclaim.detectors import (
    _category_enabled,
    _category_retention_days,
    _category_size_guard_exempt,
    _dedupe_by_path,
    _drop_nested_candidates,
    detect_archive_pairs,
    detect_crash_dumps,
    detect_dev_artifacts,
    detect_large_logs,
    detect_model_caches,
    detect_old_installers,
    detect_package_caches,
    detect_temp_and_browser_caches,
    generate_candidates,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, FileRecord, RawCandidate, Tier
from reclaim.safety import SafetyValidator

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _record(
    path: str,
    *,
    is_dir: bool = False,
    size_bytes: int = 1024,
    mtime: float = _NOW,
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=0,
        ext=p.suffix.lower() if not is_dir else "",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=mtime,
    )


@pytest.fixture
def index(tmp_path: Path) -> Iterator[ScanIndex]:
    idx = ScanIndex(tmp_path / "index.sqlite3")
    try:
        yield idx
    finally:
        idx.close()


def _seed(index: ScanIndex, *records: FileRecord) -> None:
    """Populates `index` the same way a real scan would (via `upsert_records`), so every
    detector test below exercises the actual SQL-pushdown query path (`files_by_name`/
    `files_by_ext`/`files_larger_than`/`files_matching_path_pattern`/`direct_children`/
    `record_exists`) rather than an in-memory stand-in."""
    index.upsert_records(list(records), scanned_at=_NOW)


def _paths(candidates: list[RawCandidate]) -> set[Path]:
    return {c.path for c in candidates}


# --- Dev artifacts: manifest-adjacency ------------------------------------------------------


def test_node_modules_with_adjacent_manifest_is_proposed(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Proj/package.json"),
        _record("C:/Proj/node_modules", is_dir=True),
        _record("C:/Proj/node_modules/pkg/index.js"),
    )
    result = detect_dev_artifacts(index)
    assert Path("C:/Proj/node_modules") in _paths(result)
    candidate = next(c for c in result if c.path == Path("C:/Proj/node_modules"))
    assert candidate.category == "dev_artifact_node_modules"
    assert candidate.category_group == "dev_artifacts"
    assert candidate.suggested_tier == Tier.A
    assert "package.json" in candidate.rationale
    assert candidate.rebuild_instruction is not None


def test_node_modules_without_adjacent_manifest_is_never_proposed(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Proj/node_modules", is_dir=True),
        _record("C:/Proj/node_modules/pkg/index.js"),
    )
    result = detect_dev_artifacts(index)
    assert Path("C:/Proj/node_modules") not in _paths(result)
    assert result == []


@pytest.mark.parametrize(
    ("dir_name", "manifest_name"),
    [
        (".venv", "pyproject.toml"),
        ("venv", "requirements.txt"),
        ("target", "Cargo.toml"),
        ("target", "pom.xml"),
        ("build", "setup.py"),
        ("dist", "package.json"),
        (".next", "package.json"),
        (".gradle", "build.gradle.kts"),
    ],
)
def test_dev_artifact_variants_require_their_own_manifest_set(
    index: ScanIndex, dir_name: str, manifest_name: str
) -> None:
    _seed(
        index,
        _record(f"C:/Proj/{manifest_name}"),
        _record(f"C:/Proj/{dir_name}", is_dir=True),
    )
    result = detect_dev_artifacts(index)
    assert Path(f"C:/Proj/{dir_name}") in _paths(result)


def test_pycache_needs_no_manifest(index: ScanIndex) -> None:
    _seed(index, _record("C:/Proj/__pycache__", is_dir=True))
    result = detect_dev_artifacts(index)
    assert Path("C:/Proj/__pycache__") in _paths(result)
    candidate = result[0]
    assert candidate.category == "dev_artifact_pycache"


def test_unrelated_directory_name_is_not_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Proj/src", is_dir=True))
    assert detect_dev_artifacts(index) == []


# --- Package caches / temp & browser caches / crash dumps ----------------------------------


def test_package_cache_matches_configured_pattern(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/AppData/Local/pip/Cache", is_dir=True))
    result = detect_package_caches(index, ["C:/Users/gg/AppData/Local/pip/Cache"])
    assert Path("C:/Users/gg/AppData/Local/pip/Cache") in _paths(result)


def test_package_cache_does_not_match_unrelated_dir(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Documents/notes", is_dir=True))
    result = detect_package_caches(index, ["C:/Users/gg/AppData/Local/pip/Cache"])
    assert result == []


# --- Model-weight caches (ADR-0003) ----------------------------------------------------------


def test_model_cache_matches_configured_hub_directory(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/.cache/huggingface/hub", is_dir=True))
    result = detect_model_caches(
        index, ["C:/Users/gg/.cache/huggingface/hub"], [".safetensors", ".ckpt", ".bin"]
    )
    assert Path("C:/Users/gg/.cache/huggingface/hub") in _paths(result)
    candidate = next(c for c in result if c.path == Path("C:/Users/gg/.cache/huggingface/hub"))
    assert candidate.category == "model_cache"
    assert candidate.category_group == "model_caches"
    assert candidate.suggested_tier == Tier.B
    assert candidate.recovery_cost_note is not None


def test_model_cache_matches_safetensors_file_under_configured_root(index: ScanIndex) -> None:
    """A model-weight file sitting directly under a configured root (not itself matched by the
    whole-directory sweep, e.g. a hub layout the directory pattern doesn't cover) must still be
    caught by the extension-based surface — scoped to the same configured root, never a
    disk-wide sweep."""
    _seed(
        index,
        _record(
            "C:/Users/gg/.cache/huggingface/hub/model.safetensors",
            size_bytes=5_000_000_000,
        ),
    )
    result = detect_model_caches(
        index, ["C:/Users/gg/.cache/huggingface/hub"], [".safetensors", ".ckpt", ".bin"]
    )
    assert Path("C:/Users/gg/.cache/huggingface/hub/model.safetensors") in _paths(result)
    candidate = result[0]
    assert candidate.category == "model_cache"
    assert candidate.category_group == "model_caches"
    assert candidate.suggested_tier == Tier.B


def test_model_cache_does_not_match_files_outside_configured_roots(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Documents/my_project/weights.safetensors"))
    result = detect_model_caches(
        index, ["C:/Users/gg/.cache/huggingface/hub"], [".safetensors", ".ckpt", ".bin"]
    )
    assert result == []


def test_model_cache_never_reaches_tier_a_even_when_category_enabled(
    tmp_path: Path, index: ScanIndex
) -> None:
    """The core ADR-0003 invariant: a model-cache candidate is Tier B (review-queue) no matter
    what `config.categories.model_caches.enabled` says — unlike every other category, there is
    no config knob that promotes it to Tier A/auto-quarantine-eligible."""
    hub = tmp_path / "hub"
    _seed(index, _record(hub.as_posix(), is_dir=True, size_bytes=125_000_000_000))

    for enabled in (False, True):
        config = Config(
            safety=SafetyConfig(protected_roots=[]),
            categories=CategoriesConfig(
                model_caches=ModelCachesConfig(enabled=enabled, paths=[hub.as_posix()])
            ),
        )
        candidates: list[Candidate] = generate_candidates(
            index, config, SafetyValidator(config), now=_NOW
        )
        model_candidates = [c for c in candidates if c.category_group == "model_caches"]
        assert len(model_candidates) == 1
        assert model_candidates[0].tier == Tier.B
        assert model_candidates[0].retention_days == 30


def test_model_cache_default_retention_is_vaulted_not_none() -> None:
    """Unlike every other `retention_days=None`-by-default cache category, model caches default
    to vaulted (30-day) retention — the whole point of ADR-0003."""
    assert _category_retention_days("model_caches", Config()) == 30


def test_temp_root_children_proposed_but_never_the_root_itself(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Users/gg/AppData/Local/Temp", is_dir=True),
        _record("C:/Users/gg/AppData/Local/Temp/scratch.tmp"),
    )
    result = detect_temp_and_browser_caches(
        index,
        cache_paths=[],
        temp_roots=["C:/Users/gg/AppData/Local/Temp"],
        min_temp_root_age_hours=0.0,
        now=_NOW,
    )
    paths = _paths(result)
    assert Path("C:/Users/gg/AppData/Local/Temp/scratch.tmp") in paths
    assert Path("C:/Users/gg/AppData/Local/Temp") not in paths


def test_temp_root_children_found_even_when_the_root_itself_has_no_index_row(
    index: ScanIndex,
) -> None:
    """AU1 (2026-08-24 audit): live-reproduced, silent zero-yield -- the test above seeds an
    explicit index row for the temp root itself (`_record(..., is_dir=True)`), which is NOT what
    a real scan produces when the temp root IS the scan's own root: a scan never indexes its own
    starting directory as a row of itself, only the directories/files reached by walking it. The
    original implementation required `files_matching_path_pattern` to find a row matching the
    root before it would look at that root's children at all -- so scanning `%TEMP%` directly
    (the AC3 trip's own check 1e, and any real "just scan my temp folder" UI flow) produced ZERO
    `windows_temp` candidates, permanently, silently, regardless of how much real eligible
    content existed underneath -- live-reproduced against a real ~590k-entry account scan with
    2,741 genuinely eligible files, found zero, before this fix. Deliberately seeds NO row for
    the root itself, unlike the test above -- that's the realistic shape this test exists to
    cover."""
    _seed(
        index,
        _record("C:/Users/gg/AppData/Local/Temp/scratch.tmp", mtime=_NOW - 10 * _DAY),
    )
    result = detect_temp_and_browser_caches(
        index,
        cache_paths=[],
        temp_roots=["C:/Users/gg/AppData/Local/Temp"],
        min_temp_root_age_hours=0.0,
        now=_NOW,
    )
    paths = _paths(result)
    assert Path("C:/Users/gg/AppData/Local/Temp/scratch.tmp") in paths


def test_temp_root_glob_pattern_still_requires_an_indexed_match(index: ScanIndex) -> None:
    """The literal-path fast path above must not silently widen a genuine glob pattern's
    semantics: a `temp_roots` entry containing `*`/`?` can match zero, one, or several real
    directories, which isn't known without searching the index -- unlike a literal path, `Path(
    pattern)` alone would be wrong here (it isn't a real, single directory). Confirms glob
    patterns are unaffected by this fix: still zero candidates when nothing in the index matches
    the glob, and still found when something does."""
    result_no_match = detect_temp_and_browser_caches(
        index,
        cache_paths=[],
        temp_roots=["C:/Users/*/AppData/Local/Temp"],
        min_temp_root_age_hours=0.0,
        now=_NOW,
    )
    assert result_no_match == []

    _seed(
        index,
        _record("C:/Users/alice/AppData/Local/Temp", is_dir=True),
        _record("C:/Users/alice/AppData/Local/Temp/scratch.tmp", mtime=_NOW - 10 * _DAY),
    )
    result_matched = detect_temp_and_browser_caches(
        index,
        cache_paths=[],
        temp_roots=["C:/Users/*/AppData/Local/Temp"],
        min_temp_root_age_hours=0.0,
        now=_NOW,
    )
    assert Path("C:/Users/alice/AppData/Local/Temp/scratch.tmp") in _paths(result_matched)


@pytest.mark.skipif(os.name != "nt", reason="Win32-only: GetLongPathNameW/GetShortPathNameW")
def test_windows_temp_candidates_proposed_when_temp_env_var_was_short_form(
    index: ScanIndex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression proof for the P0 fix (2026-08 session, live-reproduced): before
    `config._win_path` resolved 8.3 short-name (DOS alias) env vars to their long form, a `%TEMP%`
    value that came back short-form (a real, confirmed-live condition on any long-enough username
    -- `C:\\Users\\RECLAI~1\\AppData\\Local\\Temp`) built a `temp_and_browser_caches` `temp_roots`
    pattern that could structurally never match the scanner's real long-form indexed paths --
    `detect_temp_and_browser_caches` proposed zero `windows_temp` candidates, permanently.

    Proves the fix across the REAL full call chain: `config._default_temp_roots()` (reading a
    genuinely short-form `%TEMP%`, via a real `GetShortPathNameW` round trip, not a hand-authored
    fake short name) -> `detect_temp_and_browser_caches` (matching against an index seeded with
    real long-form paths, exactly as a real scan would produce)."""
    import ctypes
    import ctypes.wintypes

    from reclaim.config import _default_temp_roots

    long_temp_dir = tmp_path / "a_long_enough_directory_name_for_8dot3_generation" / "Temp"
    long_temp_dir.mkdir(parents=True)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.restype = ctypes.wintypes.DWORD
    kernel32.GetShortPathNameW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPWSTR,
        ctypes.wintypes.DWORD,
    ]
    buf = ctypes.create_unicode_buffer(260)
    length = kernel32.GetShortPathNameW(str(long_temp_dir), buf, 260)
    assert length != 0, f"GetShortPathNameW failed for {long_temp_dir!r}"
    short_temp = buf.value
    if "~" not in short_temp:
        # Checked against the FULL path, not just its last component -- only the
        # `a_long_enough_directory_name_for_8dot3_generation` segment is long enough to trigger
        # short-name generation; the trailing `Temp` segment is already short-form-eligible-length
        # either way, so it alone would never carry the `~` marker.
        pytest.skip(
            "8.3 short-name generation is disabled for this volume "
            "(NtfsDisable8dot3NameCreation) -- cannot exercise the real short-form condition."
        )

    monkeypatch.setenv("TEMP", short_temp)

    temp_roots = _default_temp_roots()
    resolved_root = Path(temp_roots[0])
    # The fix itself: the resolved root must be the real LONG form, never the short alias just
    # injected via the env var above.
    assert "~" not in str(resolved_root)

    _seed(
        index,
        _record(long_temp_dir.as_posix(), is_dir=True),
        _record((long_temp_dir / "scratch.tmp").as_posix()),
    )

    result = detect_temp_and_browser_caches(
        index, cache_paths=[], temp_roots=temp_roots, min_temp_root_age_hours=0.0, now=_NOW
    )

    assert (long_temp_dir / "scratch.tmp") in _paths(result)


def test_thumbnail_cache_is_categorized_distinctly_from_browser_cache(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Users/gg/AppData/Local/Microsoft/Windows/Explorer/thumbcache_256.db"),
        _record("C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache", is_dir=True),
    )
    result = detect_temp_and_browser_caches(
        index,
        cache_paths=[
            "*/thumbcache_*.db",
            "C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache",
        ],
        temp_roots=[],
        min_temp_root_age_hours=0.0,
        now=_NOW,
    )
    categories = {c.path.name: c.category for c in result}
    assert categories["thumbcache_256.db"] == "thumbnail_cache"
    assert categories["Cache"] == "browser_cache"


def test_crash_dump_file_detected_anywhere(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Desktop/app.dmp"))
    result = detect_crash_dumps(index, root_paths=[])
    assert Path("C:/Users/gg/Desktop/app.dmp") in _paths(result)


def test_wer_root_children_proposed_but_never_the_root_itself(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/ProgramData/Microsoft/Windows/WER", is_dir=True),
        _record("C:/ProgramData/Microsoft/Windows/WER/ReportQueue", is_dir=True),
    )
    result = detect_crash_dumps(index, root_paths=["C:/ProgramData/Microsoft/Windows/WER"])
    paths = _paths(result)
    assert Path("C:/ProgramData/Microsoft/Windows/WER/ReportQueue") in paths
    assert Path("C:/ProgramData/Microsoft/Windows/WER") not in paths


def test_wer_root_children_found_even_when_the_root_itself_has_no_index_row(
    index: ScanIndex,
) -> None:
    """AW1 (2026-08-24 audit): live-reproduced, silent zero-yield -- identical mechanism and
    identical masking pattern to AU1 (`test_temp_root_children_found_even_when_the_root_itself_
    has_no_index_row` above), just in `detect_crash_dumps`' WER-report surface instead of
    `detect_temp_and_browser_caches`. The test above seeds an explicit index row for the WER root
    itself, which is NOT what a real scan produces when that root IS the scan's own root (e.g. a
    scan rooted directly at `%LOCALAPPDATA%\\CrashDumps`) -- a scan never indexes its own starting
    directory as a row of itself. The original implementation required `files_matching_path_
    pattern` to find a row matching the root before looking at its children at all, so scanning
    the CrashDumps/WER root directly produced ZERO `crash_dump_wer_report` candidates,
    permanently, silently -- live-reproduced this session (a real `.wer`-shaped file at exactly
    that root, scanned as the root: 0 candidates before this fix, 1 after). Deliberately seeds NO
    row for the root itself, unlike the test above -- that's the realistic shape this test exists
    to cover."""
    _seed(
        index,
        _record("C:/Users/gg/AppData/Local/CrashDumps/Report.wer"),
    )
    result = detect_crash_dumps(index, root_paths=["C:/Users/gg/AppData/Local/CrashDumps"])
    paths = _paths(result)
    assert Path("C:/Users/gg/AppData/Local/CrashDumps/Report.wer") in paths


def test_wer_root_glob_pattern_still_requires_an_indexed_match(index: ScanIndex) -> None:
    """The literal-path fast path above must not silently widen a genuine glob pattern's
    semantics -- same regression proof as `test_temp_root_glob_pattern_still_requires_an_indexed_
    match` above, applied to `detect_crash_dumps`."""
    result_no_match = detect_crash_dumps(index, root_paths=["C:/Users/*/AppData/Local/CrashDumps"])
    assert result_no_match == []

    _seed(
        index,
        _record("C:/Users/alice/AppData/Local/CrashDumps", is_dir=True),
        _record("C:/Users/alice/AppData/Local/CrashDumps/Report.wer"),
    )
    result_matched = detect_crash_dumps(index, root_paths=["C:/Users/*/AppData/Local/CrashDumps"])
    assert Path("C:/Users/alice/AppData/Local/CrashDumps/Report.wer") in _paths(result_matched)


# --- Old installers: age threshold ----------------------------------------------------------


def test_old_installer_past_threshold_is_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Downloads/setup.exe", mtime=_NOW - 120 * _DAY))
    result = detect_old_installers(index, max_age_days=90, now=_NOW)
    assert Path("C:/Users/gg/Downloads/setup.exe") in _paths(result)
    assert result[0].suggested_tier == Tier.A  # uniform detector-level suggestion


def test_recent_installer_under_threshold_is_never_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Downloads/setup.exe", mtime=_NOW - 10 * _DAY))
    result = detect_old_installers(index, max_age_days=90, now=_NOW)
    assert result == []


def test_installer_outside_downloads_is_never_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Desktop/setup.exe", mtime=_NOW - 400 * _DAY))
    result = detect_old_installers(index, max_age_days=90, now=_NOW)
    assert result == []


def test_non_installer_extension_in_downloads_is_never_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Users/gg/Downloads/report.pdf", mtime=_NOW - 400 * _DAY))
    result = detect_old_installers(index, max_age_days=90, now=_NOW)
    assert result == []


# --- Archive pairs: overlap threshold --------------------------------------------------------


def test_archive_with_matching_extracted_dir_proposes_only_the_archive(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Data/photos.zip"),
        _record("C:/Data/photos", is_dir=True),
        _record("C:/Data/photos/img1.jpg"),
    )
    result = detect_archive_pairs(index)
    paths = _paths(result)
    assert Path("C:/Data/photos.zip") in paths
    assert Path("C:/Data/photos") not in paths
    assert Path("C:/Data/photos/img1.jpg") not in paths
    assert "extracted copy is being kept" in result[0].rationale


def test_tar_gz_compound_suffix_is_stripped_before_matching(index: ScanIndex) -> None:
    """Also pins down the `ext`-column prefilter gap noted in `detectors.py`: `Path.suffix` for
    'backup.tar.gz' is '.gz', not '.tar.gz', so the prefilter set must include '.gz' — and
    `_archive_stem` must still correctly require the full '.tar.gz' suffix afterward."""
    _seed(
        index,
        _record("C:/Data/backup.tar.gz"),
        _record("C:/Data/backup", is_dir=True),
    )
    result = detect_archive_pairs(index)
    assert Path("C:/Data/backup.tar.gz") in _paths(result)


def test_bare_gz_file_without_tar_is_not_proposed(index: ScanIndex) -> None:
    """A '.gz' file that is *not* '.tar.gz' must survive the ext-column prefilter (ext='.gz'
    matches) but still be rejected by `_archive_stem`'s exact-suffix check, since only
    '.tar.gz' is a recognized archive suffix — '.gz' alone is not."""
    _seed(
        index,
        _record("C:/Data/plain.gz"),
        _record("C:/Data/plain", is_dir=True),
    )
    result = detect_archive_pairs(index)
    assert result == []


def test_archive_with_low_overlap_sibling_is_not_proposed(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/Data/photos.zip"),
        _record("C:/Data/unrelated_stuff", is_dir=True),
    )
    result = detect_archive_pairs(index)
    assert result == []


def test_archive_with_no_sibling_directory_is_not_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/Data/photos.zip"))
    result = detect_archive_pairs(index)
    assert result == []


# --- Large logs: size and age thresholds -----------------------------------------------------

_50MB = 50 * 1024 * 1024


def test_large_old_log_is_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/App/logs/app.log", size_bytes=_50MB + 1, mtime=_NOW - 45 * _DAY))
    result = detect_large_logs(index, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert Path("C:/App/logs/app.log") in _paths(result)


def test_large_recent_log_is_not_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/App/logs/app.log", size_bytes=_50MB + 1, mtime=_NOW - 2 * _DAY))
    result = detect_large_logs(index, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert result == []


def test_small_old_log_is_not_proposed(index: ScanIndex) -> None:
    _seed(index, _record("C:/App/logs/app.log", size_bytes=1024, mtime=_NOW - 45 * _DAY))
    result = detect_large_logs(index, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert result == []


def test_log_like_name_without_log_extension_still_matches(index: ScanIndex) -> None:
    _seed(
        index,
        _record("C:/App/access_log_2024.txt", size_bytes=_50MB + 1, mtime=_NOW - 45 * _DAY),
    )
    result = detect_large_logs(index, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert Path("C:/App/access_log_2024.txt") in _paths(result)


# --- Nested-candidate suppression -------------------------------------------------------------


def _raw(path: str, *, is_dir: bool, category: str = "test_category") -> RawCandidate:
    return RawCandidate(
        path=Path(path),
        is_dir=is_dir,
        category=category,
        category_group="dev_artifacts",
        suggested_tier=Tier.A,
        rationale="test",
    )


# --- Same-path duplicate suppression (ADR-0004) -------------------------------------------


def test_dedupe_by_path_keeps_first_occurrence() -> None:
    raw = [
        _raw("C:/Dumps/dump.dmp", is_dir=False, category="crash_dump_file"),
        _raw("C:/Dumps/dump.dmp", is_dir=False, category="crash_dump_wer_report"),
        _raw("C:/Dumps/other.dmp", is_dir=False, category="crash_dump_file"),
    ]
    deduped = _dedupe_by_path(raw)
    assert len(deduped) == 2
    assert {c.path for c in deduped} == {Path("C:/Dumps/dump.dmp"), Path("C:/Dumps/other.dmp")}
    kept = next(c for c in deduped if c.path == Path("C:/Dumps/dump.dmp"))
    assert kept.category == "crash_dump_file"  # first-seen wins


def test_dedupe_by_path_is_a_no_op_when_every_path_is_unique() -> None:
    raw = [_raw("C:/a", is_dir=False), _raw("C:/b", is_dir=False)]
    assert _dedupe_by_path(raw) == raw


def test_crash_dump_file_directly_under_root_is_never_proposed_twice(index: ScanIndex) -> None:
    """End-to-end regression for the real-disk finding: a `.dmp` file that is BOTH matched by
    extension AND sits directly under a configured CrashDumps root must survive
    `generate_candidates` exactly once, not as two separate crash_dump_file/
    crash_dump_wer_report candidates for the same path."""
    root = "C:/Users/gg/AppData/Local/CrashDumps"
    dump_path = f"{root}/app.exe.1234.dmp"
    _seed(index, _record(root, is_dir=True), _record(dump_path, size_bytes=4096))

    result = detect_crash_dumps(index, [root])
    matching = [c for c in result if c.path == Path(dump_path)]
    assert len(matching) == 2  # detect_crash_dumps itself still proposes both raw candidates...

    config = Config(
        safety=SafetyConfig(protected_roots=[]),
        categories=CategoriesConfig(crash_dumps=CrashDumpsConfig(enabled=True, paths=[root])),
    )
    candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)
    surviving = [c for c in candidates if c.path == Path(dump_path)]
    assert len(surviving) == 1  # ...but exactly one survives generate_candidates's central dedup
    assert surviving[0].category == "crash_dump_file"


def test_drop_nested_candidates_removes_descendants_of_kept_directory() -> None:
    raw = [
        _raw("C:/Proj/node_modules", is_dir=True),
        _raw("C:/Proj/node_modules/.bin/node_modules", is_dir=True),
        _raw("C:/Proj/node_modules/pkg/big.log", is_dir=False),
    ]
    kept = _drop_nested_candidates(raw)
    assert {c.path for c in kept} == {Path("C:/Proj/node_modules")}


def test_drop_nested_candidates_keeps_unrelated_siblings() -> None:
    raw = [
        _raw("C:/Proj/node_modules", is_dir=True),
        _raw("C:/Proj/__pycache__", is_dir=True),
    ]
    kept = _drop_nested_candidates(raw)
    assert {c.path for c in kept} == {Path("C:/Proj/node_modules"), Path("C:/Proj/__pycache__")}


# --- Category-group -> config enable-flag mapping ---------------------------------------------


def test_category_enabled_reflects_config_flags() -> None:
    # P0-2 (2026-08 audit): `dev_artifacts` is now default-ON (see `config.CategoriesConfig`'s
    # docstring), `old_installers` stays default-OFF -- assert both directions so this test keeps
    # proving `_category_enabled` reads the real flag rather than hardcoding one fixed value.
    config = Config()
    assert _category_enabled("dev_artifacts", config) is True
    assert _category_enabled("old_installers", config) is False

    explicitly_disabled = config.model_copy(
        update={
            "categories": config.categories.model_copy(
                update={
                    "dev_artifacts": config.categories.dev_artifacts.model_copy(
                        update={"enabled": False}
                    )
                }
            )
        }
    )
    assert _category_enabled("dev_artifacts", explicitly_disabled) is False


def test_category_enabled_rejects_unknown_group() -> None:
    with pytest.raises(ValueError, match="unknown candidate category_group"):
        _category_enabled("not_a_real_category", Config())


# --- Category-group -> config retention_days mapping (ADR-0001) -------------------------------


def test_category_retention_days_reflects_config_defaults() -> None:
    """Mirrors ADR-0001's default table: direct-delete (`None`) for dev_artifacts,
    package_caches, temp_and_browser_caches, crash_dumps; 30-day vaulted retention for
    old_installers, archive_pairs, large_logs (duplicates' default lives in dedup.py, not
    detectors.py's getter table). ADR-0003 adds model_caches at 30-day vaulted retention too —
    the one cache category that does NOT default to direct-delete, unlike its siblings."""
    config = Config()
    assert _category_retention_days("dev_artifacts", config) is None
    assert _category_retention_days("package_caches", config) is None
    assert _category_retention_days("model_caches", config) == 30
    assert _category_retention_days("temp_and_browser_caches", config) is None
    assert _category_retention_days("crash_dumps", config) is None
    assert _category_retention_days("old_installers", config) == 30
    assert _category_retention_days("archive_pairs", config) == 30
    assert _category_retention_days("large_logs", config) == 30


def test_category_retention_days_reflects_explicit_override() -> None:
    config = Config(
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=14),
            archive_pairs=ArchivePairsConfig(retention_days=None),
        )
    )
    assert _category_retention_days("dev_artifacts", config) == 14
    assert _category_retention_days("archive_pairs", config) is None


def test_category_retention_days_rejects_unknown_group() -> None:
    """Mirrors `_category_enabled`'s exact error behavior — same dict-of-lambdas shape, same
    `ValueError` on an unknown group."""
    with pytest.raises(ValueError, match="unknown candidate category_group"):
        _category_retention_days("not_a_real_category", Config())


# --- Category-group -> config size_guard_exempt mapping (ADR-0003 addendum) --------------------


def test_category_size_guard_exempt_only_true_for_package_caches() -> None:
    """package_caches is the one category exempt from the executor's cost-aware size guard by
    default — every other category must resolve `False`."""
    config = Config()
    assert _category_size_guard_exempt("package_caches", config) is True
    for group in (
        "dev_artifacts",
        "model_caches",
        "temp_and_browser_caches",
        "crash_dumps",
        "old_installers",
        "archive_pairs",
        "large_logs",
    ):
        assert _category_size_guard_exempt(group, config) is False


def test_category_size_guard_exempt_reflects_explicit_override() -> None:
    config = Config(
        categories=CategoriesConfig(
            package_caches=PackageCachesConfig(size_guard_exempt=False),
        )
    )
    assert _category_size_guard_exempt("package_caches", config) is False


def test_category_size_guard_exempt_rejects_unknown_group() -> None:
    with pytest.raises(ValueError, match="unknown candidate category_group"):
        _category_size_guard_exempt("not_a_real_category", Config())


def test_generate_candidates_resolves_size_guard_exempt_for_package_cache(
    tmp_path: Path, index: ScanIndex
) -> None:
    cache_dir = tmp_path / "pip_cache"
    _seed(index, _record(cache_dir.as_posix(), is_dir=True, size_bytes=20 * 1024 * 1024 * 1024))
    config = Config(
        safety=SafetyConfig(protected_roots=[]),
        categories=CategoriesConfig(
            package_caches=PackageCachesConfig(enabled=True, paths=[cache_dir.as_posix()])
        ),
    )
    candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)
    package_candidates = [c for c in candidates if c.category_group == "package_caches"]
    assert len(package_candidates) == 1
    assert package_candidates[0].size_guard_exempt is True


@pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")
def test_generate_candidates_resolves_reclaimable_bytes_for_a_file_level_model_cache_candidate(
    tmp_path: Path, index: ScanIndex
) -> None:
    """Audit finding E1: `_reclaimable_bytes_for_candidate`'s single-file branch (an individual
    model-weight file matched by extension under a configured `model_caches` root -- the OTHER
    detection surface `detect_model_caches` has besides the whole-directory sweep, see
    `test_model_cache_matches_safetensors_file_under_configured_root` above) needs its own real
    file + real hardlink, distinct from every other reclaimable-bytes test in this project
    (which all exercise the whole-DIRECTORY branch, via `evals/test_cache_reclaimable_bytes_gate.
    py`'s fixtures or the package-cache tests below)."""
    hub_root = tmp_path / "hub"
    external_dir = tmp_path / "external_env" / "site-packages"
    hub_root.mkdir(parents=True)
    external_dir.mkdir(parents=True)

    model_path = hub_root / "model.safetensors"
    model_path.write_bytes(b"m" * 4096)
    os.link(model_path, external_dir / "model.safetensors")  # real hardlink: nlink == 2

    _seed(index, _record(model_path.as_posix(), size_bytes=4096))
    config = Config(
        safety=SafetyConfig(protected_roots=[]),
        categories=CategoriesConfig(
            model_caches=ModelCachesConfig(enabled=True, paths=[hub_root.as_posix()])
        ),
    )
    candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)
    model_candidates = [c for c in candidates if c.category_group == "model_caches"]
    assert len(model_candidates) == 1
    # The sole name inside `hub_root` is one of two names on this inode -- the external copy
    # survives this candidate's own deletion, so 0 bytes are really reclaimable, exactly like
    # `exact_duplicate`'s existing "hardlink to a surviving copy" case.
    assert model_candidates[0].reclaimable_bytes == 0
    assert model_candidates[0].size_bytes == 4096


def test_generate_candidates_resolves_rebuildable_flag(tmp_path: Path, index: ScanIndex) -> None:
    """ADR-0005: dev_artifacts/package_caches/temp_and_browser_caches/crash_dumps candidates
    resolve `rebuildable=True`; everything else (model_caches here) resolves `False`."""
    pycache_dir = tmp_path / "proj" / "__pycache__"
    model_dir = tmp_path / "hf_hub"
    _seed(
        index,
        _record(pycache_dir.as_posix(), is_dir=True),
        _record(model_dir.as_posix(), is_dir=True, size_bytes=5 * 1024 * 1024 * 1024),
    )
    config = Config(
        safety=SafetyConfig(protected_roots=[]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True),
            model_caches=ModelCachesConfig(enabled=True, paths=[model_dir.as_posix()]),
        ),
    )
    candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)
    by_group = {c.category_group: c for c in candidates}
    assert by_group["dev_artifacts"].rebuildable is True
    assert by_group["model_caches"].rebuildable is False
