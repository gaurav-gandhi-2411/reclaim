from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.dirlist import ListingUnsupported
from reclaim.index import ScanIndex, _record_to_row, file_row
from reclaim.models import FileRecord
from reclaim.preflight import check_hardlink_shared_active_install
from reclaim.scanner import GitRepoCache, ScanStats, long_path, scan_tree

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)


def _build_tree(root: Path) -> None:
    """A tree exercising every shape the listing-sourced walk handles: nested directories, an
    empty file, dotfiles, unicode/space names, mixed-case and multi-dot extensions, a
    hardlinked pair, and a `.git` marker directory (repo-root resolution)."""
    root.mkdir()
    (root / "top.txt").write_text("top", encoding="utf-8")
    (root / "empty.bin").write_bytes(b"")
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.PY").write_text("print(1)", encoding="utf-8")
    (root / "a" / ".hidden").write_text("h", encoding="utf-8")
    (root / "a" / "naïve café.tar.gz").write_bytes(b"z" * 5000)
    (root / "a" / "日本語.md").write_text("md", encoding="utf-8")
    (root / "a" / "with space.JPG").write_bytes(b"j" * 100)
    (root / "empty_dir").mkdir()
    repo = root / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    original = root / "a" / "linked.bin"
    original.write_bytes(b"hardlinked content")
    os.link(original, root / "proj" / "src" / "linked_alias.bin")


def _scan(
    root: Path, db: Path, *, listing: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[FileRecord], ScanStats]:
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(db) as index:
        stats = scan_tree(root, index, incremental=False)
        return sorted(index.full_inventory(), key=lambda r: r.path.as_posix()), stats


def test_listing_walk_builds_records_identical_to_the_scandir_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core claim of the speed-up: for the same tree, the listing-sourced walk and the
    original scandir + per-file `os.stat()` walk produce exactly the same `FileRecord`s (path,
    size, attributes, ext, git repo, mtime, ctime, dev, ino, is_dir) -- row for row."""
    root = tmp_path / "root"
    _build_tree(root)

    legacy, legacy_stats = _scan(
        root, tmp_path / "legacy.db", listing=False, monkeypatch=monkeypatch
    )
    listed, listed_stats = _scan(
        root, tmp_path / "listed.db", listing=True, monkeypatch=monkeypatch
    )

    assert len(legacy) > 15
    assert listed == legacy
    assert listed_stats.entries_total == legacy_stats.entries_total
    assert listed_stats.dirs_visited == legacy_stats.dirs_visited
    assert listed_stats.skipped_unreadable_count == legacy_stats.skipped_unreadable_count == 0
    # The in-repo files really did get their repo root (not just None == None on both sides).
    in_repo = {r.path.name: r for r in listed if r.path.name == "main.py"}
    assert in_repo["main.py"].git_repo_root == root / "proj"


def test_listing_walk_keeps_hardlink_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _build_tree(root)

    listed, _stats = _scan(root, tmp_path / "x.db", listing=True, monkeypatch=monkeypatch)

    by_name = {r.path.name: r for r in listed}
    first, second = by_name["linked.bin"], by_name["linked_alias.bin"]
    assert (first.dev, first.ino) == (second.dev, second.ino)
    assert first.ino != 0
    stat = (root / "a" / "linked.bin").stat()
    assert (first.dev, first.ino) == (stat.st_dev, stat.st_ino)


def test_incremental_rescan_classifies_changes_like_the_scandir_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged files must be recognized as unchanged from the listing's (size, mtime) -- which
    requires its mtime float to be bit-identical to what the first scan stored -- and a changed
    file must be rewritten, in both walk modes with identical counts."""
    results: dict[bool, tuple[int, int]] = {}
    for listing in (False, True):
        monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
        root = tmp_path / f"root_{listing}"
        _build_tree(root)
        with ScanIndex(tmp_path / f"inc_{listing}.db") as index:
            first = scan_tree(root, index)
            unchanged = scan_tree(root, index)
            (root / "a" / "naïve café.tar.gz").write_bytes(b"changed" * 100)
            changed = scan_tree(root, index)
            record = index.get_record(root / "a" / "naïve café.tar.gz")
        assert first.files_written == first.entries_total
        assert unchanged.files_written == 0
        assert unchanged.files_unchanged == unchanged.entries_total
        assert changed.files_written >= 1
        assert record is not None
        assert record.size_bytes == 700
        results[listing] = (changed.files_written, changed.files_unchanged)

    assert results[True] == results[False]


