from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.config import ArchivePairsConfig, CategoriesConfig, Config, DevArtifactsConfig
from reclaim.detectors import (
    InventoryContext,
    _category_enabled,
    _category_retention_days,
    _drop_nested_candidates,
    build_inventory_context,
    detect_archive_pairs,
    detect_crash_dumps,
    detect_dev_artifacts,
    detect_large_logs,
    detect_old_installers,
    detect_package_caches,
    detect_temp_and_browser_caches,
)
from reclaim.models import FileRecord, RawCandidate, Tier

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
    )


def _ctx(*records: FileRecord) -> InventoryContext:
    return build_inventory_context(list(records))


def _paths(candidates: list[RawCandidate]) -> set[Path]:
    return {c.path for c in candidates}


# --- Dev artifacts: manifest-adjacency ------------------------------------------------------


def test_node_modules_with_adjacent_manifest_is_proposed() -> None:
    ctx = _ctx(
        _record("C:/Proj/package.json"),
        _record("C:/Proj/node_modules", is_dir=True),
        _record("C:/Proj/node_modules/pkg/index.js"),
    )
    result = detect_dev_artifacts(ctx)
    assert Path("C:/Proj/node_modules") in _paths(result)
    candidate = next(c for c in result if c.path == Path("C:/Proj/node_modules"))
    assert candidate.category == "dev_artifact_node_modules"
    assert candidate.category_group == "dev_artifacts"
    assert candidate.suggested_tier == Tier.A
    assert "package.json" in candidate.rationale
    assert candidate.rebuild_instruction is not None


def test_node_modules_without_adjacent_manifest_is_never_proposed() -> None:
    ctx = _ctx(
        _record("C:/Proj/node_modules", is_dir=True),
        _record("C:/Proj/node_modules/pkg/index.js"),
    )
    result = detect_dev_artifacts(ctx)
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
    dir_name: str, manifest_name: str
) -> None:
    ctx = _ctx(
        _record(f"C:/Proj/{manifest_name}"),
        _record(f"C:/Proj/{dir_name}", is_dir=True),
    )
    result = detect_dev_artifacts(ctx)
    assert Path(f"C:/Proj/{dir_name}") in _paths(result)


def test_pycache_needs_no_manifest() -> None:
    ctx = _ctx(_record("C:/Proj/__pycache__", is_dir=True))
    result = detect_dev_artifacts(ctx)
    assert Path("C:/Proj/__pycache__") in _paths(result)
    candidate = result[0]
    assert candidate.category == "dev_artifact_pycache"


def test_unrelated_directory_name_is_not_proposed() -> None:
    ctx = _ctx(_record("C:/Proj/src", is_dir=True))
    assert detect_dev_artifacts(ctx) == []


# --- Package caches / temp & browser caches / crash dumps ----------------------------------


def test_package_cache_matches_configured_pattern() -> None:
    ctx = _ctx(_record("C:/Users/gg/AppData/Local/pip/Cache", is_dir=True))
    result = detect_package_caches(ctx, ["C:/Users/gg/AppData/Local/pip/Cache"])
    assert Path("C:/Users/gg/AppData/Local/pip/Cache") in _paths(result)


def test_package_cache_does_not_match_unrelated_dir() -> None:
    ctx = _ctx(_record("C:/Users/gg/Documents/notes", is_dir=True))
    result = detect_package_caches(ctx, ["C:/Users/gg/AppData/Local/pip/Cache"])
    assert result == []


def test_temp_root_children_proposed_but_never_the_root_itself() -> None:
    ctx = _ctx(
        _record("C:/Users/gg/AppData/Local/Temp", is_dir=True),
        _record("C:/Users/gg/AppData/Local/Temp/scratch.tmp"),
    )
    result = detect_temp_and_browser_caches(
        ctx, cache_paths=[], temp_roots=["C:/Users/gg/AppData/Local/Temp"]
    )
    paths = _paths(result)
    assert Path("C:/Users/gg/AppData/Local/Temp/scratch.tmp") in paths
    assert Path("C:/Users/gg/AppData/Local/Temp") not in paths


def test_thumbnail_cache_is_categorized_distinctly_from_browser_cache() -> None:
    ctx = _ctx(
        _record("C:/Users/gg/AppData/Local/Microsoft/Windows/Explorer/thumbcache_256.db"),
        _record("C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache", is_dir=True),
    )
    result = detect_temp_and_browser_caches(
        ctx,
        cache_paths=[
            "*/thumbcache_*.db",
            "C:/Users/gg/AppData/Local/Google/Chrome/User Data/Default/Cache",
        ],
        temp_roots=[],
    )
    categories = {c.path.name: c.category for c in result}
    assert categories["thumbcache_256.db"] == "thumbnail_cache"
    assert categories["Cache"] == "browser_cache"


def test_crash_dump_file_detected_anywhere() -> None:
    ctx = _ctx(_record("C:/Users/gg/Desktop/app.dmp"))
    result = detect_crash_dumps(ctx, root_paths=[])
    assert Path("C:/Users/gg/Desktop/app.dmp") in _paths(result)


def test_wer_root_children_proposed_but_never_the_root_itself() -> None:
    ctx = _ctx(
        _record("C:/ProgramData/Microsoft/Windows/WER", is_dir=True),
        _record("C:/ProgramData/Microsoft/Windows/WER/ReportQueue", is_dir=True),
    )
    result = detect_crash_dumps(ctx, root_paths=["C:/ProgramData/Microsoft/Windows/WER"])
    paths = _paths(result)
    assert Path("C:/ProgramData/Microsoft/Windows/WER/ReportQueue") in paths
    assert Path("C:/ProgramData/Microsoft/Windows/WER") not in paths


