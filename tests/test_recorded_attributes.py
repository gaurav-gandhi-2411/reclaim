from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import reclaim.scanner as scanner_module
from reclaim.index import ScanIndex, file_row
from reclaim.models import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_REPARSE_POINT,
    RECORDED_ATTRIBUTE_MASK,
    FileRecord,
    recorded_attributes,
)
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# The bit this Windows build (11, build 26300) adds to a directory's os.stat() attributes.
EXTRA_BIT = 0x10000000


def test_recorded_attributes_keeps_only_the_bits_reclaim_reads() -> None:
    assert RECORDED_ATTRIBUTE_MASK == 0x00400410
    for bit in (
        FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_REPARSE_POINT,
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    ):
        assert recorded_attributes(bit | EXTRA_BIT) == bit
    # hidden / system / archive / readonly and every unknown bit are dropped, the three kept
    everything = 0xFFFFFFFF
    assert recorded_attributes(everything) == RECORDED_ATTRIBUTE_MASK
    assert recorded_attributes(recorded_attributes(everything)) == RECORDED_ATTRIBUTE_MASK
    assert recorded_attributes(0) == 0


def _record(path: str, attributes: int, *, is_dir: bool = False) -> FileRecord:
    return FileRecord(
        path=Path(path),
        is_dir=is_dir,
        size_bytes=10,
        attributes=attributes,
        ext="",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=1.0,
        ctime=1.0,
        dev=1,
        ino=1,
    )


def test_an_unknown_high_bit_never_reaches_the_index_and_flags_still_classify(
    tmp_path: Path,
) -> None:
    placeholder = _record("C:/OneDrive/p.jpg", FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | EXTRA_BIT)
    junction = _record(
        "C:/j", FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT | EXTRA_BIT, is_dir=True
    )
    folder = _record("C:/d", FILE_ATTRIBUTE_DIRECTORY | EXTRA_BIT, is_dir=True)
    with ScanIndex(tmp_path / "i.db") as index:
        index.upsert_records([placeholder, junction, folder], scanned_at=1.0)
        by_name = {r.path.as_posix(): r for r in index.full_inventory()}
    assert by_name["C:/OneDrive/p.jpg"].attributes == FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    assert by_name["C:/OneDrive/p.jpg"].is_cloud_placeholder
    assert by_name["C:/j"].attributes == FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT
    assert by_name["C:/j"].is_reparse_point
    assert by_name["C:/d"].attributes == FILE_ATTRIBUTE_DIRECTORY
    assert not by_name["C:/d"].is_reparse_point and not by_name["C:/d"].is_cloud_placeholder


def test_file_row_masks_like_the_record_path() -> None:
    row = file_row(
        posix_path="c:/x/a.bin",
        name="a.bin",
        size=1,
        mtime=1.0,
        ctime=1.0,
        ext=".bin",
        attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | EXTRA_BIT | 0x20,
        dev=1,
        ino=2,
        is_dir=False,
        git_repo_root_posix=None,
        git_repo_clean=False,
        scanned_at=1.0,
    )
    assert row[5] == FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    assert row[9] == 1  # is_cloud_placeholder


class _WithExtraBit:
    """An os.stat_result stand-in whose st_file_attributes carries the extra bit."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        if name == "st_file_attributes":
            return self._real.st_file_attributes | EXTRA_BIT
        return getattr(self._real, name)


@pytest.mark.parametrize("listing", [False, True])
def test_a_scan_still_recurses_and_stores_clean_attributes_when_stat_adds_the_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "root"
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "sub" / "deeper" / "f.txt").write_text("x", encoding="utf-8")
    (root / "top.txt").write_text("y", encoding="utf-8")
    real = scanner_module._guarded_stat
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    monkeypatch.setattr(
        scanner_module, "_guarded_stat", lambda ex, path: _WithExtraBit(real(ex, path))
    )

    with ScanIndex(tmp_path / "i.db") as index:
        scan_tree(root, index, incremental=False)
        records = {r.path.name: r for r in index.full_inventory()}

    assert {"sub", "deeper", "f.txt", "top.txt"} <= set(records)  # recursion unaffected
    assert records["sub"].is_dir and records["deeper"].is_dir
    for record in records.values():
        assert record.attributes & ~RECORDED_ATTRIBUTE_MASK == 0, hex(record.attributes)
    assert records["sub"].attributes == FILE_ATTRIBUTE_DIRECTORY
    assert not records["sub"].is_reparse_point