def test_mid_directory_batch_flush_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write batch fills in the middle of one large directory (the memory-bound guarantee
    `_BatchIndexWriter` exists for): rows built without a `FileRecord` must flush and be marked
    seen exactly like records, or pruning would delete them."""
    monkeypatch.setattr(scanner_module, "_WRITE_BATCH_SIZE", 7)
    root = tmp_path / "root"
    flat = root / "flat"
    flat.mkdir(parents=True)
    names = {f"f{i:03d}.dat" for i in range(100)}
    for name in names:
        (flat / name).write_bytes(b"1")

    with ScanIndex(tmp_path / "x.db") as index:
        scan_tree(root, index)
        stats = scan_tree(root, index)  # a second, incremental pass prunes unseen rows
        present = {r.path.name for r in index.full_inventory(under=flat)}

    assert present >= names
    assert stats.files_pruned == 0


def test_unsupported_volume_falls_back_to_the_scandir_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _build_tree(root)
    legacy, _ = _scan(root, tmp_path / "legacy.db", listing=False, monkeypatch=monkeypatch)

    def unsupported(path: str, volume_serial: int | None = None) -> object:
        raise ListingUnsupported(None, "not an NTFS volume", path, 0)

    monkeypatch.setattr(scanner_module, "list_directory", unsupported)
    fallback, _ = _scan(root, tmp_path / "fallback.db", listing=True, monkeypatch=monkeypatch)

    assert fallback == legacy


def test_network_root_never_uses_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every entry under a network-mapped/UNC root is guard-stat'd (`force_guard`); the listing's
    trust is only established for local NTFS, so such a root must stay entirely on the legacy
    path."""
    root = tmp_path / "root"
    _build_tree(root)
    calls: list[str] = []

    def spy(path: str, volume_serial: int | None = None) -> object:
        calls.append(path)
        raise AssertionError("list_directory must not be used under a network root")

    monkeypatch.setattr(scanner_module, "list_directory", spy)
    monkeypatch.setattr(scanner_module, "is_network_drive", lambda _root: True)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", True)

    with ScanIndex(tmp_path / "x.db") as index:
        stats = scan_tree(root, index)

    assert calls == []
    assert stats.guarded_stat_count == stats.entries_total


def test_unreadable_directory_is_still_reported_when_the_listing_walk_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing walk's own counterpart of the scandir-failure tests in test_scanner.py: a
    directory neither the listing nor the legacy fallback can read must still become a
    `SkippedPath`/inaccessible entry, and its siblings must still be scanned."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "readable.txt").write_text("ok", encoding="utf-8")
    blocked = root / "blocked_dir"
    blocked.mkdir()
    (blocked / "inner.txt").write_text("hidden", encoding="utf-8")
    real_list, real_scandir = scanner_module.list_directory, os.scandir

    def fake_list(path: str, volume_serial: int | None = None) -> object:
        if "blocked_dir" in path:
            raise PermissionError(13, "Access is denied", path)
        return real_list(path, volume_serial)

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if "blocked_dir" in str(path):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner_module, "list_directory", fake_list)
    monkeypatch.setattr(os, "scandir", fake_scandir)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", True)

    with ScanIndex(tmp_path / "x.db") as index:
        stats = scan_tree(root, index)
        paths = {r.path for r in index.full_inventory(under=root)}
        sample = index.inaccessible_paths_sample(limit=10)

    assert stats.skipped_unreadable_count == 1
    assert root / "readable.txt" in paths
    assert blocked / "inner.txt" not in paths
    assert [s.path for s in sample] == [blocked.as_posix()]


