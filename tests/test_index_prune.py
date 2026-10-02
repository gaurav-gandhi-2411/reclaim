from __future__ import annotations

import os
import shutil
import string
from collections.abc import Iterator
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.cli import main
from reclaim.index import ScanIndex
from reclaim.index_prune import prune_dead_rows
from reclaim.models import FileRecord
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="index targets Windows/NTFS only")


@pytest.fixture(autouse=True)
def _isolate_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("reclaim.cli.DEFAULT_LOG_PATH", tmp_path / "reclaim.log")


@pytest.fixture
def index(tmp_path: Path) -> Iterator[ScanIndex]:
    idx = ScanIndex(tmp_path / "index.sqlite3")
    yield idx
    idx.close()


def _tree(root: Path) -> None:
    """root/keep/{a,b}.txt, root/gone/{c.txt, sub/d.txt}, root/top.txt"""
    (root / "keep").mkdir(parents=True)
    (root / "gone" / "sub").mkdir(parents=True)
    for rel in ("keep/a.txt", "keep/b.txt", "gone/c.txt", "gone/sub/d.txt", "top.txt"):
        (root / rel).write_text("x" * 10, encoding="utf-8")


def _paths(index: ScanIndex, under: Path) -> set[str]:
    return {r.path.as_posix() for r in index.full_inventory(under=under)}


# --- rescan pruning (scan_tree) -------------------------------------------------------------


