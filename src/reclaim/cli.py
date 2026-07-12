from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reclaim.index import ScanIndex
from reclaim.scanner import scan_tree

_DEFAULT_DB_PATH = Path("data/reclaim_index.sqlite3")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reclaim")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan", help="Scan a directory tree and build/update the SQLite inventory index."
    )
    scan_parser.add_argument("path", type=Path, help="Root directory to scan.")
    scan_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
    )
    scan_parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full rescan, ignoring the incremental (size, mtime) cache.",
    )
    scan_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Thread pool size for the per-top-level-directory walk (default: cpu-based).",
    )
    return parser


def _run_scan(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.is_dir():
        print(f"reclaim: scan path does not exist or is not a directory: {root}", file=sys.stderr)  # noqa: T201
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with ScanIndex(args.db) as index:
        stats = scan_tree(root, index, incremental=not args.full, max_workers=args.workers)

    print(  # noqa: T201 -- CLI output, not application logging
        f"reclaim scan: {stats.entries_total} entries under {stats.root} "
        f"({stats.dirs_visited} dirs visited, {stats.files_written} written, "
        f"{stats.files_unchanged} unchanged, {stats.files_pruned} pruned) "
        f"in {stats.elapsed_seconds:.2f}s"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _run_scan(args)
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