def test_failed_directory_stat_in_the_listing_walk_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directories still go through `os.stat()` in the listing walk; a failing one must become a
    `SkippedPath` (with the best-effort size probe), never abort the walk or vanish silently."""
    root = tmp_path / "root"
    (root / "ok_dir").mkdir(parents=True)
    (root / "bad_dir").mkdir()
    (root / "ok_dir" / "f.txt").write_text("f", encoding="utf-8")
    (root / "bad_dir" / "g.txt").write_text("g", encoding="utf-8")
    real_stat = os.stat

    def fake_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if os.path.basename(str(path)) == "bad_dir":  # noqa: PTH119 -- raw str, not Path
            raise PermissionError(13, "Access is denied", str(path))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", True)

    # `scan_tree`'s own top-level loop is legacy; nest one level so `_walk_subtree` (listing)
    # is what meets `bad_dir`.
    outer = tmp_path / "outer"
    outer.mkdir()
    (root).rename(outer / "root")
    with ScanIndex(tmp_path / "x.db") as index:
        stats = scan_tree(outer, index)
        paths = {r.path for r in index.full_inventory(under=outer)}

    assert stats.skipped_unreadable_count == 1
    assert outer / "root" / "ok_dir" / "f.txt" in paths
    assert outer / "root" / "bad_dir" / "g.txt" not in paths


def test_plain_files_cost_no_stat_in_the_listing_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The speed-up itself, proved structurally rather than by wall clock: a plain file under a
    walked directory must be recorded without any `os.stat()` call naming it, while the old walk
    stats every one."""
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    for i in range(20):
        (sub / f"plain_{i}.txt").write_text("x", encoding="utf-8")
    real_stat = os.stat
    statted: list[str] = []

    def spy(path: object, *args: object, **kwargs: object) -> os.stat_result:
        statted.append(os.path.basename(str(path)))  # noqa: PTH119 -- raw str, not Path
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", spy)

    for listing, expected_plain_stats in ((False, 20), (True, 0)):
        statted.clear()
        monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
        with ScanIndex(tmp_path / f"x_{listing}.db") as index:
            scan_tree(root, index, incremental=False)
        assert sum(name.startswith("plain_") for name in statted) == expected_plain_stats


def test_volume_serial_is_read_once_per_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    _build_tree(root)
    seen_serials: list[int | None] = []
    real_list = scanner_module.list_directory

    def spy(path: str, volume_serial: int | None = None) -> object:
        seen_serials.append(volume_serial)
        return real_list(path, volume_serial)

    monkeypatch.setattr(scanner_module, "list_directory", spy)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", True)

    with ScanIndex(tmp_path / "x.db") as index:
        scan_tree(root, index, max_workers=1)

    # One walk per top-level directory (a, empty_dir, proj): the first listing of each learns the
    # serial, every later directory of that walk reuses it.
    assert seen_serials.count(None) == 3
    assert len(seen_serials) > 3
    assert {s for s in seen_serials if s is not None} == {root.stat().st_dev}


def test_junction_is_recorded_not_followed_and_matches_the_scandir_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reparse points keep the timeout-guarded `os.stat()` and are never descended into; the
    resulting record must equal the legacy walk's (attributes incl. the reparse bit, is_dir)."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").write_text("t", encoding="utf-8")
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "plain.txt").write_text("p", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(sub / "link"), str(target)],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {result.stderr or result.stdout}")

    legacy, _ = _scan(root, tmp_path / "legacy.db", listing=False, monkeypatch=monkeypatch)
    listed, stats = _scan(root, tmp_path / "listed.db", listing=True, monkeypatch=monkeypatch)

    assert listed == legacy
    link = next(r for r in listed if r.path.name == "link")
    assert link.is_reparse_point
    assert not any(r.path.name == "inside.txt" for r in listed)
    assert stats.guarded_stat_count >= 1


