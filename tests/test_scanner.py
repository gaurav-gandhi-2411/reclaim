from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.index import ScanIndex, logical_size_bytes, physical_size_bytes
from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT
from reclaim.scanner import (
    _HEARTBEAT_INTERVAL_SECONDS,
    GitRepoCache,
    _due,
    build_record_for_path,
    count_entries_fast,
    is_cloud_sync_root,
    long_path,
    scan_tree,
)

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


# --- A5: disk-full during the index write is a clean abort, not an uncaught crash --------------


def test_scan_tree_raises_scan_disk_full_error_on_enospc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan walk itself is read-only; the only write is the index upsert at the end of
    `scan_tree`. Simulating the OS raising ENOSPC there must abort the scan with a distinct,
    catchable `ScanDiskFullError` rather than an uncaught `OSError` traceback."""
    import errno

    from reclaim.index import ScanIndex as ScanIndexClass
    from reclaim.scanner import ScanDiskFullError

    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    def fake_upsert(self: object, records: object, *, scanned_at: float) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ScanIndexClass, "upsert_records", fake_upsert)

    with ScanIndex(tmp_path / "index.sqlite3") as index, pytest.raises(ScanDiskFullError):
        scan_tree(root, index)


def test_scan_tree_raises_scan_disk_full_error_on_sqlite_disk_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite itself intercepts the OS write failure and reports it as
    `sqlite3.OperationalError: database or disk is full` (no errno) rather than letting a raw
    OSError through -- that's the realistic shape this fix must also catch, not just a synthetic
    OSError."""
    import sqlite3

    from reclaim.index import ScanIndex as ScanIndexClass
    from reclaim.scanner import ScanDiskFullError

    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    def fake_upsert(self: object, records: object, *, scanned_at: float) -> int:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(ScanIndexClass, "upsert_records", fake_upsert)

    with ScanIndex(tmp_path / "index.sqlite3") as index, pytest.raises(ScanDiskFullError):
        scan_tree(root, index)