def test_rescan_removes_rows_for_deleted_files_and_directories(
    tmp_path: Path, index: ScanIndex
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    shutil.rmtree(root / "gone")
    (root / "keep" / "b.txt").unlink()

    stats = scan_tree(root, index)

    assert stats.files_pruned == 5  # gone, gone/c.txt, gone/sub, gone/sub/d.txt, keep/b.txt
    assert _paths(index, root) == {
        (root / "keep").as_posix(),
        (root / "keep" / "a.txt").as_posix(),
        (root / "top.txt").as_posix(),
    }


def test_rescan_of_one_root_never_touches_another_roots_rows(
    tmp_path: Path, index: ScanIndex
) -> None:
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _tree(r1)
    _tree(r2)
    scan_tree(r1, index)
    scan_tree(r2, index)
    shutil.rmtree(r1 / "gone")
    shutil.rmtree(r2 / "gone")  # r2 is stale on disk but r1 is the only root rescanned

    scan_tree(r1, index)

    assert (r2 / "gone" / "c.txt").as_posix() in _paths(index, r2)
    assert (r1 / "gone" / "c.txt").as_posix() not in _paths(index, r1)


def test_unreadable_directory_keeps_its_rows_but_a_deleted_sibling_is_still_pruned(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    (root / "keep" / "b.txt").unlink()  # genuinely gone, readable dir: must be pruned (teeth)

    # Legacy walk so the failure can be injected via os.scandir (the listing path bypasses it).
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", False)
    real_scandir = os.scandir

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if str(path).rstrip("\\").endswith("\\gone"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", fake_scandir)
    stats = scan_tree(root, index)

    kept = _paths(index, root)
    assert stats.skipped_unreadable_count == 1
    assert (root / "gone" / "c.txt").as_posix() in kept
    assert (root / "gone" / "sub" / "d.txt").as_posix() in kept
    assert (root / "keep" / "b.txt").as_posix() not in kept
    assert (root / "keep" / "a.txt").as_posix() in kept


def test_unreadable_root_keeps_every_row(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    before = _paths(index, root)

    real_scandir = os.scandir

    def offline(path: object, *args: object, **kwargs: object) -> object:
        if str(path).rstrip("\\").endswith("\\root"):
            raise OSError(53, "The network path was not found", str(path))  # offline share
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", offline)
    stats = scan_tree(root, index)

    assert stats.files_pruned == 0
    assert _paths(index, root) == before


def test_cancelled_scan_does_not_prune(tmp_path: Path, index: ScanIndex) -> None:
    import threading

    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    before = _paths(index, root)
    (root / "gone" / "c.txt").unlink()  # prunable on a completed scan, not on a cancelled one

    cancel = threading.Event()
    cancel.set()
    stats = scan_tree(root, index, cancel_event=cancel)

    assert stats.cancelled is True
    assert stats.files_pruned == 0
    assert _paths(index, root) == before


# --- prune_dead_rows ------------------------------------------------------------------------


def _scan_then_delete(tmp_path: Path, index: ScanIndex) -> Path:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    shutil.rmtree(root / "gone")
    return root


def test_dry_run_reports_but_deletes_nothing(tmp_path: Path, index: ScanIndex) -> None:
    root = _scan_then_delete(tmp_path, index)
    before = _paths(index, root)

    report = prune_dead_rows(index)

    assert report.applied is False
    assert report.dead_rows == 3  # gone/c.txt, gone/sub (dir row), gone/sub/d.txt
    assert report.dead_bytes == 20  # the two 10-byte files; directory rows add no bytes
    assert _paths(index, root) == before


def test_apply_deletes_exactly_the_dead_rows(tmp_path: Path, index: ScanIndex) -> None:
    root = _scan_then_delete(tmp_path, index)
    before = _paths(index, root)

    report = prune_dead_rows(index, apply=True)

    gone = (root / "gone").as_posix()
    expected_dead = {
        f"{gone}/c.txt",
        f"{gone}/sub",
        f"{gone}/sub/d.txt",
    }
    assert report.dead_rows == len(expected_dead)
    # The `gone` directory row itself lives in `root`, which still exists: only a rescan (not
    # a directory-level check) can know it is gone, so it stays. Everything else survives too.
    assert _paths(index, root) == before - expected_dead
    assert (root / "keep" / "a.txt").as_posix() in _paths(index, root)  # teeth: live dir keeps rows


def test_existing_directory_never_loses_rows_even_if_files_are_missing_without_deep(
    tmp_path: Path, index: ScanIndex
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    (root / "keep" / "b.txt").unlink()

    report = prune_dead_rows(index, apply=True)  # directory-level only: `keep` still exists

    assert report.dead_rows == 0
    assert (root / "keep" / "b.txt").as_posix() in _paths(index, root)


def test_deep_removes_entries_missing_from_an_existing_directory(
    tmp_path: Path, index: ScanIndex
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    (root / "keep" / "b.txt").unlink()

    report = prune_dead_rows(index, apply=True, deep=True)

    assert report.dead_rows == 1
    paths = _paths(index, root)
    assert (root / "keep" / "b.txt").as_posix() not in paths
    assert (root / "keep" / "a.txt").as_posix() in paths


def test_deep_keeps_rows_when_the_directory_cannot_be_listed(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    (root / "keep" / "b.txt").unlink()
    real_scandir = os.scandir

    def denied(path: object, *args: object, **kwargs: object) -> object:
        if str(path).endswith("\\keep"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", denied)

    report = prune_dead_rows(index, apply=True, deep=True)

    assert report.dead_rows == 0
    assert report.unknown_rows_kept >= 2
    assert (root / "keep" / "b.txt").as_posix() in _paths(index, root)


def test_deep_treats_a_case_only_difference_as_present(tmp_path: Path, index: ScanIndex) -> None:
    root = tmp_path / "root"
    _tree(root)
    scan_tree(root, index)
    (root / "keep" / "a.txt").rename(root / "keep" / "A.TXT")

    report = prune_dead_rows(index, apply=True, deep=True)

    assert report.dead_rows == 0


def _unused_drive_letter() -> str:
    for letter in reversed(string.ascii_uppercase):
        if not Path(f"{letter}:\\").exists():
            return letter
    pytest.skip("no free drive letter")


def test_rows_on_an_unreachable_drive_are_kept(index: ScanIndex) -> None:
    letter = _unused_drive_letter()
    records = [
        FileRecord(
            path=Path(f"{letter}:/data/file{i}.bin"),
            is_dir=False,
            size_bytes=5,
            attributes=0,
            ext=".bin",
            git_repo_root=None,
            git_repo_clean=False,
            mtime=1.0,
            ctime=1.0,
            dev=0,
            ino=0,
        )
        for i in range(3)
    ]
    index.upsert_records(records, scanned_at=1.0)

    report = prune_dead_rows(index, apply=True, deep=True)

    assert report.dead_rows == 0
    assert report.unknown_rows_kept == 3
    assert len(index.full_inventory()) == 3


def test_access_denied_directory_is_unknown_not_missing(
    tmp_path: Path, index: ScanIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _scan_then_delete(tmp_path, index)
    shutil.rmtree(root / "keep")
    real_stat = os.stat

    def stat(path: object, *args: object, **kwargs: object) -> object:
        if str(path).endswith("\\keep"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", stat)

    prune_dead_rows(index, apply=True)

    assert (root / "keep" / "a.txt").as_posix() in _paths(index, root)  # unverifiable -> kept


def test_root_prefixes_limit_the_pass(tmp_path: Path, index: ScanIndex) -> None:
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _tree(r1)
    _tree(r2)
    scan_tree(r1, index)
    scan_tree(r2, index)
    shutil.rmtree(r1 / "gone")
    shutil.rmtree(r2 / "gone")

    report = prune_dead_rows(index, apply=True, root_prefixes=[r1.as_posix()])

    assert report.dead_rows == 3
    assert (r2 / "gone" / "c.txt").as_posix() in _paths(index, r2)
    assert (r1 / "gone" / "c.txt").as_posix() not in _paths(index, r1)


# --- CLI ------------------------------------------------------------------------------------


def test_cli_index_prune_dry_run_then_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    _tree(root)
    db = tmp_path / "cli.sqlite3"
    assert main(["scan", str(root), "--db", str(db)]) == 0
    shutil.rmtree(root / "gone")
    capsys.readouterr()

    assert main(["index-prune", "--db", str(db)]) == 0
    dry = capsys.readouterr().out
    assert "would remove (dry run, use --apply) 3 of" in dry
    with ScanIndex(db) as idx:
        assert (root / "gone" / "c.txt").as_posix() in _paths(idx, root)

    assert main(["index-prune", "--db", str(db), "--apply", "--vacuum"]) == 0
    applied = capsys.readouterr().out
    assert "removed 3 of" in applied
    with ScanIndex(db) as idx:
        assert (root / "gone" / "c.txt").as_posix() not in _paths(idx, root)
        assert (root / "keep" / "a.txt").as_posix() in _paths(idx, root)


def test_cli_index_prune_vacuum_requires_apply_and_missing_db_fails(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite3"
    ScanIndex(db).close()
    assert main(["index-prune", "--db", str(db), "--vacuum"]) == 2
    assert main(["index-prune", "--db", str(tmp_path / "nope.sqlite3")]) == 1
