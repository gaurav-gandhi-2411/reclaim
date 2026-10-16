from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.index import ScanIndex
from reclaim.models import OPAQUE_DIR_NAMES, FileRecord
from reclaim.scanner import OpaqueAggregate, aggregate_tree, scan_tree

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Reclaim targets Windows/NTFS exclusively"
)

# ADR-0042: opaque dependency trees. Every scan test runs through BOTH walk paths -- the
# ID-bearing directory-listing fast path and the legacy os.scandir path -- because the aggregate
# walk and the row hooks exist in both.

_BOTH_PATHS = pytest.mark.parametrize("listing", [True, False], ids=["listing", "scandir"])


def _build_tree(root: Path) -> None:
    """A project with every opaque kind, nested opaque names, a hardlink pair inside one tree
    and ordinary files that must stay individually recorded."""
    root.mkdir()
    (root / "README.md").write_text("readme", encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("print(1)", encoding="utf-8")
    nm = root / "node_modules"
    for pkg, nbytes in (("left-pad", 100), ("react", 4000), ("@scope/inner", 30)):
        (nm / pkg / "lib").mkdir(parents=True)
        (nm / pkg / "index.js").write_bytes(b"j" * nbytes)
        (nm / pkg / "lib" / "util.js").write_bytes(b"u" * (nbytes // 2))
    (nm / "react" / "node_modules" / "dep").mkdir(parents=True)  # nested opaque name: still inside
    (nm / "react" / "node_modules" / "dep" / "x.js").write_bytes(b"x" * 7)
    os.link(nm / "react" / "index.js", nm / "left-pad" / "react_alias.js")  # hardlink in tree
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ" * 50)
    (venv / "Lib" / "site-packages" / "pkg").mkdir(parents=True)
    (venv / "Lib" / "site-packages" / "pkg" / "__init__.py").write_bytes(b"p" * 11)
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"c" * 64)
    git = root / ".git"
    (git / "objects" / "ab").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "objects" / "ab" / "cdef").write_bytes(b"g" * 90)
    sp = root / "vendor" / "lib" / "Site-Packages"  # case-insensitive name match
    sp.mkdir(parents=True)
    (sp / "mod.py").write_bytes(b"m" * 21)


def _independent_aggregate(root: Path) -> tuple[int, int, float | None, int, float]:
    """(files, bytes, newest file mtime, physical bytes, newest directory mtime incl. the root)
    via os.walk + lstat -- shares no code with the scanner's walkers."""
    files = 0
    total = 0
    physical = 0
    newest: float | None = None
    newest_dir = os.lstat(root).st_mtime
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            mtime = os.lstat(os.path.join(dirpath, name)).st_mtime  # noqa: PTH118
            newest_dir = max(newest_dir, mtime)
        for name in filenames:
            st = os.lstat(os.path.join(dirpath, name))  # noqa: PTH118
            files += 1
            total += st.st_size
            newest = st.st_mtime if newest is None or st.st_mtime > newest else newest
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                physical += st.st_size
    return files, total, newest, physical, newest_dir


def _rows(index: ScanIndex, root: Path) -> dict[str, FileRecord]:
    return {r.path.as_posix(): r for r in index.full_inventory(under=root)}


def _scan(root: Path, db: Path, monkeypatch: pytest.MonkeyPatch, listing: bool) -> ScanIndex:
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    index = ScanIndex(db)
    scan_tree(root, index, incremental=False)
    return index


def _make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed test args
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 -- cmd builtin
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {result.stderr or result.stdout}")


@_BOTH_PATHS
def test_each_opaque_root_is_one_row_with_independently_verified_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        rows = _rows(index, root)
        opaque_paths = sorted(p for p, r in rows.items() if r.opaque)
        expected_roots = sorted(
            (root / rel).as_posix()
            for rel in (
                "node_modules",
                ".venv",
                "src/__pycache__",
                ".git",
                "vendor/lib/Site-Packages",
            )
        )
        assert opaque_paths == expected_roots
        # No row at all below an opaque root: exactly one row per root.
        for opaque_path in opaque_paths:
            assert [p for p in rows if p.startswith(opaque_path + "/")] == []
            assert rows[opaque_path].is_dir is True
        # Ordinary files and directories stay individually recorded.
        for plain in ("README.md", "src", "src/app.py", "vendor", "vendor/lib"):
            assert (root / plain).as_posix() in rows
            assert rows[(root / plain).as_posix()].opaque is False

        for opaque_path in opaque_paths:
            record = rows[opaque_path]
            files, nbytes, newest, physical, newest_dir = _independent_aggregate(Path(opaque_path))
            assert (record.agg_files, record.agg_bytes) == (files, nbytes), opaque_path
            assert record.agg_newest_mtime == newest, opaque_path
            assert record.agg_newest_dir_mtime == newest_dir, opaque_path
            assert record.agg_physical_bytes == physical, opaque_path

        nm = rows[(root / "node_modules").as_posix()]
        assert nm.agg_physical_bytes < nm.agg_bytes  # the in-tree hardlink is counted once


@_BOTH_PATHS
def test_consumers_see_the_aggregates_after_a_real_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        nm = root / "node_modules"
        files, nbytes, newest, _, newest_dir = _independent_aggregate(nm)
        assert index.subtree_size_bytes(nm) == nbytes
        row_mtime = index.get_record(nm).mtime  # type: ignore[union-attr]
        assert index.subtree_newest_mtime(nm) == max(newest or 0.0, newest_dir, row_mtime)
        assert index.subtree_entry_count(nm) == 1 + files


@_BOTH_PATHS
def test_walk_paths_agree_on_the_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """Scan time and apply time may use different walks (listing vs scandir) on one machine:
    the live recount must equal the stored aggregate bit for bit."""
    del monkeypatch, listing
    root = tmp_path / "proj"
    _build_tree(root)
    via_listing = aggregate_tree(root / "node_modules", use_listing=True)
    via_scandir = aggregate_tree(root / "node_modules", use_listing=False)
    assert via_listing is not None
    assert via_listing == via_scandir
    assert isinstance(via_listing, OpaqueAggregate)


@_BOTH_PATHS
def test_opaque_root_directly_under_the_scan_root_is_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    (root / "node_modules" / "a").mkdir(parents=True)
    (root / "node_modules" / "a" / "i.js").write_bytes(b"1234")
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        rows = _rows(index, root)
        assert sorted(rows) == [(root / "node_modules").as_posix()]
        assert rows[(root / "node_modules").as_posix()].agg_bytes == 4


@_BOTH_PATHS
def test_rescan_converts_a_per_file_index_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        # Old behaviour: nothing is opaque, every file is a row.
        monkeypatch.setattr(scanner_module, "OPAQUE_DIR_NAMES", frozenset())
        scan_tree(root, index)
        before = _rows(index, root)
        nm_children = [p for p in before if p.startswith((root / "node_modules").as_posix() + "/")]
        assert nm_children and not any(r.opaque for r in before.values())
        total_before = index.subtree_size_bytes(root)

        monkeypatch.setattr(scanner_module, "OPAQUE_DIR_NAMES", OPAQUE_DIR_NAMES)
        scan_tree(root, index)  # incremental: every file is unchanged by size/mtime
        after = _rows(index, root)

        # (the stale child rows are shed by the opaque root's own flush, before the prune pass)
        assert not [p for p in after if p.startswith((root / "node_modules").as_posix() + "/")]
        assert after[(root / "node_modules").as_posix()].opaque is True
        assert len(after) < len(before)
        # The converted index answers the size question exactly as the per-file one did.
        assert index.subtree_size_bytes(root) == total_before


@_BOTH_PATHS
def test_a_scan_that_stops_collapsing_a_kind_overwrites_the_stale_opaque_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """A kind dropped from OPAQUE_DIR_NAMES later: the old aggregate row must not survive next to
    freshly written child rows (double counting)."""
    root = tmp_path / "proj"
    (root / "node_modules" / "a").mkdir(parents=True)
    (root / "node_modules" / "a" / "i.js").write_bytes(b"12345")
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(root, index)
        assert index.get_record(root / "node_modules").opaque is True  # type: ignore[union-attr]
        monkeypatch.setattr(scanner_module, "OPAQUE_DIR_NAMES", frozenset())
        scan_tree(root, index)
        record = index.get_record(root / "node_modules")
        assert record is not None and record.opaque is False and record.agg_bytes == 0
        assert index.subtree_size_bytes(root) == 5


@_BOTH_PATHS
def test_opaque_row_is_rewritten_every_scan_and_aggregates_track_deep_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """Staleness: a deep in-place write moves neither the root directory's mtime nor any size
    the old unchanged-compare could see on the root row, yet the aggregates must follow."""
    root = tmp_path / "proj"
    _build_tree(root)
    deep = root / "node_modules" / "react" / "lib" / "util.js"
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(root, index)
        nm_path = root / "node_modules"
        first = index.get_record(nm_path)
        assert first is not None
        root_dir_mtime = nm_path.stat().st_mtime

        with deep.open("ab") as handle:  # in-place growth: directory mtimes do not move
            handle.write(b"!" * 1000)
        bumped = (first.agg_newest_mtime or 0.0) + 500
        os.utime(deep, (bumped, bumped))
        assert nm_path.stat().st_mtime == root_dir_mtime  # the premise of the test

        stats = scan_tree(root, index)
        second = index.get_record(nm_path)
        assert second is not None
        assert second.agg_bytes == first.agg_bytes + 1000
        assert (second.agg_newest_mtime or 0.0) >= (first.agg_newest_mtime or 0.0) + 500
        assert second.mtime == first.mtime  # the root's own row alone could not have shown this
        # every opaque root was rewritten although nothing about its own row changed
        assert stats.files_written >= 5


@_BOTH_PATHS
def test_a_reparse_point_inside_an_opaque_tree_is_counted_but_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"h" * 100_000)
    root = tmp_path / "proj"
    (root / "node_modules" / "a").mkdir(parents=True)
    (root / "node_modules" / "a" / "i.js").write_bytes(b"1234")
    _make_junction(root / "node_modules" / "a" / "link", outside)
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        record = index.get_record(root / "node_modules")
        assert record is not None and record.opaque is True
        assert record.agg_bytes == 4  # nothing from `outside`
        assert record.agg_files == 2  # i.js + the junction counted as an entry
        assert (outside / "huge.bin").exists()


