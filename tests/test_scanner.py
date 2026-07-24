from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reclaim.index import ScanIndex, logical_size_bytes, physical_size_bytes
from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT
from reclaim.scanner import is_cloud_sync_root, long_path, scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_GIT_EMAIL = "scanner-test@reclaim.test"
_GIT_NAME = "Reclaim Scanner Test"


def _make_deep_tree(root: Path, *, depth: int = 15, segment_len: int = 20) -> Path:
    r"""Builds a directory tree whose full path comfortably exceeds Windows' 260-char MAX_PATH,
    to exercise `\\?\`-prefixed long-path handling (D12/ADR-0004). Uses `os.makedirs` on a raw
    `\\?\`-prefixed string rather than `Path.mkdir` — `pathlib.Path` doesn't reliably round-trip
    that prefix, same reasoning as `reclaim.scanner`'s own long-path helpers. Mirrors
    `tests/test_executor.py::_make_deep_tree` (ADR-0004's own fixture) — duplicated rather than
    imported so this test module doesn't take on a cross-test-module dependency."""
    current = root
    for i in range(depth):
        current = current / (f"seg_{i:03d}_" + "x" * segment_len)
        os.makedirs(long_path(current), exist_ok=True)  # noqa: PTH103
    assert len(str(current)) > 260, f"fixture path too short: {len(str(current))} chars"
    return current


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


# --- D12: long-path-safe scan walk + visible skipped/unreadable accounting ----------------------


def test_scan_tree_walks_past_max_path_without_dropping_the_subtree(tmp_path: Path) -> None:
    """The real-disk regression this fix responds to: before D12, `build_record`'s bare
    `Path.stat()` call raised `WinError 3` on the first entry past Windows' 260-char MAX_PATH,
    returned `None`, and — because a directory's `None` return means it's never pushed onto the
    walk stack — its ENTIRE subtree (every directory AND file below that point) silently never
    got visited, while the scan still reported success with a plausible-looking count. Every
    directory segment down to, and including, a real >260-char leaf must now appear in the scan
    inventory, and zero entries should be recorded as skipped."""
    root = tmp_path / "root"
    root.mkdir()
    leaf = _make_deep_tree(root)
    payload = leaf / "payload.bin"
    with open(long_path(payload), "wb") as fh:  # noqa: PTH123 -- \\?\ str, not Path
        fh.write(b"deep-payload-past-max-path")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    paths = {r.path for r in inventory}
    current = root
    for part in leaf.relative_to(root).parts:
        current = current / part
        assert current in paths, f"{current} missing from scan inventory -- subtree was dropped"
    assert payload in paths
    assert stats.skipped_unreadable_count == 0
    assert stats.skipped_unreadable_paths == ()


def test_scan_tree_reports_genuinely_unreadable_path_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine per-entry failure (simulated via monkeypatch — a real ACL-denied fixture isn't
    reliable enough across environments to assert against directly) must be visible in
    `ScanStats.skipped_unreadable_count`/`skipped_unreadable_paths`, not silently vanish the way
    every unreadable entry did before D12. The sibling `readable.txt` must still be scanned
    normally — one bad entry never aborts the rest of the directory."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "readable.txt").write_text("ok", encoding="utf-8")
    blocked = root / "blocked.txt"
    blocked.write_text("blocked", encoding="utf-8")

    real_stat = os.stat

    def fake_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if os.path.basename(str(path)) == "blocked.txt":  # noqa: PTH119 -- raw str, not Path
            raise PermissionError(13, "Access is denied", str(path))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", fake_stat)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index)
        inventory = index.full_inventory(under=root)

    paths = {r.path for r in inventory}
    assert root / "readable.txt" in paths
    assert blocked not in paths
    assert stats.skipped_unreadable_count == 1
    assert str(blocked) in stats.skipped_unreadable_paths
