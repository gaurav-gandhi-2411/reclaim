from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reclaim.config import load_config
from reclaim.dedup import generate_duplicate_candidates, materiality_exclusion_stats
from reclaim.detectors import generate_candidates
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    QuarantineMethod,
    RecycleBinRestoreUnsupportedError,
    SafetyInvariantError,
    apply_batch,
    restore_batch,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, HashSkip, MaterialityExclusionStats, Tier
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

_DEFAULT_DB_PATH = Path("data/reclaim_index.sqlite3")
_DEFAULT_CONFIG_PATH = Path("config.toml")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420


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

    apply_parser = subparsers.add_parser(
        "apply",
        help="Generate candidates from a scan index and quarantine the selected tier "
        "(dry-run by default; pass --apply to actually act).",
    )
    apply_parser.add_argument(
        "path", type=Path, help="Root directory to scope candidates to (must be under this path)."
    )
    apply_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually quarantine files. Without this flag, nothing on disk is touched — a "
        "full simulated report is produced instead (dry-run is the default mode).",
    )
    apply_parser.add_argument(
        "--tier",
        choices=("A", "B", "both"),
        default="A",
        help="Which candidate tier(s) to apply. Default A only: Tier B is review-queue-only "
        "and is never silently auto-applied without an explicit --tier B/both.",
    )
    apply_parser.add_argument(
        "--include-duplicates",
        action="store_true",
        help="Also run the exact-duplicate pipeline (size bucket -> partial hash -> full "
        "BLAKE3 hash over every file on disk in a size-collision group). Opt-in and off by "
        "default: on a large/whole-disk index this pass can take a long time, so the fast, "
        "hashing-free report (rule detectors only) is always available without it — request "
        "this flag once you're ready to pay for duplicate detection too.",
    )
    apply_parser.add_argument(
        "--method",
        choices=("vault", "recycle_bin"),
        default="vault",
        help="Quarantine method. vault (default) is the only method with guaranteed, "
        "automated restore-by-batch; recycle_bin sends to the Windows Recycle Bin and cannot "
        "be restored by this tool.",
    )
    apply_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
    )
    apply_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults if "
        "missing).",
    )
    apply_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine).",
    )
    apply_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
    )

    purge_parser = subparsers.add_parser(
        "purge",
        help="Permanently delete vaulted items whose retention window has passed "
        "(dry-run by default; pass --apply to actually delete).",
    )
    purge_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete expired vault entries. Without this flag, nothing on disk is "
        "touched — a full simulated report is produced instead (dry-run is the default mode).",
    )
    purge_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults if "
        "missing) — used to build the live SafetyValidator the pre-purge re-check runs "
        "against.",
    )
    purge_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help="Accepted for CLI symmetry with 'scan'/'apply'; unused — purge_expired only reads "
        "the quarantine manifest, never the scan index.",
    )
    purge_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
    )
    purge_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine).",
    )

    undo_parser = subparsers.add_parser("undo", help="Restore a previously quarantined batch.")
    undo_parser.add_argument("batch_id", help="Batch id printed by a prior 'reclaim apply' run.")
    undo_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help="Accepted for CLI symmetry with 'scan'/'apply'; unused — restore_batch only reads "
        "the quarantine manifest, never the scan index.",
    )
    undo_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the localhost-only FastAPI dashboard (scan/review/apply/undo in a browser).",
    )
    serve_parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Bind host (default: {_DEFAULT_HOST}). This tool moves/deletes files — never bind "
        "0.0.0.0 or a non-loopback address unless you understand that risk.",
    )
    serve_parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help=f"Bind port (default: {_DEFAULT_PORT})."
    )
    serve_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
    )
    serve_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults if "
        "missing).",
    )
    serve_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine).",
    )
    serve_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
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


_TIER_SELECTIONS: dict[str, frozenset[Tier]] = {
    "A": frozenset({Tier.A}),
    "B": frozenset({Tier.B}),
    "both": frozenset({Tier.A, Tier.B}),
}


