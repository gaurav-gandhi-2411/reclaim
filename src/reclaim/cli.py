from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reclaim.config import load_config, load_effective_config
from reclaim.dedup import generate_duplicate_candidates, materiality_exclusion_stats
from reclaim.detectors import generate_candidates
from reclaim.elevation import ElevatedProcessError, assert_not_elevated
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    QuarantineMethod,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
    SafetyInvariantError,
    apply_batch,
    restore_batch,
)
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.index import ScanIndex
from reclaim.logging_config import DEFAULT_LOG_PATH, configure_logging
from reclaim.mode import (
    DEFAULT_MODE_LOG_PATH,
    ModeSwitchDeniedError,
    current_mode,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Candidate, HashSkip, MaterialityExclusionStats, Mode, Tier
from reclaim.purge import purge_expired
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

_DEFAULT_DB_PATH = Path("data/reclaim_index.sqlite3")
_DEFAULT_CONFIG_PATH = Path("config.toml")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420

# Literal loopback IPs only — deliberately excludes the hostname "localhost", since that's a
# DNS/hosts-file lookup (uvicorn/the socket layer resolves it, not this code) and a tampered
# hosts file could in principle point it somewhere non-loopback. This tool moves and deletes
# files on command from whatever hits its API, so the bind address is a hard security boundary,
# not a convenience default — see SECURITY_HOST_VALIDATION_ONLY_LITERAL_LOOPBACK_IPS.
_ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "::1"})


def _loopback_host(value: str) -> str:
    """argparse `type=` for `--host`: fails fast at parse time, not deep inside `_run_serve`,
    and can't be bypassed by any caller that goes through the CLI (including a future
    `reclaim dashboard` subcommand reusing this same parser machinery)."""
    if value not in _ALLOWED_BIND_HOSTS:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an allowed bind address — reclaim serve must never be reachable "
            "from the network (this tool moves and permanently deletes files on command from "
            f"whatever hits its API). Allowed: {', '.join(sorted(_ALLOWED_BIND_HOSTS))}."
        )
    return value


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
        "--include-categories",
        type=str,
        default=None,
        help="Comma-separated fine-grained candidate categories (e.g. "
        "'windows_temp,package_cache') to restrict this apply to. A category's group must "
        "still be enabled in config.toml and its tier still match --tier for it to be "
        "generated at all — this flag narrows an already-generated, already-tier-filtered "
        "selection further, for staged/scoped rollouts (apply a reviewed subset now, defer "
        "the rest to a later run). Default: no restriction.",
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
    apply_parser.add_argument(
        "--mode-log",
        type=Path,
        default=None,
        help=f"Override the mode-change log path (default: {DEFAULT_MODE_LOG_PATH}) — the "
        "live safe/power mode is resolved from this log, never from config.toml.",
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
    purge_parser.add_argument(
        "--rebuildable-only",
        action="store_true",
        help="Restrict this purge to entries whose category is deterministically rebuildable "
        "(dev_artifacts/package_caches/temp_and_browser_caches/crash_dumps) — never touches a "
        "model_caches/duplicates/other vault entry even if one happened to also be eligible.",
    )
    purge_parser.add_argument(
        "--mode-log",
        type=Path,
        default=None,
        help=f"Override the mode-change log path (default: {DEFAULT_MODE_LOG_PATH}) — purge "
        "unconditionally refuses while the live mode is safe, regardless of manifest content.",
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
    undo_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine) — must match the "
        "directory 'reclaim apply' actually vaulted into, since restore_batch validates every "
        "manifest entry's vault_path resolves inside it before moving anything.",
    )
    undo_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults if "
        "missing) — used to build the live SafetyValidator the pre-restore integrity check "
        "runs against.",
    )

    mode_parser = subparsers.add_parser(
        "mode",
        help="Show or switch the safety mode (safe/power). Safe is the default for every "
        "fresh install: recommend/review-only, Recycle-Bin-only, dangerous categories off. "
        "Power unlocks the full behavior (vault/direct-delete/auto-apply) and requires typed "
        "confirmation to enter; reverting to safe never requires confirmation.",
    )
    mode_parser.add_argument(
        "--mode-log",
        type=Path,
        default=None,
        help=f"Override the mode-change log path (default: {DEFAULT_MODE_LOG_PATH}).",
    )
    mode_subparsers = mode_parser.add_subparsers(dest="mode_action")
    mode_power_parser = mode_subparsers.add_parser(
        "power",
        help="Switch to power mode. Requires --confirm with the exact required phrase.",
    )
    mode_power_parser.add_argument(
        "--confirm",
        required=True,
        help='Must exactly equal "I understand this can permanently delete files" — a typo '
        "means not confirmed, never close enough.",
    )
    mode_subparsers.add_parser("safe", help="Switch back to safe mode. No confirmation needed.")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the localhost-only FastAPI dashboard (scan/review/apply/undo in a browser). "
        "Does not open a browser tab for you — see 'dashboard' for that.",
    )
    _add_serve_like_arguments(serve_parser)

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Same as 'serve', but also opens your default browser to the dashboard once the "
        "server is up — the one-command way to launch Reclaim as an installed tool.",
    )
    _add_serve_like_arguments(dashboard_parser)

    return parser


