from __future__ import annotations

import argparse
import errno
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from reclaim import autoclean_schedule, autoclean_state, regenerable
from reclaim.app_paths import data_root
from reclaim.config import Config, exclusion_patterns, load_config, load_effective_config
from reclaim.dedup import generate_duplicate_candidates, materiality_exclusion_stats
from reclaim.detectors import generate_candidates
from reclaim.elevation import ElevatedProcessError, assert_not_elevated
from reclaim.executor import (
    DEFAULT_MANIFEST_PATH,
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    ProgressCallback,
    QuarantineMethod,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
    SafetyInvariantError,
    apply_batch,
    estimate_batch_seconds,
    fold_latest_manifest_entries,
    restore_batch,
    should_warn_about_batch_duration,
)
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.index import ScanIndex
from reclaim.index_prune import prune_dead_rows
from reclaim.logging_config import DEFAULT_LOG_PATH, configure_logging
from reclaim.mode import (
    DEFAULT_MODE_LOG_PATH,
    ModeSwitchDeniedError,
    current_mode,
    switch_to_power_mode,
    switch_to_safe_mode,
)
from reclaim.models import Candidate, HashSkip, MaterialityExclusionStats, Mode, Tier
from reclaim.purge import purge_eligible_entries, purge_expired
from reclaim.reconciliation import NotAVolumeRootError, compute_disk_reconciliation
from reclaim.safety import SafetyValidator
from reclaim.scanner import ScanDiskFullError, scan_tree

# Anchored via reclaim.app_paths.data_root (see PR #51 for the original confirmed-live crash
# this class of bug caused elsewhere): CWD-independent when compiled -- the frozen build now
# anchors to the real exe's directory instead of an arbitrary launch CWD. Dev/test resolution is
# deliberately UNCHANGED (still lazily CWD-relative, exactly like the original bare
# `Path("data/...")` literal -- data_root()'s own docstring explains why eager `Path.cwd()`
# capture would silently break `monkeypatch.chdir(tmp_path)`-based test isolation). Not yet
# reachable from any working-directory-less invocation today, but "not reachable today" is a
# property of today's call sites, not of the code.
_DEFAULT_DB_PATH = data_root() / "data" / "reclaim_index.sqlite3"
_DEFAULT_CONFIG_PATH = Path("config.toml")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420

