from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path

import pytest

import reclaim.reconciliation as reconciliation_module
from reclaim.index import InaccessibleEntry, ScanIndex
from reclaim.models import FileRecord
from reclaim.reconciliation import (
    NotAVolumeRootError,
    compute_disk_reconciliation,
    is_volume_root,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only drive-letter paths")

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _record(path: str, *, size_bytes: int = 1024, dev: int = 0, ino: int = 0) -> FileRecord:
    return FileRecord(
        path=Path(path),
        is_dir=False,
        size_bytes=size_bytes,
        attributes=0,
        ext=Path(path).suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
        dev=dev,
        ino=ino,
    )


def test_is_volume_root_true_only_for_a_bare_drive_root() -> None:
    assert is_volume_root(Path("C:/")) is True
    assert is_volume_root(Path("C:/Users")) is False
    assert is_volume_root(Path("C:/Users/gaura")) is False


def test_compute_disk_reconciliation_rejects_a_non_volume_root(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "index.sqlite3") as index, pytest.raises(NotAVolumeRootError):
        compute_disk_reconciliation(index, tmp_path)


def test_compute_disk_reconciliation_computes_delta_and_pct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end math check with a real `ScanIndex` and a fake `shutil.disk_usage` -- pins the
    exact arithmetic (`reported_total_bytes = indexed + known-inaccessible`,
    `delta = used - reported_total`, `delta_pct = delta / used * 100`) against hand-computed
    numbers, and confirms `inaccessible_known_bytes` is genuinely folded INTO
    `reported_total_bytes` (not just reported alongside it)."""
    volume = Path("C:/")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        index.upsert_records(
            [_record("C:/a.txt", size_bytes=1000, dev=1, ino=1)], scanned_at=1000.0
        )
        index.replace_inaccessible_under_root(
            volume,
            [
                InaccessibleEntry(
                    path="C:/blocked1",
                    error="denied",
                    size_estimate_bytes=200,
                    size_estimate_is_lower_bound=True,
                ),
                InaccessibleEntry(
                    path="C:/blocked2",
                    error="denied",
                    size_estimate_bytes=None,
                    size_estimate_is_lower_bound=False,
                ),
            ],
            scanned_at=1000.0,
        )

        def fake_disk_usage(_path: object) -> _DiskUsage:
            # Real volume usage is 2000 bytes; indexed (1000) + known-inaccessible (200) =
            # 1200 reported -- an 800-byte gap, part of which (the unknown-size `blocked2`
            # entry) is honestly attributable, per `inaccessible_unknown_count`.
            return _DiskUsage(total=10_000, used=2000, free=8000)

        monkeypatch.setattr(reconciliation_module.shutil, "disk_usage", fake_disk_usage)

        report = compute_disk_reconciliation(index, volume)

    assert report.indexed_bytes == 1000
    assert report.inaccessible_known_bytes == 200
    assert report.inaccessible_path_count == 2
    assert report.inaccessible_unknown_count == 1
    assert report.reported_total_bytes == 1200
    assert report.volume_used_bytes == 2000
    assert report.delta_bytes == 800
    assert report.delta_pct == pytest.approx(40.0)


def test_compute_disk_reconciliation_zero_used_bytes_yields_zero_pct_not_a_division_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = Path("C:/")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        monkeypatch.setattr(
            reconciliation_module.shutil,
            "disk_usage",
            lambda _path: _DiskUsage(total=0, used=0, free=0),
        )
        report = compute_disk_reconciliation(index, volume)

    assert report.volume_used_bytes == 0
    assert report.delta_pct == 0.0
