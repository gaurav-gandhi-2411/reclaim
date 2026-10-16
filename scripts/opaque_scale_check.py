from __future__ import annotations

# opaque_scale_check.py -- ADR-0042 before/after on a synthetic tree: one big `node_modules`
# (60,000 files by default) next to ordinary files (20,000), scanned into a fresh index with the
# opaque kinds DISABLED (the pre-ADR behaviour: every file a row) and ENABLED. Prints rows,
# on-disk index size and scan wall time for each; asserts nothing about time (numbers go in the
# PR, not in a CI gate).
#
# Usage: python scripts/opaque_scale_check.py [WORKDIR] [NODE_MODULES_FILES] [OTHER_FILES]
#
# WORKDIR must be a scratch directory OUTSIDE the user profile; it is created if missing and the
# generated tree/indexes are left in place (delete them yourself once the numbers are recorded).
import sqlite3
import sys
import time
from pathlib import Path

import reclaim.scanner as scanner_module
from reclaim.index import ScanIndex
from reclaim.models import OPAQUE_DIR_NAMES
from reclaim.scanner import scan_tree

_FILES_PER_DIR = 50


def _build(root: Path, node_modules_files: int, other_files: int) -> None:
    nm = root / "app" / "node_modules"
    for i in range(node_modules_files):
        directory = nm / f"pkg{i // _FILES_PER_DIR:05d}" / "lib"
        if i % _FILES_PER_DIR == 0:
            directory.mkdir(parents=True)
        (directory / f"m{i}.js").write_bytes(b"x" * 64)
    other = root / "docs"
    for i in range(other_files):
        directory = other / f"d{i // _FILES_PER_DIR:05d}"
        if i % _FILES_PER_DIR == 0:
            directory.mkdir(parents=True)
        (directory / f"f{i}.txt").write_bytes(b"y" * 32)


def _db_bytes(db: Path) -> int:
    return sum(p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.exists())


def _measure(root: Path, db: Path, *, opaque: bool) -> dict[str, object]:
    scanner_module.OPAQUE_DIR_NAMES = OPAQUE_DIR_NAMES if opaque else frozenset()
    started = time.perf_counter()
    with ScanIndex(db) as index:
        stats = scan_tree(root, index, incremental=False)
        rescan_started = time.perf_counter()
        rescan = scan_tree(root, index)
        rescan_seconds = time.perf_counter() - rescan_started
    seconds = rescan_started - started
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    return {
        "opaque": opaque,
        "rows": rows,
        "files_written_first_scan": stats.files_written,
        "files_written_rescan": rescan.files_written,
        "index_bytes": _db_bytes(db),
        "first_scan_seconds": round(seconds, 2),
        "rescan_seconds": round(rescan_seconds, 2),
    }


def main(argv: list[str]) -> int:
    workdir = Path(argv[1] if len(argv) > 1 else r"D:\reclaim-build\tmp\opaque_scale").resolve()
    node_modules_files = int(argv[2]) if len(argv) > 2 else 60_000
    other_files = int(argv[3]) if len(argv) > 3 else 20_000
    if Path.home() in (workdir, *workdir.parents):
        print(f"refusing: {workdir} is inside the user profile")
        return 2
    tree = workdir / "tree"
    if not tree.exists():
        _build(tree, node_modules_files, other_files)
    for opaque in (False, True):
        db = workdir / f"index_{'opaque' if opaque else 'perfile'}.sqlite3"
        if db.exists():
            print(f"refusing: {db} already exists (remove it deliberately and re-run)")
            return 2
        print(_measure(tree, db, opaque=opaque), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
