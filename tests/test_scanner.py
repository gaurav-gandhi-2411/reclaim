from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reclaim.index import ScanIndex, logical_size_bytes, physical_size_bytes
from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT
from reclaim.scanner import is_cloud_sync_root, scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_GIT_EMAIL = "scanner-test@reclaim.test"
_GIT_NAME = "Reclaim Scanner Test"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    git_exe = shutil.which("git")
    if git_exe is None:
        pytest.skip("git not on PATH")
    return subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        [git_exe, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "--quiet"], cwd=repo_dir)
    (repo_dir / "tracked.txt").write_text("hello\n", encoding="utf-8")
    _run_git(["add", "-A"], cwd=repo_dir)
    # Identity scoped via -c to this single commit invocation only — no `git config` call at
    # all, local or global, ever writes a config file for this. See scripts/git_guard.py's
    # docstring for why this repo prefers -c over even repo-local `git config` where possible.
    _run_git(
        [
            "-c",
            f"user.email={_GIT_EMAIL}",
            "-c",
            f"user.name={_GIT_NAME}",
            "commit",
            "--quiet",
            "-m",
            "chore: baseline",
        ],
        cwd=repo_dir,
    )


# --- is_cloud_sync_root heuristic -------------------------------------------------------


def test_is_cloud_sync_root_matches_onedrive_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    onedrive_dir = tmp_path / "SomeCustomOneDriveFolderName"
    onedrive_dir.mkdir()
    monkeypatch.setenv("OneDrive", str(onedrive_dir))
    assert is_cloud_sync_root(onedrive_dir) is True
    assert is_cloud_sync_root(tmp_path) is False


def test_is_cloud_sync_root_matches_folder_name_conventions(tmp_path: Path) -> None:
    for name in ("OneDrive - Personal", "Dropbox", "Google Drive"):
        folder = tmp_path / name
        folder.mkdir()
        assert is_cloud_sync_root(folder) is True
    unrelated = tmp_path / "Documents"
    unrelated.mkdir()
    assert is_cloud_sync_root(unrelated) is False


def test_is_cloud_sync_root_matches_dropbox_marker(tmp_path: Path) -> None:
    folder = tmp_path / "MySyncedStuff"
    folder.mkdir()
    (folder / ".dropbox").write_text("marker\n", encoding="utf-8")
    assert is_cloud_sync_root(folder) is True


# --- scan_tree structural correctness ---------------------------------------------------


def test_scan_tree_builds_full_inventory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a" * 10, encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b" * 20, encoding="utf-8")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    paths = {r.path for r in inventory}
    assert root / "a.txt" in paths
    assert root / "sub" in paths
    assert root / "sub" / "b.txt" in paths
    assert stats.entries_total == len(inventory)
    assert stats.files_pruned == 0


def test_scan_tree_reparse_point_is_recorded_but_not_recursed_into(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = tmp_path / "junction_target"
    target.mkdir(parents=True)
    (target / "should_not_appear.txt").write_text("secret", encoding="utf-8")
    root.mkdir()
    link = root / "link_to_target"

    result = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 -- cmd is a builtin
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {result.stderr or result.stdout}")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    by_path = {r.path: r for r in inventory}
    assert link in by_path
    assert by_path[link].attributes & FILE_ATTRIBUTE_REPARSE_POINT
    assert all("should_not_appear.txt" not in str(p) for p in by_path)


def test_scan_tree_hardlink_physical_size_counted_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    original = root / "a.txt"
    original.write_bytes(b"x" * 1000)
    hardlink = root / "b_hardlink.txt"
    os.link(original, hardlink)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    files_only = [r for r in inventory if not r.is_dir]
    assert logical_size_bytes(files_only) == 2000
    assert physical_size_bytes(files_only) == 1000


def test_scan_tree_detects_clean_git_repo(tmp_path: Path) -> None:
    root = tmp_path / "root"
    repo = root / "myrepo"
    _init_repo(repo)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    by_path = {r.path: r for r in inventory}
    tracked = by_path[repo / "tracked.txt"]
    assert tracked.git_repo_root == repo
    assert tracked.git_repo_clean is True

    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(root, index, incremental=False)
        inventory = index.full_inventory(under=root)
    by_path = {r.path: r for r in inventory}
    assert by_path[repo / "tracked.txt"].git_repo_clean is False


def test_scan_tree_incremental_rescan_skips_unchanged_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("stable", encoding="utf-8")

    db_path = tmp_path / "index.sqlite3"
    with ScanIndex(db_path) as index:
        first = scan_tree(root, index)
        second = scan_tree(root, index)

    assert first.files_written > 0
    assert second.files_written == 0
    assert second.files_unchanged == first.entries_total


def test_scan_tree_prunes_deleted_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    keep = root / "keep.txt"
    gone = root / "gone.txt"
    keep.write_text("keep", encoding="utf-8")
    gone.write_text("gone", encoding="utf-8")

    db_path = tmp_path / "index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(root, index)
        gone.unlink()
        second = scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    assert second.files_pruned == 1
    assert {r.path for r in inventory} == {keep}
