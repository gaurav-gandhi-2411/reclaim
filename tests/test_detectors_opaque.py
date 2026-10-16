from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from test_restat_decision_points import _stale_listing

import reclaim.detectors as detectors_module
from reclaim.detectors import (
    _reclaimable_bytes_for_candidate,
    detect_dev_artifacts,
    detect_temp_and_browser_caches,
)
from reclaim.index import ScanIndex
from reclaim.models import RawCandidate, Tier
from reclaim.scanner import scan_tree

# ADR-0042 consumers in detectors.py: the ADR-0035 decision-point recheck for a temp child that
# contains (or is) an opaque tree, and the hardlink-aware reclaimable-bytes estimate.

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

_DAY = 86400.0
_MIN_AGE_HOURS = 24.0


def _age_tree(root: Path, *, mtime: float) -> None:
    """Sets every file and directory under `root` (deepest first) to `mtime`."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (mtime, mtime))  # noqa: PTH118
        for name in dirnames:
            os.utime(os.path.join(dirpath, name), (mtime, mtime))  # noqa: PTH118
    os.utime(root, (mtime, mtime))


def _temp_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Temp/sess/{keep.txt, node_modules/pkg/a.js}; everything 30 days old."""
    temp = tmp_path / "Temp"
    nm = temp / "sess" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "a.js").write_bytes(b"a" * 40)
    (temp / "sess" / "keep.txt").write_bytes(b"k")
    _age_tree(temp, mtime=time.time() - 30 * _DAY)
    return temp, nm / "a.js"


def _proposed(index: ScanIndex, temp: Path) -> list[str]:
    found = detect_temp_and_browser_caches(
        index, [], [str(temp)], min_temp_root_age_hours=_MIN_AGE_HOURS, now=time.time()
    )
    return sorted(c.path.name for c in found)


def test_old_temp_child_with_an_untouched_opaque_tree_is_proposed(tmp_path: Path) -> None:
    temp, _ = _temp_tree(tmp_path)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        assert _proposed(index, temp) == ["sess"]


def test_a_deep_file_touched_after_the_scan_inside_an_opaque_tree_blocks_the_proposal(
    tmp_path: Path,
) -> None:
    """The ADR-0035 hazard for a file that is no longer a row: the recorded aggregate says the
    tree is 30 days old, the live tree says a file inside it was written an hour ago."""
    temp, deep = _temp_tree(tmp_path)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        recent = time.time() - 3600
        os.utime(deep, (recent, recent))  # in place: no directory mtime moves
        assert _proposed(index, temp) == []


def test_a_listing_that_lags_a_live_write_inside_an_opaque_tree_is_overruled_by_a_fresh_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan's listing reports 30 days for a file that was in fact just written (directory
    entry not refreshed -- the original ADR-0035 failure). The recheck must read the file itself."""
    temp, deep = _temp_tree(tmp_path)
    os.utime(deep, (time.time() - 60, time.time() - 60))  # genuinely fresh on disk
    _stale_listing(monkeypatch, {"a.js"}, age_days=30)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        recorded = index.get_record(temp / "sess" / "node_modules")
        assert recorded is not None and recorded.agg_newest_mtime is not None
        assert time.time() - recorded.agg_newest_mtime > 20 * _DAY  # the stale value was recorded
        monkeypatch.undo()
        assert _proposed(index, temp) == []


def test_a_temp_child_that_is_itself_an_opaque_root_is_rechecked(tmp_path: Path) -> None:
    temp = tmp_path / "Temp"
    (temp / "node_modules" / "pkg").mkdir(parents=True)
    deep = temp / "node_modules" / "pkg" / "a.js"
    deep.write_bytes(b"a")
    _age_tree(temp, mtime=time.time() - 30 * _DAY)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        assert _proposed(index, temp) == ["node_modules"]
        os.utime(deep, (time.time() - 60, time.time() - 60))
        assert _proposed(index, temp) == []


def test_an_unverifiable_opaque_tree_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp, _ = _temp_tree(tmp_path)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        monkeypatch.setattr(detectors_module, "aggregate_tree", lambda *a, **k: None)
        assert _proposed(index, temp) == []


def test_an_opaque_tree_beyond_the_recheck_budget_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp, _ = _temp_tree(tmp_path)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(temp, index, incremental=False)
        assert _proposed(index, temp) == ["sess"]
        monkeypatch.setattr(detectors_module, "_OPAQUE_RECHECK_MAX_ENTRIES", 1)
        assert _proposed(index, temp) == []


def test_dev_artifact_detector_still_proposes_an_opaque_node_modules(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "node_modules" / "p").mkdir(parents=True)
    (proj / "node_modules" / "p" / "i.js").write_bytes(b"1")
    (proj / "package.json").write_text("{}", encoding="utf-8")
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(tmp_path, index, incremental=False)
        found = detect_dev_artifacts(index)
        assert [c.path for c in found] == [proj / "node_modules"]


# --- _reclaimable_bytes_for_candidate -------------------------------------------------------


def _raw(path: Path) -> RawCandidate:
    return RawCandidate(
        path=path,
        is_dir=True,
        category="package_cache",
        category_group="package_caches",
        suggested_tier=Tier.B,
        rationale="r",
        rebuild_instruction=None,
    )


def test_reclaimable_estimate_for_a_cache_dir_without_opaque_roots_is_unchanged(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 100)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(tmp_path, index, incremental=False)
        assert _reclaimable_bytes_for_candidate(index, _raw(cache), 100) == 100
        empty = tmp_path / "emptycache"
        empty.mkdir()
        scan_tree(tmp_path, index, incremental=False)
        assert _reclaimable_bytes_for_candidate(index, _raw(empty), 0) == 0


def test_reclaimable_estimate_is_a_lower_bound_when_an_opaque_root_is_inside(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "pkgs" / "site-packages").mkdir(parents=True)
    (cache / "pkgs" / "site-packages" / "m.py").write_bytes(b"m" * 5000)
    (cache / "blob.bin").write_bytes(b"b" * 100)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(tmp_path, index, incremental=False)
        size = index.subtree_size_bytes(cache)
        assert size == 5100
        estimate = _reclaimable_bytes_for_candidate(index, _raw(cache), size)
        # Only the indexed blob is evaluated; the opaque 5000 bytes are never over-credited.
        assert estimate == 100
        assert estimate <= size


def test_reclaimable_estimate_is_not_a_misleading_zero_for_an_opaque_only_directory(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "site-packages").mkdir(parents=True)
    (cache / "site-packages" / "m.py").write_bytes(b"m" * 5000)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(tmp_path, index, incremental=False)
        # None = "no hardlink-aware estimate" -> consumers fall back to the logical size, the same
        # value every category without a link-aware estimate already reports.
        assert _reclaimable_bytes_for_candidate(index, _raw(cache), 5000) is None