# Hardcoded rather than read via `importlib.metadata.version("reclaim")`: the Nuitka standalone
# build (packaging/build_installer.ps1) never bundles a dist-info directory, so that lookup
# would raise `PackageNotFoundError` in the shipped binary specifically -- the one place this
# flag matters most (a user can't `pip show` an installed .exe). Checked against
# pyproject.toml's version by tests/test_version_consistency.py, the same pattern already used
# for packaging/reclaim.iss and packaging/build_installer.ps1's own version strings.
_VERSION = "1.3.0"

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
    # `action="version"` short-circuits during parsing (prints and calls `parser.exit()`
    # immediately when the flag is seen) -- it runs before the `required=True` subparsers check
    # below, so `reclaim --version` alone works with no subcommand. This is also the only
    # currently-available way to isolate pure interpreter+import overhead from a real
    # subcommand's own work for cold-start measurement (see packaging/RELEASE_RUNBOOK.md).
    parser.add_argument("--version", action="version", version=f"reclaim {_VERSION}")
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

    prune_parser = subparsers.add_parser(
        "index-prune",
        help="Remove index rows for files/directories that no longer exist (dry run unless "
        "--apply). Checks directories, not files; anything it cannot verify is kept.",
    )
    prune_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
    )
    prune_parser.add_argument(
        "--apply", action="store_true", help="Actually delete the dead rows (default: dry run)."
    )
    prune_parser.add_argument(
        "--root",
        type=str,
        action="append",
        default=None,
        help="Only consider rows under this path (repeatable). Default: the whole index.",
    )
    prune_parser.add_argument(
        "--deep",
        action="store_true",
        help="Also list every existing directory once and drop rows for entries no longer in "
        "it (slower: one directory listing per indexed directory).",
    )
    prune_parser.add_argument(
        "--vacuum",
        action="store_true",
        help="With --apply: VACUUM afterwards so the file shrinks (needs free disk about equal "
        "to the index size and no other process using the index).",
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Compare a fully-scanned volume's indexed total against the OS's real used-bytes "
        "figure, surfacing how much of any gap is explained by inaccessible (ACL-denied/IO-"
        "faulted) directories the scan could not enumerate (P0-5).",
    )
    reconcile_parser.add_argument(
        "volume", type=Path, help=r"Drive root that was scanned in full (e.g. C:\)."
    )
    reconcile_parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite index file (default: {_DEFAULT_DB_PATH}).",
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

    recover_parser = subparsers.add_parser(
        "recover",
        help="Detect and reconcile crash-orphaned quarantine operations — an apply/restore/"
        "purge interrupted mid-item by a kill or crash before its completion was logged "
        "(ADR-0026). Dry-run by default (reports only); pass --apply to write the reconciling "
        "manifest records. Never moves or deletes a file — only classifies what already "
        "happened by inspecting real on-disk state and appends the manifest record that "
        "reflects it.",
    )
    recover_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the reconciling manifest records. Without this flag, nothing is written — "
        "a full report of what would be reconciled is printed instead (dry-run is the default).",
    )
    recover_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Override the quarantine manifest path (default: data/quarantine/manifest.jsonl).",
    )
    recover_parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override the vault directory (default: data/quarantine) — used to verify a "
        "recorded vault_path actually resolves inside it before trusting it (same "
        "zip-slip-equivalent guard 'reclaim undo' already applies).",
    )

    check_disk_space_parser = subparsers.add_parser(
        "check-disk-space",
        help="Best-effort background check: fires a native Windows toast if disk free space "
        "has crossed the configured threshold (config.toml's [notifications] section, default "
        "OFF). Intended to run from the low-frequency Task Scheduler entry the installer "
        "registers (packaging/reclaim.iss), not interactively -- never raises, always prints a "
        "one-line status and exits 0 once config itself loads.",
    )
    check_disk_space_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults -- "
        "notifications disabled -- if missing).",
    )
    check_disk_space_parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Override the notification debounce/snooze state path (default: "
        "data/notification_state.json).",
    )
    check_disk_space_parser.add_argument(
        "--apply-snooze",
        action="store_true",
        help="Apply a snooze instead of running a normal check -- invoked by the toast's "
        "Snooze action button via the registered 'reclaim-notify:' protocol handler (see "
        "packaging/reclaim.iss), never by a user directly. Suppresses checks for "
        "config.toml's [notifications] snooze_days (default 7), then exits; fires no toast.",
    )

    auto_clean_parser = subparsers.add_parser(
        "auto-clean",
        help="Clean ONLY the regenerable safe tier (package-manager caches, aged temp files, "
        "crash dumps, caches of closed browsers -- ADR-0034's closed allow-list; never your "
        "files, the Recycle Bin or the quarantine vault). Dry run by default; this is what the "
        "weekly scheduled task runs with --apply --notify --scheduled.",
    )
    auto_clean_mode = auto_clean_parser.add_mutually_exclusive_group()
    auto_clean_mode.add_argument(
        "--apply",
        action="store_true",
        help="Really delete (permanently -- these items are regenerated by their owner). "
        "Without this flag nothing is deleted.",
    )
    auto_clean_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be cleaned and delete nothing (this is the default).",
    )
    auto_clean_mode.add_argument(
        "--reconcile-task",
        action="store_true",
        help="Installer hook (ADR-0034 'Upgrade path'): if config.toml's [autoclean] enabled is "
        "true, (re)register this account's weekly task with the current definition; if false, do "
        "nothing and create no task. Idempotent; cleans nothing.",
    )
    auto_clean_parser.add_argument(
        "--include-pytest-temp",
        action="store_true",
        help="Dry run only: also REPORT old pytest-<N> folders under pytest-of-<user> in %%TEMP%% "
        "(ADR-0034 addendum). It never enables deletion; that needs config.toml's "
        "[regenerable] pytest_temp = true, because those folders are shared by every project "
        "you test and Reclaim cannot tell whose is whose.",
    )
    auto_clean_parser.add_argument(
        "--json", action="store_true", help="Print the run as JSON (the dashboard API's shape)."
    )
    auto_clean_parser.add_argument(
        "--notify",
        action="store_true",
        help="With --apply: show a Windows toast when something was freed or left alone because "
        "it was in use.",
    )
    auto_clean_parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Invoked by the weekly Task Scheduler entry: do nothing (exit 0) unless "
        "config.toml's [autoclean] enabled is true, so a stale task never cleans after the user "
        "turned the feature off.",
    )
    auto_clean_parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {_DEFAULT_CONFIG_PATH}, built-in defaults -- "
        "auto-clean disabled -- if missing).",
    )

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

    mcp_serve_parser = subparsers.add_parser(
        "mcp-serve",
        help="Run the Model Context Protocol (MCP) control surface over stdio, for an AI agent "
        "to scan/review/delete through — see reclaim.mcp's module docstring for the safety "
        "model (selection by rule/category only, never a free-form path; every delete requires "
        "a fresh preview_apply's selection_hash). No network transport exists for this command.",
    )
    _add_mcp_serve_arguments(mcp_serve_parser)

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


def _add_mcp_serve_arguments(parser: argparse.ArgumentParser) -> None:
    """`mcp-serve`'s own storage arguments -- the same set `_add_serve_like_arguments` gives
    `serve`/`dashboard`, minus `--host`/`--port`: `reclaim.mcp.server.run_mcp_server` always
    speaks MCP over stdio, never binds a network socket at all, so there is no bind address for
    this command to accept or validate (see that function's own docstring)."""
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


def _load_config_or_none(
    command: str, config_path: Path, *, mode: Mode | None = None, effective: bool = False
) -> Config | None:
    """Loads `config.toml` for a CLI command, turning a malformed file into one friendly line
    on stderr instead of a raw traceback (D16), and returns `None` in that case so the caller
    can `return 1` -- same "print a clean message, exit 1" shape `ElevatedProcessError` already
    uses above.

    Every failure mode `load_config`/`load_effective_config` can raise for a bad *file* --
    `tomllib.TOMLDecodeError` (invalid TOML syntax), `UnknownConfigKeyError` (an unrecognized
    key with no forward-compat justification), `pydantic.ValidationError` (a recognized key with
    an invalid value) -- is a `ValueError` subclass; catching that one base class here covers all
    three without enumerating them, and anything that isn't one of those (a real bug) still
    propagates rather than being silently absorbed.
    """
    resolved_path = config_path if config_path.exists() else None
    try:
        if effective:
            return load_effective_config(resolved_path, mode=mode)
        return load_config(resolved_path)
    except (ValueError, OSError) as exc:  # OSError: unreadable file (e.g. PermissionError)
        print(  # noqa: T201
            f"reclaim {command}: config.toml is invalid ({config_path}): {exc}",
            file=sys.stderr,
        )
        return None