def _add_serve_like_arguments(parser: argparse.ArgumentParser) -> None:
    """Shared by `serve` and `dashboard` — identical bind/storage arguments, since `dashboard`
    is `serve` plus an auto-opened browser tab (see `_run_dashboard`), not a different server."""
    parser.add_argument(
        "--host",
        type=_loopback_host,
        default=_DEFAULT_HOST,
        help=f"Bind host (default: {_DEFAULT_HOST}). Hard-enforced loopback-only — "
        f"{', '.join(sorted(_ALLOWED_BIND_HOSTS))} are the only accepted values; this tool "
        "moves and permanently deletes files and must never be reachable from the network.",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help=f"Bind port (default: {_DEFAULT_PORT})."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults if "
        "missing).",
    )
    parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
    )
    parser.add_argument(
        "--mode-log",
        type=Path,
        default=None,
        help=f"Override the mode-change log path (default: {DEFAULT_MODE_LOG_PATH}).",
    )
    parser.add_argument(
        "--first-run-state",
        type=Path,
        default=None,
        help=f"Override the first-run-acknowledged marker path (default: "
        f"{DEFAULT_FIRST_RUN_STATE_PATH}).",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help=f"Override the persistent rotating log file path (default: "
        f"{DEFAULT_LOG_PATH}) — see SUPPORT.md for what this file is for.",
    )


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
        # ADR-0006: only printed when this category has actually computed a hardlink-aware
        # estimate (reclaimable_bytes is None for every category that hasn't) — logical size
        # above is always real; this line is never silently substituted for it.
        if (
            candidate.reclaimable_bytes is not None
            and candidate.reclaimable_bytes != candidate.size_bytes
        ):
            print(  # noqa: T201
                f"      estimated reclaimable: {candidate.reclaimable_bytes:,} bytes "
                f"(logical size above may be shared with a surviving hardlink)"
            )
        if candidate.rebuild_instruction is not None:
            print(f"      recovery: {candidate.rebuild_instruction}")  # noqa: T201
        if candidate.recovery_cost_note is not None:
            print(f"      cost: {candidate.recovery_cost_note}")  # noqa: T201