# --- Old installers: age threshold ----------------------------------------------------------


def test_old_installer_past_threshold_is_proposed() -> None:
    ctx = _ctx(_record("C:/Users/gg/Downloads/setup.exe", mtime=_NOW - 120 * _DAY))
    result = detect_old_installers(ctx, max_age_days=90, now=_NOW)
    assert Path("C:/Users/gg/Downloads/setup.exe") in _paths(result)
    assert result[0].suggested_tier == Tier.A  # uniform detector-level suggestion


def test_recent_installer_under_threshold_is_never_proposed() -> None:
    ctx = _ctx(_record("C:/Users/gg/Downloads/setup.exe", mtime=_NOW - 10 * _DAY))
    result = detect_old_installers(ctx, max_age_days=90, now=_NOW)
    assert result == []


def test_installer_outside_downloads_is_never_proposed() -> None:
    ctx = _ctx(_record("C:/Users/gg/Desktop/setup.exe", mtime=_NOW - 400 * _DAY))
    result = detect_old_installers(ctx, max_age_days=90, now=_NOW)
    assert result == []


def test_non_installer_extension_in_downloads_is_never_proposed() -> None:
    ctx = _ctx(_record("C:/Users/gg/Downloads/report.pdf", mtime=_NOW - 400 * _DAY))
    result = detect_old_installers(ctx, max_age_days=90, now=_NOW)
    assert result == []


# --- Archive pairs: overlap threshold --------------------------------------------------------


def test_archive_with_matching_extracted_dir_proposes_only_the_archive() -> None:
    ctx = _ctx(
        _record("C:/Data/photos.zip"),
        _record("C:/Data/photos", is_dir=True),
        _record("C:/Data/photos/img1.jpg"),
    )
    result = detect_archive_pairs(ctx)
    paths = _paths(result)
    assert Path("C:/Data/photos.zip") in paths
    assert Path("C:/Data/photos") not in paths
    assert Path("C:/Data/photos/img1.jpg") not in paths
    assert "extracted copy is being kept" in result[0].rationale


def test_tar_gz_compound_suffix_is_stripped_before_matching() -> None:
    ctx = _ctx(
        _record("C:/Data/backup.tar.gz"),
        _record("C:/Data/backup", is_dir=True),
    )
    result = detect_archive_pairs(ctx)
    assert Path("C:/Data/backup.tar.gz") in _paths(result)


def test_archive_with_low_overlap_sibling_is_not_proposed() -> None:
    ctx = _ctx(
        _record("C:/Data/photos.zip"),
        _record("C:/Data/unrelated_stuff", is_dir=True),
    )
    result = detect_archive_pairs(ctx)
    assert result == []


def test_archive_with_no_sibling_directory_is_not_proposed() -> None:
    ctx = _ctx(_record("C:/Data/photos.zip"))
    result = detect_archive_pairs(ctx)
    assert result == []


# --- Large logs: size and age thresholds -----------------------------------------------------

_50MB = 50 * 1024 * 1024


def test_large_old_log_is_proposed() -> None:
    ctx = _ctx(_record("C:/App/logs/app.log", size_bytes=_50MB + 1, mtime=_NOW - 45 * _DAY))
    result = detect_large_logs(ctx, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert Path("C:/App/logs/app.log") in _paths(result)


def test_large_recent_log_is_not_proposed() -> None:
    ctx = _ctx(_record("C:/App/logs/app.log", size_bytes=_50MB + 1, mtime=_NOW - 2 * _DAY))
    result = detect_large_logs(ctx, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert result == []


def test_small_old_log_is_not_proposed() -> None:
    ctx = _ctx(_record("C:/App/logs/app.log", size_bytes=1024, mtime=_NOW - 45 * _DAY))
    result = detect_large_logs(ctx, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert result == []


def test_log_like_name_without_log_extension_still_matches() -> None:
    ctx = _ctx(_record("C:/App/access_log_2024.txt", size_bytes=_50MB + 1, mtime=_NOW - 45 * _DAY))
    result = detect_large_logs(ctx, min_size_bytes=_50MB, stale_days=30, now=_NOW)
    assert Path("C:/App/access_log_2024.txt") in _paths(result)


# --- Nested-candidate suppression -------------------------------------------------------------


def _raw(path: str, *, is_dir: bool) -> RawCandidate:
    return RawCandidate(
        path=Path(path),
        is_dir=is_dir,
        category="test_category",
        category_group="dev_artifacts",
        suggested_tier=Tier.A,
        rationale="test",
    )


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
    config = Config()
    assert _category_enabled("dev_artifacts", config) is False
    assert _category_enabled("old_installers", config) is False


def test_category_enabled_rejects_unknown_group() -> None:
    with pytest.raises(ValueError, match="unknown candidate category_group"):
        _category_enabled("not_a_real_category", Config())


# --- Category-group -> config retention_days mapping (ADR-0001) -------------------------------


def test_category_retention_days_reflects_config_defaults() -> None:
    """Mirrors ADR-0001's default table: direct-delete (`None`) for dev_artifacts,
    package_caches, temp_and_browser_caches, crash_dumps; 30-day vaulted retention for
    old_installers, archive_pairs, large_logs (duplicates' default lives in dedup.py, not
    detectors.py's getter table)."""
    config = Config()
    assert _category_retention_days("dev_artifacts", config) is None
    assert _category_retention_days("package_caches", config) is None
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