def _under_root(candidate_path: Path, root: Path) -> bool:
    """True if `candidate_path` is `root` itself or a descendant of it. `resolve()` doesn't
    require the path to exist, so this works for candidates the index recorded even if the
    filesystem has changed since the last scan."""
    resolved_root = root.resolve()
    resolved_candidate = candidate_path.resolve()
    return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents


_REPORT_TOP_N = 10


def _print_top_n_largest(selected: Sequence[Candidate]) -> None:
    largest = sorted(selected, key=lambda c: c.size_bytes, reverse=True)[:_REPORT_TOP_N]
    if not largest:
        return
    print(f"  top {len(largest)} largest candidates:")  # noqa: T201
    for candidate in largest:
        print(f"    {candidate.size_bytes:>14,} bytes  {candidate.path}")  # noqa: T201
        if candidate.rebuild_instruction is not None:
            print(f"      recovery: {candidate.rebuild_instruction}")  # noqa: T201
        if candidate.recovery_cost_note is not None:
            print(f"      cost: {candidate.recovery_cost_note}")  # noqa: T201


def _print_hash_skips(skips: Sequence[HashSkip]) -> None:
    if not skips:
        return
    print(f"  skipped/unreadable during duplicate hashing: {len(skips)}")  # noqa: T201
    for skip in skips[:_REPORT_TOP_N]:
        print(f"    [{skip.stage}] {skip.path} — {skip.reason}")  # noqa: T201
    if len(skips) > _REPORT_TOP_N:
        print(f"    ... and {len(skips) - _REPORT_TOP_N} more")  # noqa: T201


def _print_materiality_exclusion(
    stats: MaterialityExclusionStats, *, min_reclaim_bytes: int
) -> None:
    if stats.excluded_bucket_count == 0:
        return
    print(  # noqa: T201
        f"  duplicate detection: {stats.excluded_bucket_count} size bucket(s) excluded as "
        f"immaterial (below config.categories.duplicates.min_reclaim_bytes floor of "
        f"{min_reclaim_bytes:,} bytes), theoretical best-case size "
        f"{stats.theoretical_bytes:,} bytes (never hashed, so this is an upper bound, not a "
        "measured number)"
    )