def _print_duplicate_reclaim_estimate(selected: Sequence[Candidate]) -> None:
    """ADR-0006: the uv/cache purge measured logical size (14.3GB) against real disk-free delta
    (5.21GB) and found a large gap from Windows hardlinks sharing blocks across names. Exact
    duplicates are the same shape in reverse — a "duplicate" that's actually a hardlink to the
    kept copy reclaims 0 bytes if deleted — so the logical `size_bytes` total this category
    reports is never trustable on its own; this prints the hardlink-aware estimate alongside it,
    clearly separated, never blended into one number."""
    duplicates = [c for c in selected if c.category_group == "duplicates"]
    if not duplicates:
        return
    logical_total = sum(c.size_bytes for c in duplicates)
    reclaimable_total = sum(
        c.reclaimable_bytes if c.reclaimable_bytes is not None else c.size_bytes for c in duplicates
    )
    already_deduplicated = [c for c in duplicates if c.reclaimable_bytes == 0]
    print(  # noqa: T201
        f"  exact_duplicate reclaim estimate: logical={logical_total:,} bytes, "
        f"hardlink-aware estimated reclaimable={reclaimable_total:,} bytes"
    )
    if already_deduplicated:
        print(  # noqa: T201
            f"    {len(already_deduplicated)} candidate(s) already deduplicated via an existing "
            "hardlink to the surviving copy — 0 bytes reclaimable each, excluded from the "
            "estimated-reclaimable total above"
        )


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
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim apply: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    root: Path = args.path
    if not root.is_dir():
        print(f"reclaim: apply path does not exist or is not a directory: {root}", file=sys.stderr)  # noqa: T201
        return 1
    if not args.db.exists():
        print(f"reclaim: index not found at {args.db} — run 'reclaim scan' first", file=sys.stderr)  # noqa: T201
        return 1

    config_path: Path = args.config
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    config = load_effective_config(
        config_path if config_path.exists() else None, mode=current_mode(mode_log)
    )

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

    if args.include_categories is not None:
        wanted_categories = {c.strip() for c in args.include_categories.split(",") if c.strip()}
        before_count = len(selected)
        selected = [c for c in selected if c.category in wanted_categories]
        print(  # noqa: T201
            f"reclaim apply: --include-categories restricted selection to "
            f"{sorted(wanted_categories)} — {len(selected)}/{before_count} "
            "tier/root-eligible candidate(s) kept, the rest deferred to a later run."
        )

    # Safe mode only ever allows recycle_bin (apply_batch enforces this structurally regardless
    # of what's passed here) — resolved automatically rather than requiring the user to already
    # know to pass --method recycle_bin, so a plain `reclaim apply --apply` just works under
    # the default safe mode instead of failing on the --method flag's own "vault" default.
    method: QuarantineMethod = "recycle_bin" if config.mode == Mode.SAFE else args.method
    try:
        report = apply_batch(
            selected,
            safety=safety,
            apply=args.apply,
            method=method,
            mode=config.mode,
            vault_dir=args.vault_dir,
            manifest_path=args.manifest,
            direct_delete_size_guard_bytes=config.safety.direct_delete_size_guard_bytes,
            direct_delete_size_guard_retention_days=(
                config.safety.direct_delete_size_guard_retention_days
            ),
        )
    except (SafetyInvariantError, SafeModeViolationError) as exc:
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
    _print_duplicate_reclaim_estimate(selected)
    _print_top_n_largest(selected)
    _print_hash_skips(hash_skips)
    if materiality is not None:
        _print_materiality_exclusion(materiality, min_reclaim_bytes=min_reclaim_bytes)
    for item in report.items:
        if not item.succeeded:
            print(f"  FAILED: {item.path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def _run_serve(args: argparse.Namespace, *, open_browser: bool = False) -> int:
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim serve: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    # Imports deferred to inside the function: uvicorn/the FastAPI app are only needed for
    # `reclaim serve`/`dashboard`, so `scan`/`apply`/`undo` (and every existing test importing
    # this module) never pay the FastAPI/uvicorn import cost.
    import threading
    import webbrowser

    import uvicorn

    from reclaim.api.app import create_app

    # Defense in depth: `_loopback_host` already gates this at argparse parse time for every
    # real CLI invocation, but this function is also callable directly (tests, a future
    # in-process caller) bypassing argparse entirely — re-validate here so there is no path to
    # `uvicorn.run` with a non-loopback host regardless of caller.
    _loopback_host(args.host)

    config_path: Path = args.config
    # Raw config, deliberately — AppState.effective_config resolves the live mode (and, when
    # safe, the category override) fresh on every request, not once at server startup, so a
    # mode switch via the API takes effect immediately without a restart. See AppState's
    # docstring for why create_app must never receive an already-mode-resolved config here.
    config = load_config(config_path if config_path.exists() else None)
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    first_run_state = (
        args.first_run_state if args.first_run_state is not None else DEFAULT_FIRST_RUN_STATE_PATH
    )
    log_path = args.log_path if args.log_path is not None else DEFAULT_LOG_PATH
    app = create_app(
        db_path=args.db,
        config=config,
        vault_dir=args.vault_dir,
        manifest_path=args.manifest,
        mode_log_path=mode_log,
        first_run_state_path=first_run_state,
        log_path=log_path,
        host=args.host,
        port=args.port,
    )
    url = f"http://{args.host}:{args.port}"
    print(f"reclaim serve: {url} (Ctrl+C to stop)")  # noqa: T201
    if open_browser:
        # uvicorn.run() blocks for the life of the server, so the browser is opened from a
        # short-delayed background timer rather than after the call — by the time the delay
        # elapses the server is up in the near-totality of real runs (startup is sub-second);
        # if it isn't, the browser's own connection retry/error page covers the gap, same as
        # opening a bookmark half a second before your server finishes starting normally would.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _run_dashboard(args: argparse.Namespace) -> int:
    return _run_serve(args, open_browser=True)


def _run_mode(args: argparse.Namespace) -> int:
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    action = getattr(args, "mode_action", None)

    if action is None:
        live = current_mode(mode_log)
        print(f"reclaim mode: {live.value}")  # noqa: T201
        return 0

    if action == "power":
        try:
            entry = switch_to_power_mode(args.confirm, log_path=mode_log)
        except ModeSwitchDeniedError as exc:
            print(f"reclaim mode: {exc}", file=sys.stderr)  # noqa: T201
            return 1
        print(  # noqa: T201
            f"reclaim mode: switched {entry.from_mode.value} -> {entry.to_mode.value} "
            f"at {entry.changed_at}"
        )
        return 0

    if action == "safe":
        entry = switch_to_safe_mode(log_path=mode_log)
        print(  # noqa: T201
            f"reclaim mode: switched {entry.from_mode.value} -> {entry.to_mode.value} "
            f"at {entry.changed_at}"
        )
        return 0

    print(f"reclaim mode: unknown action {action!r}", file=sys.stderr)  # noqa: T201
    return 1


def _run_undo(args: argparse.Namespace) -> int:
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim undo: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    config_path: Path = args.config
    config = load_config(config_path if config_path.exists() else None)
    safety = SafetyValidator(config)

    try:
        report = restore_batch(
            args.batch_id,
            manifest_path=args.manifest,
            vault_dir=args.vault_dir,
            safety=safety,
        )
    except (
        BatchNotFoundError,
        RecycleBinRestoreUnsupportedError,
        DirectDeleteRestoreImpossibleError,
        RestoreIntegrityError,
    ) as exc:
        print(f"reclaim undo: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    print(  # noqa: T201
        f"reclaim undo: batch={report.batch_id} processed={report.files_processed} "
        f"succeeded={report.files_succeeded} failed={report.files_failed} "
        f"unsupported={report.files_unsupported} bytes_restored={report.bytes_restored}"
    )
    for item in report.items:
        if item.restore_unsupported:
            print(f"  SKIPPED (not restorable): {item.original_path} — {item.error}")  # noqa: T201
        elif not item.succeeded:
            print(f"  FAILED: {item.original_path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def _run_purge(args: argparse.Namespace) -> int:
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim purge: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    config_path: Path = args.config
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    live_mode = current_mode(mode_log)
    config = load_effective_config(config_path if config_path.exists() else None, mode=live_mode)
    safety = SafetyValidator(config)

    try:
        report = purge_expired(
            apply=args.apply,
            manifest_path=args.manifest,
            vault_dir=args.vault_dir,
            safety=safety,
            only_rebuildable=args.rebuildable_only,
            mode=live_mode,
        )
    except (SafetyInvariantError, SafeModeViolationError) as exc:
        print(f"reclaim purge: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(  # noqa: T201
        f"reclaim purge [{mode}] processed={report.files_processed} "
        f"succeeded={report.files_succeeded} failed={report.files_failed} "
        f"bytes_freed={report.bytes_freed}"
    )
    if report.stale_count > 0:
        print(  # noqa: T201
            f"  stale (original path re-occupied, never restorable): "
            f"count={report.stale_count} bytes={report.stale_bytes}"
        )
    if report.disk_free_delta_bytes is not None:
        print(  # noqa: T201
            f"reclaim purge: disk free before={report.disk_free_before_bytes} "
            f"after={report.disk_free_after_bytes} delta={report.disk_free_delta_bytes}"
        )
    for category, breakdown in sorted(report.category_breakdown.items()):
        print(f"  {category}: count={breakdown.count} bytes={breakdown.bytes_freed}")  # noqa: T201
    for item in report.items:
        if item.succeeded and item.stale:
            print(f"  STALE: {item.original_path} (original path re-occupied)")  # noqa: T201
        elif not item.succeeded:
            print(f"  FAILED: {item.original_path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    # Every subcommand goes through this one entry point, so this is the single place a
    # persistent, rotating log file needs wiring up once per process (G25: before this, every
    # `structlog.get_logger(__name__)` call in the codebase rendered to structlog's
    # console-only default, which vanishes the moment a console-less launch -- a Start Menu
    # shortcut, or a closed console window -- has nowhere to show it). Reads `DEFAULT_LOG_PATH`
    # via this module's own imported name (not `logging_config.configure_logging`'s internal
    # default) so a test can redirect it with `monkeypatch.setattr("reclaim.cli.DEFAULT_LOG_PATH",
    # ...)`, the same pattern already used for `DEFAULT_MODE_LOG_PATH` elsewhere in this file --
    # without that, every CLI invocation in the test suite would write into the real repo's
    # working directory instead of a test's own tmp_path.
    configure_logging(DEFAULT_LOG_PATH)
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
    if args.command == "mode":
        return _run_mode(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