def test_scan_tree_does_not_swallow_unrelated_write_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a genuine disk-full condition gets the friendly `ScanDiskFullError` treatment -- any
    other write failure must still propagate as itself, not be misreported as disk-full."""
    import errno

    from reclaim.index import ScanIndex as ScanIndexClass

    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    def fake_upsert(self: object, records: object, *, scanned_at: float) -> int:
        raise OSError(errno.EIO, "some other I/O error")

    monkeypatch.setattr(ScanIndexClass, "upsert_records", fake_upsert)

    with ScanIndex(tmp_path / "index.sqlite3") as index, pytest.raises(OSError, match="I/O error"):
        scan_tree(root, index)


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


def test_git_repo_cache_finds_repo_root_past_max_path(tmp_path: Path) -> None:
    r"""D12 follow-up: `GitRepoCache.repo_root_for`'s upward `.git`-directory walk used
    `Path.is_dir()`, which silently returns `False` past Windows' 260-char MAX_PATH (it never
    raises, it just never touches the filesystem) — the exact same failure *shape* `build_record`
    had before this fix, just reached via a different call. Left unfixed, a file inside a git
    repo whose ROOT itself sits past MAX_PATH would get `git_repo_root=None`, silently bypassing
    `safety.py`'s in-repo deletion protection (`_builtin_deny` only blocks a candidate when
    `record.git_repo_root is not None`). The repo root itself must be deep enough that the `.git`
    check at the root is past the limit, not just a file nested somewhere below a shallow root.

    Unit-level against `GitRepoCache` directly (a plain `.git` DIRECTORY marker, not a real git
    repo/commit) rather than via `scan_tree` + a real `git init` subprocess: attempting the
    latter surfaced a separate, real Windows constraint worth noting — `subprocess.run(cwd=...)`
    cannot use a `\\?\`-prefixed `cwd` at all (`CreateProcess` rejects it outright with
    `NotADirectoryError: [WinError 267]`, confirmed empirically), so a real git subprocess cannot
    be invoked with a working directory past MAX_PATH without system-wide `LongPathsEnabled` —
    orthogonal to this fix, which only needed `GitRepoCache`'s OWN directory check to be
    long-path-safe, not `git.exe` itself.
    """
    root = _make_deep_tree(tmp_path / "root", depth=12, segment_len=20)
    os.makedirs(long_path(root / ".git"), exist_ok=True)  # noqa: PTH103 -- \\?\ str, not Path

    found = GitRepoCache().repo_root_for(root)

    assert found == root


def test_build_record_for_path_fails_closed_on_8dot3_short_name_input(tmp_path: Path) -> None:
    r"""D13 second pass (audit brief item 4): does `build_record_for_path` ever construct a
    `FileRecord.path` from a caller-supplied 8.3 SHORT-name form (`C:\PROGRA~1\...`), which would
    then reach `SafetyValidator` looking like the short alias rather than the real long name?

    No -- `build_record_for_path` re-`scandir`s the parent directory and matches by
    `entry.name == path.name` (NFC-normalized, D11); `os.scandir`'s `entry.name` on Windows is
    always populated from `WIN32_FIND_DATA.cFileName` (the LONG name), never
    `cAlternateFileName` (the short 8.3 name) — so a short-name `path.name` genuinely never
    equals any real `entry.name`, and the lookup returns `None` (fails closed: the caller
    treats `None` as "path missing", not "here is a FileRecord" — never a false ELIGIBLE). This
    means the short-name-alias gap `tests/test_safety.py::
    test_8dot3_short_name_alias_of_protected_root_denied` closes is only reachable via a path
    `SafetyValidator.evaluate()` is handed WITHOUT going through this reconstruction step (e.g. a
    hand-built `FileRecord`, or `api.service._build_user_selected_candidate`'s `Path(path_str)`
    fed DIRECTLY into `record.path` for pattern-matching -- it also calls
    `build_record_for_path`, so it inherits this exact same fail-closed behavior).

    Skips (not silently passes) if this volume has 8.3 short-name generation disabled, matching
    `tests/test_safety.py`'s own honesty discipline for this OS-configurable feature.
    """
    real_dir = tmp_path / "Long Named Directory For 8dot3 Reachability"
    real_dir.mkdir()
    (real_dir / "file.txt").write_text("hi")

    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(real_dir), buf, 260)  # type: ignore[attr-defined]
    if not n or buf.value == str(real_dir):
        pytest.skip("8.3 short-name generation is disabled on this volume (fsutil 8dot3name)")

    short_form_path = Path(buf.value)
    assert short_form_path.name != real_dir.name  # sanity: genuinely a different name string
    assert short_form_path.resolve() == real_dir.resolve()  # but the same real directory

    record = build_record_for_path(short_form_path, GitRepoCache())

    assert record is None


# --- full-drive-scan-eta: progress-heartbeat pure predicate --------------------------------


def test_due_is_false_before_the_interval_elapses() -> None:
    assert (
        _due(
            last=100.0,
            now=100.0 + _HEARTBEAT_INTERVAL_SECONDS - 0.001,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
        )
        is False
    )


def test_due_is_true_once_the_interval_elapses() -> None:
    assert (
        _due(
            last=100.0,
            now=100.0 + _HEARTBEAT_INTERVAL_SECONDS,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
        )
        is True
    )


# --- full-drive-scan-eta: count_entries_fast -------------------------------------------------


def test_count_entries_fast_matches_scan_tree_entries_total_on_a_normal_tree(
    tmp_path: Path,
) -> None:
    """The fast pre-pass's whole purpose is to predict what a real `scan_tree` call will visit
    -- if the two disagree on a plain tree with no reparse points/long paths/unreadable
    directories, the ETA this feeds is systematically wrong from the start."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "nested").mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b" * 5, encoding="utf-8")
    (root / "sub" / "nested" / "c.txt").write_text("c", encoding="utf-8")

    fast_count = count_entries_fast(root)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index)

    assert fast_count == stats.entries_total


def test_count_entries_fast_reparse_point_is_counted_but_not_recursed_into(
    tmp_path: Path,
) -> None:
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

    count = count_entries_fast(root)

    assert count == 1  # the junction itself, never `should_not_appear.txt`


def test_count_entries_fast_walks_past_max_path_without_dropping_the_subtree(
    tmp_path: Path,
) -> None:
    """D12 regression class: cross-checked against `scan_tree`'s own already-proven MAX_PATH
    handling (`test_scan_tree_walks_past_max_path_without_dropping_the_subtree`) -- if
    `count_entries_fast` silently dropped everything past MAX_PATH while `scan_tree` didn't, the
    two counts would diverge exactly on this fixture."""
    root = tmp_path / "root"
    root.mkdir()
    leaf = _make_deep_tree(root)
    payload = leaf / "payload.bin"
    with open(long_path(payload), "wb") as fh:  # noqa: PTH123 -- \\?\ str, not Path
        fh.write(b"deep-payload-past-max-path")

    fast_count = count_entries_fast(root)

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index)

    assert fast_count == stats.entries_total


def test_count_entries_fast_tolerates_an_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely unreadable subdirectory is skipped, not fatal -- the sibling entry and the
    unreadable directory's own listing entry (counted when ITS parent was listed) still count;
    only what's INSIDE the unreadable directory is missing from the total."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "readable.txt").write_text("ok", encoding="utf-8")
    blocked_dir = root / "blocked_dir"
    blocked_dir.mkdir()
    (blocked_dir / "inner.txt").write_text("hidden", encoding="utf-8")

    real_scandir = os.scandir

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if "blocked_dir" in str(path):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", fake_scandir)

    count = count_entries_fast(root)

    assert count == 2  # readable.txt + blocked_dir itself, never inner.txt


def test_count_entries_fast_reports_interval_gated_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    root = tmp_path / "root"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")

    calls: list[tuple[int, float]] = []
    count = count_entries_fast(
        root, on_progress=lambda counted, elapsed: calls.append((counted, elapsed))
    )

    assert count == 5
    assert len(calls) >= 1
    assert [c[0] for c in calls] == sorted(c[0] for c in calls)  # non-decreasing
    assert calls[-1][0] <= count
    assert all(elapsed >= 0.0 for _, elapsed in calls)


def test_count_entries_fast_never_calls_progress_when_none(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    # No on_progress passed -- must not raise, matches every other progress-hook default in
    # this codebase.
    assert count_entries_fast(root) == 1


# --- full-drive-scan-eta: scan_tree's own on_progress ----------------------------------------


def test_scan_tree_reports_interval_gated_progress_with_estimated_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    for i in range(5):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    (root / "sub" / "g.txt").write_text("y", encoding="utf-8")

    calls: list[tuple[int, int | None, float]] = []

    def on_progress(processed: int, estimated_total: int | None, elapsed: float) -> None:
        calls.append((processed, estimated_total, elapsed))

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(root, index, on_progress=on_progress, entries_estimated_total=42)

    assert len(calls) >= 1
    assert all(estimated_total == 42 for _, estimated_total, _ in calls)
    assert all(0 < processed <= stats.entries_total for processed, _, _ in calls)


def test_scan_tree_progress_defaults_to_none_estimated_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    calls: list[int | None] = []

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        scan_tree(root, index, on_progress=lambda _p, total, _e: calls.append(total))

    assert calls  # at least one heartbeat fired
    assert all(total is None for total in calls)


def test_scan_tree_progress_counter_stays_consistent_across_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_ProgressTracker` is shared across every `ThreadPoolExecutor` worker `scan_tree` fans
    out to (one per top-level directory) -- this proves the shared counter survives real
    concurrent `add()` calls from multiple worker threads without over/under-counting: the
    final aggregate `entries_total` must exactly match the fixture's known entry count, and no
    individual progress report can ever exceed it."""
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    root = tmp_path / "root"
    root.mkdir()
    num_dirs, files_per_dir = 8, 20
    for d in range(num_dirs):
        subdir = root / f"dir_{d}"
        subdir.mkdir()
        for f in range(files_per_dir):
            (subdir / f"f_{f}.txt").write_text("x", encoding="utf-8")

    calls: list[int] = []

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        stats = scan_tree(
            root,
            index,
            on_progress=lambda processed, _total, _elapsed: calls.append(processed),
            max_workers=8,
        )

    expected_total = num_dirs + num_dirs * files_per_dir  # each dir itself + its files
    assert stats.entries_total == expected_total
    assert len(calls) >= 1
    assert max(calls) <= expected_total
    assert min(calls) >= 1


def test_scan_tree_on_progress_none_is_the_default_and_never_called(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    with ScanIndex(tmp_path / "index.sqlite3") as index:
        # Every pre-existing scan_tree test in this file already calls it with no on_progress at
        # all -- this test just makes the "default None, zero behavior change" contract explicit.
        stats = scan_tree(root, index)

    assert stats.entries_total == 1


def test_build_record_for_path_matches_nfd_disk_entry_against_nfc_lookup_path(
    tmp_path: Path,
) -> None:
    """D11 regression: NTFS stores filenames as raw UTF-16 with no Unicode normalization, so a
    filename written to disk in decomposed (NFD) form -- e.g. by a macOS-authored file, or any
    tool that builds combining-character sequences instead of precomposed codepoints -- round-
    trips back from `os.scandir` byte-for-byte as NFD, even when the caller's in-memory `path`
    for that exact same file is in composed (NFC) form. Before this fix, `build_record_for_path`
    compared `entry.name == path.name` with no normalization, so this real-same-file case wrongly
    fell through the loop and returned `None` -- e.g. `executor.py`'s pre-delete safety re-check
    (ADR-0001) would treat a genuinely-existing candidate as vanished.
    """
    root = tmp_path / "root"
    root.mkdir()
    nfc_name = "café.txt"  # single precomposed U+00E9 codepoint
    nfd_name = unicodedata.normalize("NFD", nfc_name)  # "e" + combining acute accent U+0301
    assert nfc_name != nfd_name, "fixture invalid -- both forms collapsed to the same string"

    (root / nfd_name).write_text("nfd-content", encoding="utf-8")
    lookup_path = root / nfc_name  # caller only has the NFC-form path, never touched the disk name

    record = build_record_for_path(lookup_path, GitRepoCache())

    assert record is not None
    assert record.size_bytes == len(b"nfd-content")
    # entry.name is used verbatim for FileRecord.path (per this function's docstring) -- only the
    # equality check itself is normalization-scoped, so the record still reports the on-disk NFD
    # form, not the NFC lookup form.
    assert record.path.name == nfd_name