@_BOTH_PATHS
def test_an_unreadable_subdirectory_prevents_collapsing_and_is_accounted_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """Decision (ADR-0042 addendum): an opaque root whose tree could not be fully measured is
    never written as an aggregate -- it falls back to ordinary recursion, so the unreadable path
    lands in the existing skipped / inaccessible accounting and nothing claims completeness."""
    root = tmp_path / "proj"
    (root / "node_modules" / "ok").mkdir(parents=True)
    (root / "node_modules" / "ok" / "i.js").write_bytes(b"12345")
    (root / "node_modules" / "locked").mkdir()
    (root / "node_modules" / "locked" / "secret.js").write_bytes(b"secret")

    real_scandir = os.scandir
    real_list = scanner_module.list_directory

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if str(path).endswith("locked"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    def fake_list(path: str, *args: object, **kwargs: object) -> object:
        if path.endswith("locked"):
            raise PermissionError(13, "Access is denied", path)
        return real_list(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", fake_scandir)
    monkeypatch.setattr(scanner_module, "list_directory", fake_list)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        stats = scan_tree(root, index, incremental=False)
        record = index.get_record(root / "node_modules")
        assert record is not None
        assert record.opaque is False and record.agg_bytes == 0 and record.agg_files == 0
        assert stats.skipped_unreadable_count == 1
        assert index.inaccessible_summary().path_count == 1
        # the readable part keeps its per-file rows (the ordinary, honest, lower-bound view)
        assert index.record_exists(root / "node_modules" / "ok" / "i.js")


def test_aggregate_tree_fails_closed_on_cancellation_budget_and_missing_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "t"
    (root / "a").mkdir(parents=True)
    for i in range(5):
        (root / "a" / f"f{i}.txt").write_bytes(b"x")
    assert aggregate_tree(root) is not None
    cancelled = threading.Event()
    cancelled.set()
    assert aggregate_tree(root, cancel_event=cancelled) is None
    assert aggregate_tree(root, max_entries=3) is None  # over budget: no partial number
    assert aggregate_tree(root, max_entries=100) is not None
    assert aggregate_tree(tmp_path / "does-not-exist") is None


def test_aggregate_tree_empty_tree_has_no_newest_mtime(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert aggregate_tree(empty) == OpaqueAggregate(
        files=0,
        bytes=0,
        newest_mtime=None,
        physical_bytes=0,
        entries=0,
        newest_dir_mtime=empty.stat().st_mtime,  # the root directory itself is always a directory
    )


@_BOTH_PATHS
def test_progress_tracker_is_credited_for_entries_walked_inside_opaque_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    seen: list[int] = []
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        scan_tree(root, index, on_progress=lambda done, _total, _t: seen.append(done))
    total_entries = sum(len(d) + len(f) for _, d, f in os.walk(root))
    assert seen and max(seen) >= total_entries - 1  # entries inside opaque trees are counted


@_BOTH_PATHS
def test_row_count_does_not_grow_with_the_size_of_an_opaque_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """The point of the ADR: index rows are independent of how many files sit in a dependency
    tree (what the aggregate says changes, the row count does not)."""
    counts = []
    for n_files in (3, 300):
        root = tmp_path / f"proj_{n_files}"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_bytes(b"a")
        for i in range(n_files):
            pkg = root / "node_modules" / f"pkg{i % 10}"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / f"f{i}.js").write_bytes(b"x")
        with _scan(root, tmp_path / f"i{n_files}.sqlite3", monkeypatch, listing) as index:
            counts.append(len(_rows(index, root)))
            assert index.get_record(root / "node_modules").agg_files == n_files  # type: ignore[union-attr]
    assert counts[0] == counts[1] == 3  # src, src/a.py, node_modules


# --- a scan rooted INSIDE an opaque tree (known limitation, bounded by a safe invalidation) -------


@_BOTH_PATHS
def test_scanning_a_path_inside_an_opaque_tree_drops_the_enclosing_aggregate_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """Characterisation: the inner scan indexes the tree's files as ordinary rows, so the earlier
    whole-project scan's aggregate row for `node_modules` must not survive next to them (it would
    be counted twice and could be compared as current). It is dropped; the candidate returns when
    the enclosing root is rescanned."""
    root = tmp_path / "proj"
    _build_tree(root)
    nm = root / "node_modules"
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        assert index.get_record(nm).opaque is True  # type: ignore[union-attr]
        whole = index.subtree_size_bytes(root)
        scan_tree(nm / "react", index)  # the root of this scan is INSIDE node_modules
        assert index.get_record(nm) is None  # the stale aggregate is gone, not double counted
        react_files = sum(p.stat().st_size for p in (nm / "react").rglob("*") if p.is_file())
        assert index.subtree_size_bytes(nm) == react_files  # only what the inner scan indexed
        assert index.subtree_size_bytes(root) < whole  # under-counts, never over-counts
        # rescanning the enclosing root restores the single aggregate row and the full size
        scan_tree(root, index)
        assert index.get_record(nm).opaque is True  # type: ignore[union-attr]
        assert index.subtree_size_bytes(root) == whole
        assert not [p for p in _rows(index, root) if p.startswith(nm.as_posix() + "/")]


@_BOTH_PATHS
def test_scanning_the_opaque_directory_itself_drops_its_own_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    nm = root / "node_modules"
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        scan_tree(nm, index)
        assert index.get_record(nm) is None
        assert index.record_exists(nm / "react" / "index.js")


def test_scanning_elsewhere_leaves_opaque_rows_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    _build_tree(root)
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, True) as index:
        scan_tree(root / "src", index)
        assert index.get_record(root / "node_modules").opaque is True  # type: ignore[union-attr]


@_BOTH_PATHS
def test_a_case_variant_scan_root_inside_an_opaque_tree_drops_the_enclosing_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """NTFS is case-insensitive, index paths keep the case they were walked with: a scan rooted
    at `.../Node_Modules/react` must still invalidate the row stored as `.../node_modules`."""
    root = tmp_path / "proj"
    _build_tree(root)
    nm = root / "node_modules"
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        whole = index.subtree_size_bytes(root)
        nm_bytes = sum(p.stat().st_size for p in nm.rglob("*") if p.is_file())
        react_bytes = sum(p.stat().st_size for p in (nm / "react").rglob("*") if p.is_file())
        scan_tree(root / "Node_Modules" / "react", index)  # different case than on disk
        assert index.get_record(nm) is None
        # the stale aggregate is gone (not counted next to the inner scan's rows): the project
        # total is what is outside node_modules plus what the inner scan indexed
        assert index.subtree_size_bytes(root) == whole - nm_bytes + react_bytes


@_BOTH_PATHS
def test_an_unreadable_directory_stat_inside_an_opaque_tree_prevents_collapsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    """Only the directory `os.stat` fails (listing/scandir succeed): the aggregate cannot be
    complete, so it must report 'incomplete' -- not record a 0.0 directory mtime and collapse."""
    root = tmp_path / "proj"
    (root / "node_modules" / "ok").mkdir(parents=True)
    (root / "node_modules" / "ok" / "i.js").write_bytes(b"12345")
    (root / "node_modules" / "locked").mkdir()
    (root / "node_modules" / "locked" / "s.js").write_bytes(b"s")

    real_stat = os.stat

    def flaky_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path).endswith("locked"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", flaky_stat)
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    assert aggregate_tree(root / "node_modules", use_listing=listing) is None
    with ScanIndex(tmp_path / "i.sqlite3") as index:
        stats = scan_tree(root, index, incremental=False)
        record = index.get_record(root / "node_modules")
        assert record is not None and record.opaque is False
        assert record.agg_newest_dir_mtime is None and record.agg_files == 0
        assert stats.skipped_unreadable_count >= 1  # the ordinary walk accounts for it
        assert index.record_exists(root / "node_modules" / "ok" / "i.js")


@_BOTH_PATHS
def test_a_junction_named_like_an_opaque_kind_stays_a_plain_reparse_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"h" * 5000)
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    _make_junction(root / "node_modules", outside)  # directly under the scan root
    _make_junction(root / "app" / ".venv", outside)  # nested
    with _scan(root, tmp_path / "i.sqlite3", monkeypatch, listing) as index:
        rows = _rows(index, root)
        for junction in (root / "node_modules", root / "app" / ".venv"):
            record = rows[junction.as_posix()]
            assert record.is_reparse_point is True
            assert record.opaque is False and record.agg_files == 0 and record.agg_bytes == 0
        assert not [p for p in rows if "huge.bin" in p]  # target never followed
        assert index.subtree_size_bytes(root) == 0


def test_aggregate_tree_refuses_a_reparse_point_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.bin").write_bytes(b"x" * 10)
    link = tmp_path / "node_modules"
    _make_junction(link, outside)
    assert aggregate_tree(link) is None
    assert aggregate_tree(link, use_listing=False) is None