def test_file_row_equals_record_to_row() -> None:
    """`file_row` is the allocation-free twin of `_record_to_row`; one layout, two builders."""
    path = Path("C:/Users/x/Proj/Sub/Résumé.FINAL.PDF")
    record = FileRecord(
        path=path,
        is_dir=False,
        size_bytes=123,
        attributes=0x20,
        ext=".pdf",
        git_repo_root=Path("C:/Users/x/Proj"),
        git_repo_clean=True,
        mtime=1700000000.5,
        ctime=1600000000.25,
        dev=2**63 + 5,  # above SQLite's signed range: exercises the int64 wraparound
        ino=7,
    )

    row = file_row(
        posix_path=path.as_posix(),
        name=path.name,
        size=123,
        mtime=1700000000.5,
        ctime=1600000000.25,
        ext=".pdf",
        attributes=0x20,
        dev=2**63 + 5,
        ino=7,
        is_dir=False,
        git_repo_root_posix="C:/Users/x/Proj",
        git_repo_clean=True,
        scanned_at=42.0,
    )

    assert row == _record_to_row(record, 42.0)


def _make_venv(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = C:/Python312\n", encoding="utf-8")
    return root


def test_hardlinked_cache_file_in_a_scanned_tree_still_trips_the_active_install_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the original incident class: a package-cache file hardlinked into
    live venvs. After scanning with the listing walk, (a) the index must record the SAME
    (dev, ino) for the cache name and every venv name -- the identity the dedup/preflight logic
    keys on -- exactly as the legacy walk does, and (b) `check_hardlink_shared_active_install`
    on the cache file must return the same positive verdict under both walks (it is a live
    filesystem check, so this also pins that scanning never perturbs it)."""
    tree = tmp_path / "tree"
    cache_file = tree / "uv-cache" / "archive-v0" / "abc123" / "mypy_extensions.py"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"stdlib-ish-module-content")
    venv_roots = []
    for i in range(2):
        venv = _make_venv(tree / f"project_{i}" / ".venv")
        site_packages = venv / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        os.link(cache_file, site_packages / "mypy_extensions.py")
        venv_roots.append(venv)

    verdicts = {}
    identities = {}
    for listing in (False, True):
        records, _stats = _scan(
            tree, tmp_path / f"hl_{listing}.db", listing=listing, monkeypatch=monkeypatch
        )
        by_path = {r.path: r for r in records}
        linked = [by_path[cache_file]] + [
            by_path[v / "Lib" / "site-packages" / "mypy_extensions.py"] for v in venv_roots
        ]
        identities[listing] = [(r.dev, r.ino) for r in linked]
        assert len({(r.dev, r.ino) for r in linked}) == 1, "hardlinked names must share an id"
        result = check_hardlink_shared_active_install(cache_file)
        verdicts[listing] = (
            result.is_shared_with_other_environment,
            result.own_environment_root,
            set(result.sibling_environment_roots),
        )

    assert identities[True] == identities[False]
    assert verdicts[True] == verdicts[False] == (True, None, set(venv_roots))


def test_build_record_for_path_is_unchanged_by_the_listing_walk(tmp_path: Path) -> None:
    """The apply-time re-check (`build_record_for_path`, used by the executor's identity
    re-verification) deliberately stays on the per-file `os.stat()` path; it must keep agreeing
    with what the listing walk stored for the same file."""
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    target = sub / "f.txt"
    target.write_text("data", encoding="utf-8")
    with ScanIndex(tmp_path / "x.db") as index:
        scan_tree(root, index)
        stored = index.get_record(target)

    fresh = scanner_module.build_record_for_path(target, GitRepoCache())

    assert stored is not None
    assert fresh == stored
    assert long_path(target).startswith("\\\\?\\")