def _run_apply(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.is_dir():
        print(f"reclaim: apply path does not exist or is not a directory: {root}", file=sys.stderr)  # noqa: T201
        return 1
    if not args.db.exists():
        print(f"reclaim: index not found at {args.db} — run 'reclaim scan' first", file=sys.stderr)  # noqa: T201
        return 1

    config_path: Path = args.config
    config = load_config(config_path if config_path.exists() else None)

    hash_skips: list[HashSkip] = []
    materiality: MaterialityExclusionStats | None = None
    min_reclaim_bytes = config.categories.duplicates.min_reclaim_bytes
    with ScanIndex(args.db) as index:
        safety = SafetyValidator(config)
        candidates: list[Candidate] = generate_candidates(index, config, safety)
        if args.include_duplicates:
            candidates += generate_duplicate_candidates(index, config, safety, skips=hash_skips)
            materiality = materiality_exclusion_stats(index, min_reclaim_bytes=min_reclaim_bytes)
        else:
            print(  # noqa: T201
                "reclaim apply: duplicate detection skipped (pass --include-duplicates to "
                "also run the size/hash-based exact-duplicate pipeline)."
            )

    tiers = _TIER_SELECTIONS[args.tier]
    selected = [c for c in candidates if c.tier in tiers and _under_root(c.path, root)]

    method: QuarantineMethod = args.method
    try:
        report = apply_batch(
            selected,
            safety=safety,
            apply=args.apply,
            method=method,
            vault_dir=args.vault_dir,
            manifest_path=args.manifest,
            direct_delete_size_guard_bytes=config.safety.direct_delete_size_guard_bytes,
            direct_delete_size_guard_retention_days=(
                config.safety.direct_delete_size_guard_retention_days
            ),
        )
    except SafetyInvariantError as exc:
        print(f"reclaim apply: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(  # noqa: T201
        f"reclaim apply [{mode}] batch={report.batch_id} method={report.method} "
        f"processed={report.files_processed} succeeded={report.files_succeeded} "
        f"failed={report.files_failed} bytes_freed={report.bytes_freed}"
    )
    if report.disk_free_delta_bytes is not None:
        print(  # noqa: T201
            f"reclaim apply: disk free before={report.disk_free_before_bytes} "
            f"after={report.disk_free_after_bytes} delta={report.disk_free_delta_bytes}"
        )
    for category, breakdown in sorted(report.category_breakdown.items()):
        print(  # noqa: T201
            f"  {category}: count={breakdown.count} bytes={breakdown.bytes_freed}"
        )
    _print_top_n_largest(selected)
    _print_hash_skips(hash_skips)
    if materiality is not None:
        _print_materiality_exclusion(materiality, min_reclaim_bytes=min_reclaim_bytes)
    for item in report.items:
        if not item.succeeded:
            print(f"  FAILED: {item.path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def _run_serve(args: argparse.Namespace) -> int:
    # Imports deferred to inside the function: uvicorn/the FastAPI app are only needed for
    # `reclaim serve`, so `scan`/`apply`/`undo` (and every existing test importing this module)
    # never pay the FastAPI/uvicorn import cost.
    import uvicorn

    from reclaim.api.app import create_app

    config_path: Path = args.config
    config = load_config(config_path if config_path.exists() else None)
    app = create_app(
        db_path=args.db, config=config, vault_dir=args.vault_dir, manifest_path=args.manifest
    )
    print(f"reclaim serve: http://{args.host}:{args.port} (Ctrl+C to stop)")  # noqa: T201
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _run_undo(args: argparse.Namespace) -> int:
    try:
        report = restore_batch(args.batch_id, manifest_path=args.manifest)
    except (
        BatchNotFoundError,
        RecycleBinRestoreUnsupportedError,
        DirectDeleteRestoreImpossibleError,
    ) as exc:
        print(f"reclaim undo: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    print(  # noqa: T201
        f"reclaim undo: batch={report.batch_id} processed={report.files_processed} "
        f"succeeded={report.files_succeeded} failed={report.files_failed} "
        f"bytes_restored={report.bytes_restored}"
    )
    for item in report.items:
        if not item.succeeded:
            print(f"  FAILED: {item.original_path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def _run_purge(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    config = load_config(config_path if config_path.exists() else None)
    safety = SafetyValidator(config)

    try:
        report = purge_expired(
            apply=args.apply,
            manifest_path=args.manifest,
            vault_dir=args.vault_dir,
            safety=safety,
        )
    except SafetyInvariantError as exc:
        print(f"reclaim purge: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(  # noqa: T201
        f"reclaim purge [{mode}] processed={report.files_processed} "
        f"succeeded={report.files_succeeded} failed={report.files_failed} "
        f"bytes_freed={report.bytes_freed}"
    )
    if report.disk_free_delta_bytes is not None:
        print(  # noqa: T201
            f"reclaim purge: disk free before={report.disk_free_before_bytes} "
            f"after={report.disk_free_after_bytes} delta={report.disk_free_delta_bytes}"
        )
    for category, breakdown in sorted(report.category_breakdown.items()):
        print(f"  {category}: count={breakdown.count} bytes={breakdown.bytes_freed}")  # noqa: T201
    for item in report.items:
        if not item.succeeded:
            print(f"  FAILED: {item.original_path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _run_scan(args)
    if args.command == "apply":
        return _run_apply(args)
    if args.command == "undo":
        return _run_undo(args)
    if args.command == "purge":
        return _run_purge(args)
    if args.command == "serve":
        return _run_serve(args)
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