def _on_scan_progress_printer(processed: int, _estimated_total: int | None, elapsed: float) -> None:
    """Wave 1 finding #2 (2026-07-30 real-disk diagnosis): the raw `reclaim scan` CLI command
    used to call `scan_tree` with no `on_progress` at all -- a real 2.67M-file scan sat silent
    on the terminal for 7+ minutes, indistinguishable from a hang. `scan_tree`'s own
    `_ProgressTracker` already interval-gates calls to this (every `_HEARTBEAT_INTERVAL_SECONDS`
    -- never per-entry), so this only needs to print, not throttle. Deliberately does NOT run
    `count_entries_fast`'s pre-pass first the way the dashboard's live-ETA view does (see
    `api.service.run_scan`) -- that pre-pass is a second full stat-free tree walk purely to
    produce a total/ETA, and doubling I/O on every CLI invocation isn't worth it just to show a
    growing count instead of a count-with-a-denominator; "clearly still working" is the actual
    gap being closed here, not an ETA."""
    print(f"reclaim scan: scanning... {processed:,} entries visited ({elapsed:.0f}s elapsed)")  # noqa: T201


def _run_scan(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.is_dir():
        print(f"reclaim: scan path does not exist or is not a directory: {root}", file=sys.stderr)  # noqa: T201
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ScanIndex(args.db) as index:
            stats = scan_tree(
                root,
                index,
                incremental=not args.full,
                max_workers=args.workers,
                on_progress=_on_scan_progress_printer,
            )
    except ScanDiskFullError as exc:
        print(f"reclaim scan: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    print(  # noqa: T201 -- CLI output, not application logging
        f"reclaim scan: {stats.entries_total} entries under {stats.root} "
        f"({stats.dirs_visited} dirs visited, {stats.files_written} written, "
        f"{stats.files_unchanged} unchanged, {stats.files_pruned} pruned, "
        f"{stats.skipped_unreadable_count} skipped/unreadable, "
        f"{stats.guarded_stat_count} guarded-path stat, {stats.fast_stat_count} fast-path stat) "
        f"in {stats.elapsed_seconds:.2f}s"
    )
    # D12: a skip is a real permission/IO failure (long-path-only failures no longer land here
    # at all -- see reclaim.scanner's D12 module note), so it's surfaced loudly here rather than
    # only ever existing in a structlog line nobody reads.
    if stats.skipped_unreadable_paths:
        sample = "; ".join(stats.skipped_unreadable_paths)
        sample_count = len(stats.skipped_unreadable_paths)
        suffix = ", ..." if stats.skipped_unreadable_count > sample_count else ""
        print(  # noqa: T201 -- CLI output, not application logging
            f"reclaim scan: skipped/unreadable (first {sample_count} of "
            f"{stats.skipped_unreadable_count}): {sample}{suffix}"
        )
    # P0-5: what fraction of the skipped bytes above is actually known vs. genuinely unknown --
    # printed unconditionally alongside the skip count itself so "0 skipped" and "N skipped, all
    # of unknown size" are never conflated by a reader skimming just the first line.
    if stats.skipped_unreadable_count:
        print(  # noqa: T201 -- CLI output, not application logging
            f"reclaim scan: inaccessible-path size accounting: "
            f"{stats.inaccessible_known_bytes} bytes known (best-effort estimate), "
            f"{stats.inaccessible_unknown_count} path(s) with no size estimate at all "
            "-- see `reclaim reconcile` for how this compares to real disk usage."
        )
    return 0


def _run_index_prune(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(  # noqa: T201
            f"reclaim index-prune: no index found at {args.db} -- nothing to prune.",
            file=sys.stderr,
        )
        return 1
    if args.vacuum and not args.apply:
        print("reclaim index-prune: --vacuum requires --apply.", file=sys.stderr)  # noqa: T201
        return 2
    roots = [Path(root).as_posix() for root in args.root] if args.root else None
    size_before = args.db.stat().st_size
    try:
        with ScanIndex(args.db) as index:
            report = prune_dead_rows(index, apply=args.apply, root_prefixes=roots, deep=args.deep)
            if args.apply and report.dead_rows:
                # Same reason a scan refreshes them: planner statistics describe the table as
                # it was; a large delete changes row counts per prefix.
                index.refresh_planner_stats()
            if args.apply and args.vacuum:
                index.vacuum()
    except sqlite3.OperationalError as exc:
        print(f"reclaim index-prune: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    verb = "removed" if args.apply else "would remove (dry run, use --apply)"
    print(  # noqa: T201 -- CLI output, not application logging
        f"reclaim index-prune: {verb} {report.dead_rows} of {report.rows_examined} rows "
        f"({report.dead_bytes} bytes); {report.dirs_checked} directories checked, "
        f"{report.dirs_missing} missing; {report.unknown_rows_kept} rows kept because their "
        f"existence could not be verified; {report.seconds:.1f}s"
    )
    for prefix, count in report.dead_by_prefix.most_common(10):
        print(f"reclaim index-prune:   {count:>9} dead rows under {prefix}")  # noqa: T201
    if args.apply:
        print(  # noqa: T201
            f"reclaim index-prune: index file {size_before} -> {args.db.stat().st_size} bytes"
            + ("" if args.vacuum else " (pass --vacuum to return freed pages to the OS)")
        )
    return 0


def _run_reconcile(args: argparse.Namespace) -> int:
    if not args.db.exists():
        print(  # noqa: T201
            f"reclaim reconcile: no index found at {args.db} -- run `reclaim scan` first.",
            file=sys.stderr,
        )
        return 1
    try:
        with ScanIndex(args.db) as index:
            report = compute_disk_reconciliation(index, args.volume)
    except NotAVolumeRootError as exc:
        print(f"reclaim reconcile: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    print(  # noqa: T201 -- CLI output, not application logging
        f"reclaim reconcile: volume={report.volume} "
        f"indexed_bytes={report.indexed_bytes} "
        f"inaccessible_known_bytes={report.inaccessible_known_bytes} "
        f"({report.inaccessible_path_count} inaccessible path(s), "
        f"{report.inaccessible_unknown_count} with no size estimate) "
        f"reported_total_bytes={report.reported_total_bytes} "
        f"volume_used_bytes={report.volume_used_bytes} "
        f"delta_bytes={report.delta_bytes} delta_pct={report.delta_pct:.2f}%"
    )
    if report.inaccessible_unknown_count:
        print(  # noqa: T201
            f"reclaim reconcile: {report.inaccessible_unknown_count} inaccessible path(s) have "
            "no size estimate at all -- they contribute an unknown share of any remaining delta "
            "above, on top of the known inaccessible bytes already folded into "
            "reported_total_bytes."
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


# --- Progress feedback + pre-apply time estimate (fix/apply-progress-feedback) -----------------
#
# ADR-0026's measured ~9x per-item fsync cost turns a large apply/restore/purge into a
# multi-minute operation with no visible feedback — a real hazard: it looks hung, which makes a
# frustrated user more likely to kill the process mid-batch (exactly the scenario the crash-safe
# manifest exists to survive, but still worth heading off with real information instead of
# silence). This section wires `executor.ProgressCallback`/`estimate_batch_seconds` into a CLI
# user actually watching the terminal.


def _cli_progress_printer(label: str) -> ProgressCallback:
    """Interval-gated (never per-item — see `executor._HEARTBEAT_INTERVAL_SECONDS`) stdout
    heartbeat for a CLI user watching the terminal during a long `apply`/`purge`/`undo` run —
    structlog's own `executor.*_progress`/`purge.progress` line at the same cadence is captured
    for operators/log tooling, but isn't necessarily visible on a plain terminal, so this prints
    directly too, mirroring `_run_scan`'s existing `print(f"reclaim scan: ...")` convention."""

    def _print_progress(items_processed: int, items_total: int, current_category: str) -> None:
        print(  # noqa: T201 -- CLI output, not application logging
            f"reclaim {label}: {items_processed:,}/{items_total:,} item(s) processed "
            f"(current category: {current_category})"
        )

    return _print_progress


def _print_batch_duration_warning(label: str, item_count: int) -> None:
    """Pre-apply/-purge/-undo time estimate, derived from ADR-0026's real measured per-item
    fsync cost (`executor.estimate_batch_seconds` — never a guessed number), shown only above
    `executor.should_warn_about_batch_duration`'s threshold (a handful of items doesn't need
    one). Purely informational, not a second typed-confirmation gate: `--apply`/`undo`'s own
    batch_id argument are already this CLI's "are you sure" gate (mirroring the dry-run-by-
    default design), and an interactive prompt here would break non-interactive/scripted use —
    unlike power-mode's typed confirmation (`reclaim mode power --confirm ...`), which guards a
    structural safety-mode transition, not a single already-explicit apply/restore/purge
    invocation. Also states the interrupt-safety guarantee explicitly (verified for a graceful
    Ctrl-C specifically, not just a hard crash — see tests/test_recovery.py's
    `test_apply_keyboard_interrupt_*` tests): stopping is safe, nothing is lost, `reclaim
    recover` reconciles it afterward.
    """
    if not should_warn_about_batch_duration(item_count):
        return
    seconds = estimate_batch_seconds(item_count)
    minutes = seconds / 60.0
    time_phrase = "under a minute" if minutes < 1 else f"approximately {minutes:.1f} minute(s)"
    print(  # noqa: T201
        f"reclaim {label}: about to process {item_count:,} item(s) — at a measured ~8ms/item "
        f"durability cost (ADR-0026), this may take {time_phrase}. It is SAFE to interrupt "
        "(Ctrl-C) or close this terminal at any point during this: nothing is lost, whether "
        "quarantined-but-not-yet-recorded or genuinely mid-move. Run 'reclaim recover' "
        "afterward to reconcile anything interrupted mid-item."
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
    config = _load_config_or_none("apply", config_path, mode=current_mode(mode_log), effective=True)
    if config is None:
        return 1

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
    if args.apply:
        _print_batch_duration_warning("apply", len(selected))
    try:
        # P0-K1a/M1: a fresh `ScanIndex` opened right here (the earlier `with ScanIndex(...)`
        # above, used for candidate generation, has already closed by this point) so
        # `apply_batch`'s full-subtree re-walk has the SAME persisted scan data to re-verify
        # irreversible directory candidates against.
        with ScanIndex(args.db) as apply_scan_index:
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
                direct_delete_entry_count_guard=config.safety.direct_delete_entry_count_guard,
                on_progress=_cli_progress_printer("apply"),
                scan_index=apply_scan_index,
                # AE1: defense-in-depth — `selected` above is already filtered to `_under_root(
                # c.path, root)`, so this never restricts anything further here; it exists so the
                # apply choke point itself never trusts an already-filtered caller's list without
                # re-checking, the same two-layer posture the direct-delete safety re-check above
                # already uses. `Path.home()` is included too since `root` (the CLI's own
                # explicit `--path`) may legitimately be a subdirectory of it, not the whole home
                # tree itself.
                allowed_roots=(Path.home(), root),
            )
    except (SafetyInvariantError, SafeModeViolationError) as exc:
        print(f"reclaim apply: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(  # noqa: T201
        f"reclaim apply [{mode}] batch={report.batch_id} method={report.method} "
        f"processed={report.files_processed} succeeded={report.files_succeeded} "
        f"failed={report.files_failed} bytes_freed={report.bytes_freed} "
        f"bytes_moved={report.bytes_moved}"
    )
    if report.bytes_moved > 0:
        # No disk space is freed by a Recycle Bin / vault move until the bin is emptied / the
        # vault purged -- say so in the output itself rather than leaving it to inference.
        where = "Recycle Bin" if report.method == "recycle_bin" else "vault"
        print(  # noqa: T201
            f"  bytes_moved={report.bytes_moved} moved to {where}, not yet freed "
            f"(space is released when the {where} is emptied)"
        )
    if report.disk_free_delta_bytes is not None:
        print(  # noqa: T201
            f"reclaim apply: disk free before={report.disk_free_before_bytes} "
            f"after={report.disk_free_after_bytes} delta={report.disk_free_delta_bytes}"
        )
    if report.synchronously_purged_count > 0:
        # ADR-0032: an entry-count/size-guard-downgraded, rebuildable candidate was vaulted
        # (M1's re-walk skipped, only the cheap top-level check applied) and then immediately
        # purged back out within this same apply — "vault" in the method column above never
        # meant "still sitting in the vault" for these specific items.
        print(  # noqa: T201
            f"  synchronously purged (guard-downgraded, rebuildable, freed immediately): "
            f"count={report.synchronously_purged_count} "
            f"bytes={report.bytes_synchronously_purged}"
        )
    for category, breakdown in sorted(report.category_breakdown.items()):
        print(  # noqa: T201
            f"  {category}: count={breakdown.count} bytes_freed={breakdown.bytes_freed} "
            f"bytes_moved={breakdown.bytes_moved}"
        )
    _print_duplicate_reclaim_estimate(selected)
    _print_top_n_largest(selected)
    _print_hash_skips(hash_skips)
    if materiality is not None:
        _print_materiality_exclusion(materiality, min_reclaim_bytes=min_reclaim_bytes)
    for item in report.items:
        if item.skip_reason is not None:
            # A pre-flight skip was never attempted (`error` is None) -- name the reason instead
            # of printing a bare "FAILED: <path> — None".
            print(  # noqa: T201
                f"  SKIPPED: {item.path} — {item.skip_reason}", file=sys.stderr
            )
        elif not item.succeeded:
            print(f"  FAILED: {item.path} — {item.error}", file=sys.stderr)  # noqa: T201
    return 0 if report.files_failed == 0 else 1


def _run_check_disk_space(args: argparse.Namespace) -> int:
    # Imports deferred to inside the function, same reasoning as `_run_serve`'s deferred uvicorn
    # import: `reclaim.notifications` is only needed for this one subcommand, so `scan`/`apply`/
    # `undo`/etc. never pay its (small, but non-zero) import cost.
    from reclaim.notifications import (
        DEFAULT_STATE_PATH,
        apply_snooze,
        check_disk_space,
        record_notified,
        send_disk_space_toast,
    )

    config_path: Path = args.config
    config = _load_config_or_none("check-disk-space", config_path)
    if config is None:
        return 1
    state_path = args.state if args.state is not None else DEFAULT_STATE_PATH

    if args.apply_snooze:
        apply_snooze(state_path, snooze_days=config.notifications.snooze_days)
        print(  # noqa: T201
            f"reclaim check-disk-space: snoozed for {config.notifications.snooze_days} day(s)"
        )
        return 0

    result = check_disk_space(config.notifications, state_path=state_path)
    toast_note = ""
    if result.should_notify:
        # Debounce only once Windows accepted the toast: a refused one retries on the next run.
        if send_disk_space_toast(result):
            record_notified(state_path)
            toast_note = " toast=sent"
        else:
            toast_note = " toast=not_delivered"
    print(  # noqa: T201
        f"reclaim check-disk-space: status={result.status} reason={result.reason} "
        f"percent_used={result.percent_used} threshold={result.threshold_percent}{toast_note}"
    )
    return 0


def _record_autoclean_state(
    previous: autoclean_state.AutoCleanState,
    plan: autoclean_state.ScheduledRunPlan,
    response: object,
) -> None:
    """Persists what the scheduled run learned (last full run, tools still in use). A failure to
    write is logged, never fatal: the worst case is one extra full run, which is safe."""
    items = getattr(response, "items", [])
    native = [(item.key, item.status) for item in items if item.kind == "native_command"]
    new_state = autoclean_state.state_after_run(
        previous,
        plan,
        native,
        [spec.key for spec in regenerable.NATIVE_TOOLS],
        autoclean_state.utc_now(),
    )
    try:
        autoclean_state.write_state(new_state, autoclean_state.default_state_path())
    except OSError as exc:
        print(f"reclaim auto-clean: could not save run state: {exc}", file=sys.stderr)  # noqa: T201


def _auto_clean_status_json(
    args: argparse.Namespace, status: str, reason: str, error_type: str | None = None
) -> None:
    """Every path of an `auto-clean --json` run that starts executing the command writes exactly
    one JSON document to stdout (argparse usage errors and `--help` are printed by argparse and
    are not JSON; KeyboardInterrupt/SystemExit are deliberately not caught). Paths that never
    reach the run's own response write this minimal, schema-stable object instead: `status`
    ("skipped" | "error"; `--reconcile-task` adds "ok"), `reason` (autoclean_disabled |
    nothing_to_do | busy | config_invalid | elevated | run_failed; `--reconcile-task` adds
    task_registered), `applied` (always false -- nothing ran) and, for exceptions,
    `error_type` (the exception class name only: the text may hold paths, so it stays on
    stderr). The normal-path document is the `RegenerableCleanResponse` and has no `status` key.
    Human text goes to stderr."""
    if not args.json:
        return
    doc: dict[str, object] = {"status": status, "reason": reason, "applied": False}
    if error_type is not None:
        doc["error_type"] = error_type
    print(json.dumps(doc))  # noqa: T201
    args.json_emitted = True


def _run_reconcile_autoclean_task(
    args: argparse.Namespace,
    *,
    exe_path: Path | None = None,
    runner: autoclean_schedule.SchtasksRunner | None = None,
    diag_log_path: Path | None = None,
) -> int:
    """Outer wrapper (same contract as `_run_auto_clean`): with `--json`, an exception that
    escapes before a document was written becomes the `run_failed` error document (exit 1, full
    text on stderr) instead of an empty stdout. Without `--json` the traceback behaviour stays."""
    try:
        return _reconcile_autoclean_task_inner(
            args, exe_path=exe_path, runner=runner, diag_log_path=diag_log_path
        )
    except Exception as exc:
        if not args.json or getattr(args, "json_emitted", False):
            raise
        print(f"reclaim auto-clean: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "run_failed", type(exc).__name__)
        return 1


def _reconcile_autoclean_task_inner(
    args: argparse.Namespace,
    *,
    exe_path: Path | None,
    runner: autoclean_schedule.SchtasksRunner | None,
    diag_log_path: Path | None,
) -> int:
    """`auto-clean --reconcile-task`: makes an existing install's task match the current
    definition after an upgrade (ADR-0034 'Upgrade path'). Enabled -> `register_task` (schtasks
    `/f` overwrite, so an old single-trigger task gains the logon trigger). Disabled -> nothing at
    all, never a new task. Source/dev run -> a message and exit 0 (nothing to schedule is not a
    failure). A real failure prints the actionable error and returns 1. With `--json`, human text
    goes to stderr and `_auto_clean_status_json` writes the one stdout document."""
    out = sys.stderr if args.json else sys.stdout
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim auto-clean: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "elevated", type(exc).__name__)
        return 1
    config = _load_config_or_none("auto-clean", args.config)
    if config is None:
        _auto_clean_status_json(args, "error", "config_invalid")
        return 1
    if not config.autoclean.enabled:
        print(
            "reclaim auto-clean: nothing to do -- weekly auto-clean is off in config.toml "
            "([autoclean] enabled = false); no task was created or changed.",
            file=out,
        )
        _auto_clean_status_json(args, "skipped", "autoclean_disabled")
        return 0
    try:
        name = autoclean_schedule.register_task(
            exe_path=exe_path,
            # Resolved at call time (not a default argument) so tests can substitute the seam.
            runner=runner if runner is not None else autoclean_schedule.run_schtasks,
            diag_log_path=diag_log_path,
        )
    except autoclean_schedule.NotAnInstalledBuildError as exc:
        print(f"reclaim auto-clean: nothing to do -- {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "skipped", "nothing_to_do")
        return 0
    except (autoclean_schedule.AutoCleanScheduleError, OSError) as exc:
        print(f"reclaim auto-clean: could not update the weekly task: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "run_failed", type(exc).__name__)
        return 1
    print(f"reclaim auto-clean: weekly task '{name}' is up to date.", file=out)
    _auto_clean_status_json(args, "ok", "task_registered")
    return 0


def _run_auto_clean(args: argparse.Namespace) -> int:
    """Outer wrapper: any Exception that escapes before a JSON document was written becomes the
    `run_failed` error document (exit 1, full text on stderr) instead of an empty stdout."""
    try:
        return _run_auto_clean_inner(args)
    except Exception as exc:
        if not args.json or getattr(args, "json_emitted", False):
            raise  # not --json, or the document is already out: keep the traceback behaviour
        print(f"reclaim auto-clean: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        # Literal fallback: this path must not be able to raise (the class name is a plain str).
        name = type(exc).__name__
        safe = name if name.isidentifier() else "Exception"
        print(  # noqa: T201
            '{"status": "error", "reason": "run_failed", "applied": false, '
            f'"error_type": "{safe}"}}'
        )
        return 1


def _run_auto_clean_inner(args: argparse.Namespace) -> int:
    # Deferred imports, same reasoning as `_run_check_disk_space`: only this subcommand needs the
    # API service module (shared with the dashboard's one-click clean so the two report the exact
    # same shape and write the same audit log) and the toast stack.
    from reclaim.api import service
    from reclaim.api.schemas import format_bytes
    from reclaim.notifications import send_autoclean_toast

    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim auto-clean: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "elevated", type(exc).__name__)
        return 1
    config = _load_config_or_none("auto-clean", args.config)
    if config is None:
        _auto_clean_status_json(args, "error", "config_invalid")
        return 1
    if args.scheduled and not config.autoclean.enabled:
        # Belt and braces: the toggle removes the task; a stale copy must still be inert.
        print(
            "reclaim auto-clean: skipped -- weekly auto-clean is turned off in config.toml "
            "([autoclean] enabled = false); nothing was cleaned.",
            file=sys.stderr if args.json else sys.stdout,
        )
        _auto_clean_status_json(args, "skipped", "autoclean_disabled")
        return 0

    apply: bool = args.apply
    # ADR-0034 addendum "pytest temp": deletion only on the config opt-in; the CLI flag can only
    # ADD the category to a dry run, so it can never widen what an --apply run deletes.
    if args.include_pytest_temp and apply and not config.regenerable.pytest_temp:
        print(  # noqa: T201
            "reclaim auto-clean: --include-pytest-temp is dry-run only; to delete pytest temp "
            "folders set [regenerable] pytest_temp = true in config.toml.",
            file=sys.stderr,
        )
        return 2
    pytest_temp: regenerable.PytestTempMode = "off"
    if config.regenerable.pytest_temp:
        pytest_temp = "delete"
    elif args.include_pytest_temp:
        pytest_temp = "report"
    # ADR-0034 addendum: the task also fires shortly after sign-in. A scheduled apply run asks the
    # state file whether to run the whole tier, only the tools left in use last time, or nothing.
    scheduled_state: autoclean_state.AutoCleanState | None = None
    plan: autoclean_state.ScheduledRunPlan | None = None
    if args.scheduled and apply:
        known_tools = [spec.key for spec in regenerable.NATIVE_TOOLS]
        scheduled_state = autoclean_state.read_state(
            autoclean_state.default_state_path(), known_tools=known_tools
        )
        plan = autoclean_state.decide_scheduled_run(scheduled_state, autoclean_state.utc_now())
        if plan.mode == "noop":
            # No toast: a sign-in with nothing to do must be invisible.
            print(  # noqa: T201
                f"reclaim auto-clean: nothing to do -- {plan.reason}.", file=sys.stderr
            )
            _auto_clean_status_json(args, "skipped", "nothing_to_do")
            return 0
        # stderr: stdout stays the machine-readable `--json` document.
        print(  # noqa: T201
            f"reclaim auto-clean: scheduled run mode={plan.mode} ({plan.reason}).",
            file=sys.stderr,
        )
    try:
        # ADR-0039: the weekly task reads the installed config.toml, so its exclusions apply.
        response = service.regenerable_clean_response(
            apply=apply,
            excluded_patterns=exclusion_patterns(config),
            only_keys=plan.only_keys if plan is not None else None,
            pytest_temp=pytest_temp,
        )
    except service.RegenerableCleanBusyError as exc:
        print(f"reclaim auto-clean: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "busy", type(exc).__name__)
        return 1
    except Exception as exc:
        # The run itself crashed (per-item failures never reach here -- they are reported below).
        print(f"reclaim auto-clean: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)  # noqa: T201
        _auto_clean_status_json(args, "error", "run_failed", type(exc).__name__)
        return 1

    if args.json:
        document = response.model_dump_json(indent=2)  # serialize first: a failure prints nothing
        print(document)  # noqa: T201
        args.json_emitted = True
    else:
        for item in response.items:
            print(  # noqa: T201
                f"{item.status:<24} {item.bytes_removed_human:>9}  {item.label}"
                + (f"  -- {item.detail}" if item.detail else "")
            )
        for entry in response.excluded:
            print(f"skipped_excluded: {entry}")  # noqa: T201
        print(  # noqa: T201
            f"excluded: {len(response.excluded)}, excluded_applied: {response.excluded_applied}"
        )
        verb = "Total freed" if apply else "Total that would be freed"
        print(f"{verb}: {response.bytes_removed_human} ({response.bytes_removed} bytes)")  # noqa: T201
        if apply and response.disk_free_before_bytes is not None:
            after = response.disk_free_after_bytes
            delta = response.disk_free_delta_bytes
            print(  # noqa: T201
                f"disk free before {format_bytes(response.disk_free_before_bytes)}, "
                f"after {format_bytes(after) if after is not None else 'n/a'}, "
                f"delta {format_bytes(delta) if delta is not None else 'n/a'}"
            )
        used = response.percent_used_after
        if apply:
            suffix = f", C: now {used:.0f}% used" if used is not None else ""
            print(f"Freed {response.bytes_removed_human}{suffix}")  # noqa: T201
        else:
            print(  # noqa: T201
                f"Would free {response.bytes_removed_human} (dry run -- pass --apply to clean)"
            )

    if response.excluded_applied > 0:
        # Mechanical form of "no excluded project appeared among applied paths": a
        # non-zero count means the skip logic itself failed -- never report success.
        print(  # noqa: T201
            f"reclaim auto-clean: INVARIANT VIOLATION excluded_applied={response.excluded_applied}",
            file=sys.stderr,
        )
        return 1

    # Fail closed: only a run that passed the invariant above may mark the full run done / clear
    # pending tools.
    if plan is not None and scheduled_state is not None:
        _record_autoclean_state(scheduled_state, plan, response)

    if apply and args.notify and (response.bytes_removed > 0 or response.files_skipped_in_use > 0):
        send_autoclean_toast(
            response.bytes_removed, response.percent_used_after, response.files_skipped_in_use
        )
    return 0


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
    config = _load_config_or_none("serve", config_path)
    if config is None:
        return 1
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    first_run_state = (
        args.first_run_state if args.first_run_state is not None else DEFAULT_FIRST_RUN_STATE_PATH
    )
    log_path = args.log_path if args.log_path is not None else DEFAULT_LOG_PATH
    app = create_app(
        db_path=args.db,
        config=config,
        config_path=config_path,
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
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    except OSError as exc:
        # uvicorn/the socket layer raises a raw OSError (WinError 10048 on Windows,
        # errno.EADDRINUSE on POSIX) for a port already in use, and a raw WinError
        # 10013/errno.EACCES for a privileged port without permission — both would
        # otherwise surface as an unhandled traceback to the user. Distinguish the two so
        # the message tells the user what to actually do next (rule 104: errors are part
        # of the API).
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            print(  # noqa: T201
                f"reclaim serve: port {args.port} is already in use — stop whatever is "
                "using it, or pass --port to pick another.",
                file=sys.stderr,
            )
            return 1
        if exc.errno == errno.EACCES or getattr(exc, "winerror", None) == 10013:
            print(  # noqa: T201
                f"reclaim serve: permission denied binding to port {args.port} — "
                "pick a port above 1024, or run with the privileges that port requires.",
                file=sys.stderr,
            )
            return 1
        raise
    return 0


def _run_dashboard(args: argparse.Namespace) -> int:
    return _run_serve(args, open_browser=True)


def _run_mcp_serve(args: argparse.Namespace) -> int:
    """`reclaim mcp-serve` -- the R7 MCP control surface, an AI agent's own scan/review/delete
    entry point (see `reclaim.mcp`'s module docstring for the safety model). Same
    not-elevated guard `serve`/`apply`/`undo`/`purge` already enforce; unlike those,
    `reclaim.mcp` never touches `reclaim.executor`/`send2trash` directly itself, but the
    `AppState` it's handed is the exact one `reclaim.api.service.mcp_execute_delete` calls
    `apply_batch` through, so the same "never run elevated" invariant applies here too.

    Imports deferred to inside the function, same reasoning `_run_serve` already documents for
    uvicorn/FastAPI: `scan`/`apply`/`undo`/`serve` (and every existing test importing this
    module) must never pay the `mcp` package's import cost.
    """
    try:
        assert_not_elevated()
    except ElevatedProcessError as exc:
        print(f"reclaim mcp-serve: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    from reclaim.mcp.server import build_state, run_mcp_server

    config_path: Path = args.config
    # Raw config, deliberately -- same reasoning `_run_serve` documents: `AppState.
    # effective_config` resolves the live mode fresh on every tool call, not once at process
    # startup.
    config = _load_config_or_none("mcp-serve", config_path)
    if config is None:
        return 1
    mode_log: Path = args.mode_log if args.mode_log is not None else DEFAULT_MODE_LOG_PATH
    first_run_state = (
        args.first_run_state if args.first_run_state is not None else DEFAULT_FIRST_RUN_STATE_PATH
    )
    log_path = args.log_path if args.log_path is not None else DEFAULT_LOG_PATH
    state = build_state(
        db_path=args.db,
        config=config,
        vault_dir=args.vault_dir,
        manifest_path=args.manifest,
        mode_log_path=mode_log,
        first_run_state_path=first_run_state,
        log_path=log_path,
    )
    print(  # noqa: T201
        "reclaim mcp-serve: MCP control surface starting over stdio (Ctrl+C to stop)",
        file=sys.stderr,
    )
    run_mcp_server(state)
    return 0


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
    config = _load_config_or_none("undo", config_path)
    if config is None:
        return 1
    safety = SafetyValidator(config)

    # 'undo' has no dry-run concept (a restore is always real) — the pre-restore time estimate
    # is scoped to the batch's actual vault-entry count (the only entries a restore ever moves;
    # see `executor.resolve_restorable_entries`'s docstring), not the whole batch's item count.
    resolved_manifest_path = args.manifest if args.manifest is not None else DEFAULT_MANIFEST_PATH
    vault_entry_count = sum(
        1
        for entry in fold_latest_manifest_entries(resolved_manifest_path)
        if entry.batch_id == args.batch_id and entry.method == "vault"
    )
    _print_batch_duration_warning("undo", vault_entry_count)

    try:
        report = restore_batch(
            args.batch_id,
            manifest_path=args.manifest,
            vault_dir=args.vault_dir,
            safety=safety,
            on_progress=_cli_progress_printer("undo"),
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
    config = _load_config_or_none("purge", config_path, mode=live_mode, effective=True)
    if config is None:
        return 1
    safety = SafetyValidator(config)

    if args.apply:
        resolved_manifest_path = (
            args.manifest if args.manifest is not None else DEFAULT_MANIFEST_PATH
        )
        eligible_count = len(purge_eligible_entries(resolved_manifest_path, time.time()))
        _print_batch_duration_warning("purge", eligible_count)

    try:
        report = purge_expired(
            apply=args.apply,
            manifest_path=args.manifest,
            vault_dir=args.vault_dir,
            safety=safety,
            only_rebuildable=args.rebuildable_only,
            mode=live_mode,
            on_progress=_cli_progress_printer("purge"),
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


def _run_recover(args: argparse.Namespace) -> int:
    from reclaim.recovery import compute_reconciliation, reconcile_manifest

    fn = reconcile_manifest if args.apply else compute_reconciliation
    report = fn(manifest_path=args.manifest, vault_dir=args.vault_dir)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(  # noqa: T201
        f"reclaim recover [{mode}] scanned_intents={report.scanned_intents} "
        f"already_resolved={report.already_resolved} reconciled={len(report.reconciled)}"
    )
    for item in report.reconciled:
        print(  # noqa: T201
            f"  {item.outcome.upper()}: {item.operation} {item.original_path} — {item.detail}"
        )
    needs_review = [item for item in report.reconciled if item.outcome == "needs_review"]
    if needs_review:
        print(  # noqa: T201
            f"reclaim recover: {len(needs_review)} item(s) need manual review — see NEEDS_REVIEW "
            "lines above",
            file=sys.stderr,
        )
    return 0 if not needs_review else 1


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
    if args.command == "reconcile":
        return _run_reconcile(args)
    if args.command == "index-prune":
        return _run_index_prune(args)
    if args.command == "apply":
        return _run_apply(args)
    if args.command == "undo":
        return _run_undo(args)
    if args.command == "purge":
        return _run_purge(args)
    if args.command == "mode":
        return _run_mode(args)
    if args.command == "recover":
        return _run_recover(args)
    if args.command == "check-disk-space":
        return _run_check_disk_space(args)
    if args.command == "auto-clean":
        if args.reconcile_task:
            return _run_reconcile_autoclean_task(args)
        return _run_auto_clean(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
    if args.command == "mcp-serve":
        return _run_mcp_serve(args)
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
