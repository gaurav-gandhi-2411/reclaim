from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from reclaim.config import (
    CategoriesConfig,
    Config,
    ModelCachesConfig,
    PackageCachesConfig,
    SafetyConfig,
)
from reclaim.detectors import generate_candidates
from reclaim.index import ScanIndex
from reclaim.models import FileRecord, Mode
from reclaim.safety import SafetyValidator

# Audit finding E1 / H4: regression gate proving `detectors._reclaimable_bytes_for_candidate`
# (the H1 fix wiring `linkinfo.estimate_reclaimable_bytes` into every `package_caches`/
# `model_caches` candidate) is correct for each of the six real cache tools named in the audit,
# using REAL `os.link()`-created hardlinks (never a mocked `st_nlink`) -- same style as
# `tests/test_linkinfo.py`'s own real-hardlink fixtures, one level up at the full
# `generate_candidates` pipeline instead of the bare `linkinfo` primitive.
#
# pnpm is not installed on the machine this suite runs on (confirmed during the audit this gate
# closes) -- its fixture below is synthetic, built directly from pnpm's documented store layout
# (a global content-addressable store, hardlinked out into each project's
# `node_modules/.pnpm/<pkg>/node_modules/<pkg>`), not measured against a real pnpm install. Every
# other tool's directory shape mirrors this project's own `config._default_package_cache_paths`/
# `_default_model_cache_paths` real Windows locations.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="hardlink identity is Windows-specific")

_NOW = 1_700_000_000.0
_EXCLUSIVE_SIZE = 4_096
_SHARED_SIZE = 8_192


@dataclass(frozen=True)
class _CacheCase:
    case_id: str
    category_group: str  # "package_caches" | "model_caches"
    # Both relative to a per-test `tmp_path` -- `cache_subdir` is the directory
    # `generate_candidates` proposes as a whole-directory candidate; `external_subdir` is a
    # SEPARATE directory outside it standing in for a live venv/conda-env/node_modules/model
    # snapshot that keeps one of the cache's blobs hardlinked open after the cache directory
    # itself is deleted.
    cache_subdir: Path
    external_subdir: Path


_CASES: tuple[_CacheCase, ...] = (
    _CacheCase(
        "uv",
        "package_caches",
        Path("AppData/Local/uv/cache"),
        Path("myproject/.venv/Lib/site-packages/pkg_a"),
    ),
    _CacheCase(
        "pnpm",
        "package_caches",
        Path("AppData/Local/pnpm/store/v3/files"),
        Path("myproject/node_modules/.pnpm/pkg-a@1.0.0/node_modules/pkg-a"),
    ),
    _CacheCase(
        "conda_pkgs",
        "package_caches",
        Path("Anaconda3/pkgs"),
        Path("Anaconda3/envs/myenv/Lib/site-packages/pkg_a"),
    ),
    _CacheCase(
        "npm",
        "package_caches",
        Path("AppData/Local/npm-cache"),
        Path("myproject/node_modules/pkg-a"),
    ),
    _CacheCase(
        "pip",
        "package_caches",
        Path("AppData/Local/pip/Cache"),
        Path("myproject/.venv/Lib/site-packages/pkg_a"),
    ),
    _CacheCase(
        "hf_hub",
        "model_caches",
        Path("AppData/Local/hf_home/hub"),
        Path("myproject/model_snapshot/pkg_a"),
    ),
)


def _config_for(case: _CacheCase, cache_root: Path) -> Config:
    categories = (
        CategoriesConfig(
            package_caches=PackageCachesConfig(enabled=True, paths=[cache_root.as_posix()])
        )
        if case.category_group == "package_caches"
        else CategoriesConfig(
            model_caches=ModelCachesConfig(enabled=True, paths=[cache_root.as_posix()])
        )
    )
    return Config(
        mode=Mode.POWER,  # power mode: category-enabled state actually reaches Tier A, matching
        # a real user's applied config rather than SAFE mode's blanket Tier-B override --
        # irrelevant to `reclaimable_bytes` itself, kept for realism/consistency with the other
        # tmp_path-backed `generate_candidates` tests in tests/test_detectors.py.
        safety=SafetyConfig(protected_roots=[]),
        categories=categories,
    )


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_cache_reclaimable_bytes_matches_real_hardlink_sharing(
    tmp_path: Path, case: _CacheCase
) -> None:
    """For each of the six audited cache tools: a cache root containing one file exclusive to
    the cache (no other name anywhere) and one file real-hardlinked out to a location OUTSIDE
    the cache root (a live venv/env/node_modules/model-snapshot copy) -- the exact ADR-0006
    uv-cache-to-venv shape, reproduced for this tool's own real directory layout.

    Asserts the naive logical total is unchanged (still the full 2-file sum -- `size_bytes`
    never lies about what's logically in the directory) while `reclaimable_bytes` counts ONLY
    the exclusive file: the shared file's blocks stay allocated by the external hardlink even
    after this whole cache directory is deleted, so 0 of its bytes are real reclaim.
    """
    cache_root = tmp_path / case.cache_subdir
    external_dir = tmp_path / case.external_subdir
    cache_root.mkdir(parents=True)
    external_dir.mkdir(parents=True)

    exclusive_path = cache_root / "exclusive_blob.bin"
    exclusive_path.write_bytes(b"e" * _EXCLUSIVE_SIZE)

    shared_in_cache = cache_root / "shared_blob.bin"
    shared_in_cache.write_bytes(b"s" * _SHARED_SIZE)
    shared_external = external_dir / "shared_blob.bin"
    os.link(shared_in_cache, shared_external)  # real hardlink: nlink == 2

    db_path = tmp_path / "index.sqlite3"
    with ScanIndex(db_path) as index:
        index.upsert_records(
            [
                FileRecord(
                    path=cache_root,
                    is_dir=True,
                    size_bytes=0,
                    attributes=0,
                    ext="",
                    git_repo_root=None,
                    git_repo_clean=False,
                ),
                FileRecord(
                    path=exclusive_path,
                    is_dir=False,
                    size_bytes=_EXCLUSIVE_SIZE,
                    attributes=0,
                    ext=".bin",
                    git_repo_root=None,
                    git_repo_clean=False,
                ),
                FileRecord(
                    path=shared_in_cache,
                    is_dir=False,
                    size_bytes=_SHARED_SIZE,
                    attributes=0,
                    ext=".bin",
                    git_repo_root=None,
                    git_repo_clean=False,
                ),
            ],
            scanned_at=_NOW,
        )

        # Sanity: the index's own subtree scope for this candidate really does see just the two
        # cache-internal files, not the external copy -- otherwise the assertions below would be
        # proving nothing about the actual `under=cache_root` boundary the fix relies on.
        indexed_under_cache = {
            record.path
            for record in index.candidate_inventory(under=cache_root)
            if not record.is_dir
        }
        assert indexed_under_cache == {exclusive_path, shared_in_cache}

        config = _config_for(case, cache_root)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)

    matches = [c for c in candidates if c.path == cache_root]
    assert len(matches) == 1, f"expected exactly one candidate for {cache_root}, got {matches}"
    candidate = matches[0]
    assert candidate.category_group == case.category_group

    naive_total = _EXCLUSIVE_SIZE + _SHARED_SIZE
    assert candidate.size_bytes == naive_total  # naive logical total is unchanged by the fix

    assert candidate.reclaimable_bytes == _EXCLUSIVE_SIZE  # only the exclusive file is real
    overclaim_fraction = (candidate.size_bytes - candidate.reclaimable_bytes) / candidate.size_bytes
    assert overclaim_fraction == pytest.approx(_SHARED_SIZE / naive_total)


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_cache_reclaimable_bytes_equals_size_bytes_when_nothing_is_shared(
    tmp_path: Path, case: _CacheCase
) -> None:
    """The complementary case per tool: no external hardlink at all -- every byte in the cache
    directory really is exclusive to it, so `reclaimable_bytes` must equal the full logical
    `size_bytes`, not conservatively under-report just because the link-aware path now exists.
    (This is the real, measured shape this session found for the Hugging Face hub cache: 100%
    nlink==1, 0% overclaim -- see the H3 measurement table for the real numbers.)"""
    cache_root = tmp_path / case.cache_subdir
    cache_root.mkdir(parents=True)

    only_file = cache_root / "exclusive_blob.bin"
    only_file.write_bytes(b"e" * _EXCLUSIVE_SIZE)

    db_path = tmp_path / "index.sqlite3"
    with ScanIndex(db_path) as index:
        index.upsert_records(
            [
                FileRecord(
                    path=cache_root,
                    is_dir=True,
                    size_bytes=0,
                    attributes=0,
                    ext="",
                    git_repo_root=None,
                    git_repo_clean=False,
                ),
                FileRecord(
                    path=only_file,
                    is_dir=False,
                    size_bytes=_EXCLUSIVE_SIZE,
                    attributes=0,
                    ext=".bin",
                    git_repo_root=None,
                    git_repo_clean=False,
                ),
            ],
            scanned_at=_NOW,
        )
        config = _config_for(case, cache_root)
        candidates = generate_candidates(index, config, SafetyValidator(config), now=_NOW)

    candidate = next(c for c in candidates if c.path == cache_root)
    assert candidate.size_bytes == _EXCLUSIVE_SIZE
    assert candidate.reclaimable_bytes == _EXCLUSIVE_SIZE
