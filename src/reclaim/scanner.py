from __future__ import annotations

import errno
import functools
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path

import structlog

from reclaim.drives import is_network_drive
from reclaim.index import ScanIndex, StoredStat, is_unchanged
from reclaim.models import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileRecord,
)

logger = structlog.get_logger(__name__)

# Directory-mtime-based subtree skipping was considered and deliberately not implemented:
# NTFS only updates a directory's own mtime on direct-listing changes (add/remove/rename of
# an immediate child), not when a file *inside* an unchanged-looking subdirectory has its
# content modified in place. Trusting an unchanged directory mtime to mean "nothing changed
# below here" would silently miss content edits in a tool whose downstream stages delete
# files — that risk isn't worth the perf win, so every directory is always re-listed via
# os.scandir, and the per-file (size, mtime) compare (index.is_unchanged) is the only skip
# mechanism. Confidence: high that this is the safe choice; low that it's the fastest possible
# one — acceptable per the brief ("if in doubt, walk it").

_ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_CLOUD_ROOT_FOLDER_PREFIXES = ("onedrive", "dropbox", "google drive")

# --- D12: long-path-safe scan walk ------------------------------------------------------------
#
# ADR-0004 gave the vault/move path in `executor.py` `\\?\`-prefixed, MAX_PATH-safe filesystem
# calls, but the SCAN path itself never got the same treatment: `build_record`/`_walk_subtree`
# stat'd real filesystem entries via bare `Path`/`os.scandir` calls. On a real >260-char directory
# (confirmed via a live audit fixture: a 9-directory-deep, 604-char tree), the stat call fails with
# `WinError 3`, `build_record` returns `None` for that entry — and because a directory's `None`
# return means it's never pushed onto the walk stack, its ENTIRE subtree silently never gets
# visited. The scan still reports success (exit 0, plausible-looking counts): disk usage is
# silently under-reported for any real deeply-nested user tree, with nothing anywhere surfacing
# that it happened. `long_path` (moved here from `executor.py`, which re-exports it for backward
# compatibility — see that module) is the same primitive ADR-0004 already trusts, applied
# unconditionally to every scandir/stat call in this module's walk, matching executor.py's own
# "always prefix, it's idempotent and cheap" convention rather than only prefixing paths already
# suspected to be long. Genuinely unreadable paths (permission denied, a real I/O error) are no
# longer silently dropped either: `build_record`/`_walk_subtree`/`scan_tree` now accumulate every
# skip as a `SkippedPath` that flows into `ScanStats.skipped_unreadable_count`/
# `skipped_unreadable_paths` — visible in both the CLI's printed output and the dashboard's
# `/api/summary` view, not just logged and forgotten.
_LONG_PATH_PREFIX = "\\\\?\\"


def long_path(path: Path) -> str:
    r"""Returns an absolute, `\\?\`-prefixed path string so the Win32 APIs behind `os`/`shutil`
    bypass the legacy 260-character MAX_PATH limit (this tool targets Windows/NTFS exclusively —
    see `pytestmark` in the test suite).

    `\\?\` disables the normal path parser's `.`/`..` and forward-slash handling entirely, so the
    string must already be a fully-normalized, all-backslash absolute path before the prefix is
    added — `str(Path(...))` (not raw string concatenation) guarantees that on Windows. Idempotent:
    a path already carrying the prefix is returned unchanged. UNC paths get the `\\?\UNC\` form;
    drive-letter paths get a plain `\\?\` prefix.
    """
    raw = str(Path(path).absolute())
    if raw.startswith(_LONG_PATH_PREFIX):
        return raw
    if raw.startswith("\\\\"):  # UNC: \\server\share\... -> \\?\UNC\server\share\...
        return _LONG_PATH_PREFIX + "UNC\\" + raw[2:]
    return _LONG_PATH_PREFIX + raw


@dataclass(frozen=True, slots=True)
class SkippedPath:
    r"""One filesystem entry `scan_tree` could not stat or list, and why.

    Surfaced (not just logged) via `ScanStats.skipped_unreadable_count`/
    `skipped_unreadable_paths` — before D12, a permission error or a genuine I/O fault on one
    directory silently dropped that directory's entire subtree from the scan with no visible
    trace anywhere in the scan's output. Every scandir/stat call in this module is now
    `\\?\`-prefixed (see `long_path`), so a `SkippedPath` means a real permission/IO problem, not
    merely "the path was long".
    """

    path: str
    error: str


# Cap on how many actual `SkippedPath.path` strings `ScanStats.skipped_unreadable_paths` carries —
# the full count is always exact, but a scan hitting a genuinely inaccessible root (e.g. an entire
# protected directory tree) could otherwise accumulate an unbounded sample list for no added value
# past the first handful.
_SKIPPED_PATHS_SAMPLE_LIMIT = 20

# --- Progress feedback (full-drive-scan-eta) ----------------------------------------------------
#
# SIMPLE mode's "scan my whole computer" action must never be a silent, unbounded-looking
# operation — this section gives both `count_entries_fast`'s fast pre-pass and `scan_tree` itself
# an interval-gated progress hook `api.service.run_scan` uses to compute and republish a live ETA
# while a scan (single-path or full-drive) is running.

_HEARTBEAT_INTERVAL_SECONDS = 5.0


def _due(*, last: float, now: float, interval: float) -> bool:
    """Pure predicate behind the progress-heartbeat gate — mirrors `executor._due`/`dedup._due`'s
    exact convention (this codebase's established interval-gated-logging/-callback pattern).
    Duplicated here rather than imported: `executor.py` imports FROM `scanner.py`
    (`GitRepoCache`, `build_record_for_path`), so importing `_due` back from `executor.py` would
    be circular, and `dedup.py` doesn't export its copy either — a 2-line pure predicate isn't
    worth introducing a new inter-module dependency for."""
    return (now - last) >= interval


CountProgressCallback = Callable[[int, float], None]
"""`(entries_counted_so_far, elapsed_seconds) -> None`. Optional, interval-gated (same
`_HEARTBEAT_INTERVAL_SECONDS`/`_due` cadence as everything else in this section) progress hook
for `count_entries_fast`'s own walk — a full-drive "estimating" pre-pass over a genuinely huge
drive can itself take several seconds, and this is what keeps THAT phase from looking stuck
too, not just the real scan that follows it."""


def count_entries_fast(
    root: Path,
    *,
    on_progress: CountProgressCallback | None = None,
) -> int:
    r"""Fast, stat-free entry count under `root` — the "quick sample" `api.service.run_scan`'s
    live ETA is derived from (full-drive-scan-eta). The real `scan_tree` does one real
    `os.stat()` per entry (hardlink identity via `st_ino`/`st_dev`), git-repo detection, and
    SQLite writes; this function does none of that — pure `os.scandir` recursion, counting every
    entry (files AND directories, matching `ScanStats.entries_total`'s own definition), which
    must be dramatically cheaper per-entry than a real scan for the two-phase estimate to be
    worth doing at all.

    Mirrors `scan_tree`'s own skip rules exactly, so the total this produces matches what a real
    `scan_tree(root, ...)` call will actually visit:
    - every `os.scandir` call is `\\?\`-prefixed via `long_path()` (D12) — a real >MAX_PATH
      subtree is counted, never silently dropped;
    - a reparse point (junction/symlink) is counted itself but never recursed into, detected via
      `entry.is_dir(follow_symlinks=False)` plus `entry.stat(follow_symlinks=False)
      .st_file_attributes`'s `FILE_ATTRIBUTE_REPARSE_POINT` bit — the same check `build_record`
      uses, just without `build_record`'s extra real `os.stat()` call for `st_ino`/`st_dev`
      (which this function never needs): on Windows, `DirEntry.stat(follow_symlinks=False)` is
      populated straight from the `FindNextFile` data `scandir` already collected, so this reads
      as a free, already-cached attribute check, not a second per-entry syscall;
    - a directory that can't be listed (permission error, genuine I/O fault) is skipped, not
      fatal — same tolerance `scan_tree`'s own walk has, just without `SkippedPath` accounting
      (this is a best-effort ESTIMATE, not the real inventory `ScanStats` reports).

    Iterative (explicit stack), not recursive, matching `_walk_subtree`'s own convention — avoids
    Python's recursion limit on a real deep tree.
    """
    start_time = time.monotonic()
    count = 0
    last_heartbeat = start_time
    stack = [root]
    while stack:
        current_dir = stack.pop()
        try:
            dir_entries = list(os.scandir(long_path(current_dir)))
        except OSError as exc:
            logger.warning("scan.count_dir_unreadable", path=str(current_dir), error=str(exc))
            continue

        for entry in dir_entries:
            try:
                is_dir_entry = entry.is_dir(follow_symlinks=False)
                is_reparse_point = bool(
                    entry.stat(follow_symlinks=False).st_file_attributes
                    & FILE_ATTRIBUTE_REPARSE_POINT
                )
            except OSError as exc:
                logger.warning(
                    "scan.count_entry_unreadable",
                    path=str(current_dir / entry.name),
                    error=str(exc),
                )
                continue

            count += 1
            if is_dir_entry and not is_reparse_point:
                stack.append(current_dir / entry.name)

            if on_progress is not None:
                now = time.monotonic()
                if _due(last=last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                    on_progress(count, now - start_time)
                    last_heartbeat = now
    return count


ScanProgressCallback = Callable[[int, int | None, float], None]
"""`(entries_processed_so_far, entries_estimated_total_or_None, elapsed_seconds) -> None`.
Optional hook threaded through `scan_tree`'s walk, interval-gated at the SAME
`_HEARTBEAT_INTERVAL_SECONDS` cadence as `count_entries_fast`'s own callback and
`executor.ProgressCallback`'s established convention (never per-entry — would be needlessly
expensive on a fast walk and would spam a caller). `entries_estimated_total` is whatever the
caller passed in via `scan_tree`'s own `entries_estimated_total` parameter (typically
`count_entries_fast`'s result from the preceding "estimating" phase) — `scan_tree` itself never
computes an ETA; that's `api.service.run_scan`'s job, kept out of this module so the scanner
stays a pure filesystem-walk primitive with no orchestration/business logic of its own."""


class _ProgressTracker:
    """Thread-safe accumulator behind `scan_tree`'s optional `on_progress` callback — shared
    across the `ThreadPoolExecutor` workers `scan_tree` fans out to (one per top-level
    directory), so `entries_processed_so_far` is a real, race-free total across every worker,
    not just one thread's own local count.

    A plain `threading.Lock` guarding a plain `int` counter (rather than a lock-free primitive)
    is simplest and sufficient here: the lock is only ever held for a cheap counter
    increment/due-check, never across the actual `on_progress(...)` call itself (which runs
    outside the lock, after it's released) — real callback invocations only happen once per
    `_HEARTBEAT_INTERVAL_SECONDS`, not per entry, so contention stays negligible even with many
    worker threads all calling `add()` constantly.
    """

    def __init__(
        self,
        on_progress: ScanProgressCallback | None,
        *,
        entries_estimated_total: int | None,
        start_time: float,
    ) -> None:
        self._on_progress = on_progress
        self._entries_estimated_total = entries_estimated_total
        self._start_time = start_time
        self._lock = threading.Lock()
        self._processed = 0
        self._last_heartbeat = start_time

    def add(self, n: int) -> None:
        if self._on_progress is None or n <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._processed += n
            if not _due(last=self._last_heartbeat, now=now, interval=_HEARTBEAT_INTERVAL_SECONDS):
                return
            self._last_heartbeat = now
            processed = self._processed
        self._on_progress(processed, self._entries_estimated_total, now - self._start_time)


class ScanDiskFullError(RuntimeError):
    """Raised by `scan_tree` when the index write at the end of a scan (`upsert_records`/
    `prune_missing`) fails because the disk holding the SQLite index is full (A5).

    The scan walk itself (`os.scandir`/`os.stat`) is read-only and never triggers ENOSPC — the
    only place this scan touches disk for a write is the two index-write calls at the end of
    `scan_tree`, so that's where this is caught, not scattered through the read-only walk.
    Distinct from a generic `OSError` so the CLI can print "disk is full, free up space and try
    again" instead of an uncaught traceback, matching `ElevatedProcessError`'s pattern.
    """


def _is_disk_full(exc: Exception) -> bool:
    """True if `exc` is the OS or SQLite manifestation of "no space left on the volume holding
    the index db". SQLite intercepts the underlying OS write failure itself and reports it as
    `sqlite3.OperationalError` (message text, no errno) rather than letting a raw `OSError`
    propagate through the C extension in the common case — checked by message here since that's
    the only signal `sqlite3` gives; a raw `OSError(errno.ENOSPC, ...)` is checked too in case a
    future/alternate DB-API path ever does let one through directly.
    """
    if isinstance(exc, OSError):
        return exc.errno == errno.ENOSPC
    if isinstance(exc, sqlite3.OperationalError):
        return "disk" in str(exc).lower() and "full" in str(exc).lower()
    return False


def is_cloud_sync_root(path: Path) -> bool:
    """Best-effort heuristic: is `path` a cloud-sync provider's root folder (OneDrive/Dropbox/
    Google Drive)? This is a soft signal, not an authoritative one — matched by env var or by
    folder-name convention, either of which a user could rename or a provider could change.
    Never treat this as equivalent to the `is_cloud_placeholder` attribute check, which is a
    real filesystem fact; label anything derived from this "heuristic" per spec principle 2.
    """
    for env_var in _ONEDRIVE_ENV_VARS:
        value = os.environ.get(env_var)
        if value and Path(value).resolve() == path.resolve():
            return True
    name_lower = path.name.lower()
    if any(name_lower.startswith(prefix) for prefix in _CLOUD_ROOT_FOLDER_PREFIXES):
        return True
    return (path / ".dropbox").exists()


@dataclass(frozen=True, slots=True)
class ScanStats:
    """Summary of one `scan_tree` run."""

    root: Path
    dirs_visited: int
    entries_total: int
    files_written: int
    files_unchanged: int
    files_pruned: int
    elapsed_seconds: float
    # D12: real count of every entry `scan_tree` could not stat/list (permission error, genuine
    # I/O fault) — see the `SkippedPath` docstring. `skipped_unreadable_paths` is a sample (first
    # `_SKIPPED_PATHS_SAMPLE_LIMIT`) of the actual paths, never the full list.
    skipped_unreadable_count: int
    skipped_unreadable_paths: tuple[str, ...]
    # 2026-07-30 telemetry addendum (see `_RiskCounter`): how many entries this run's real
    # `os.stat()` calls actually took the timeout-guarded path (reparse points, cloud
    # placeholders, or a network-mapped/UNC root) vs the fast unguarded path. Turns "why did
    # wall time only partly recover after risk-targeting the guard" into a measurement on every
    # real scan, not a one-off diagnostic A/B.
    guarded_stat_count: int
    fast_stat_count: int


class GitRepoCache:
    """Memoizes directory -> git repo root, and repo root -> clean status, for one scan
    worker. Scoped to a single top-level-directory worker rather than shared across threads:
    every directory under a given top-level directory is, by construction, only ever walked
    by that directory's own worker, so a thread-local cache carries zero correctness risk
    here (no repo can span two top-level directories of the same scan) while avoiding any
    lock contention between workers.
    """

    _UNSET = object()

    def __init__(self) -> None:
        self._repo_root_cache: dict[Path, Path | None] = {}
        self._clean_cache: dict[Path, bool] = {}

    def repo_root_for(self, search_start: Path) -> Path | None:
        """Walks upward from `search_start` looking for a `.git` directory, memoizing every
        directory visited along the way so sibling files/dirs resolve in O(1).

        Uses `long_path()`-prefixed `os.path.isdir` rather than `Path.is_dir()` (D12 follow-up):
        `Path.is_dir()` silently returns `False` for a path past Windows' 260-char MAX_PATH —
        it doesn't raise, it just never touches the filesystem — so a repo whose root itself
        sits past that limit would never be found walking up from a deeply-nested file, giving
        that file `git_repo_root=None` and silently bypassing `safety.py`'s in-repo deletion
        protection (`_builtin_deny` only blocks a candidate when `record.git_repo_root is not
        None`). Same failure shape `build_record`'s own MAX_PATH bug had before this branch's
        main fix — a silent `False`/`None`, never a loud error — just reached via `Path.is_dir()`
        instead of a raw stat call.
        """
        visited: list[Path] = []
        current = search_start
        while True:
            cached = self._repo_root_cache.get(current, self._UNSET)
            if cached is not self._UNSET:
                result: Path | None = cached  # type: ignore[assignment]
                break
            visited.append(current)
            if os.path.isdir(long_path(current / ".git")):  # noqa: PTH112 -- \\?\ str, not Path
                result = current
                break
            parent = current.parent
            if parent == current:
                result = None
                break
            current = parent
        for directory in visited:
            self._repo_root_cache[directory] = result
        return result

    def is_clean(self, repo_root: Path) -> bool:
        if repo_root in self._clean_cache:
            return self._clean_cache[repo_root]
        clean = _query_git_clean(repo_root)
        self._clean_cache[repo_root] = clean
        return clean


# Wave 1 finding #5 (2026-07-30 real-disk diagnosis): on a real developer machine, a large
# fraction of directories `GitRepoCache` finds a `.git` marker under are legitimately owned but
# still trip Git's "detected dubious ownership" safe.directory protection (CVE-2022-24765) when
# `git status` runs with a different effective identity/context than whoever created the repo —
# confirmed on the diagnostic run (dozens of `exit status 128` hits under real, ordinary repo
# roots the user actually owns). This already fails SAFE (`_query_git_clean` returns `False`,
# same conservative default as every other failure here) — the fix is purely about noise: at
# real-disk scale this was thousands of `warning`-level lines competing for the rotating log's
# fixed 30MB budget (`logging_config.py`) with genuinely actionable warnings.
_GIT_DUBIOUS_OWNERSHIP_MARKER = "detected dubious ownership"


def _query_git_clean(repo_root: Path) -> bool:
    """Runs `git status --porcelain` once for a repo root. Any failure (git missing, not a
    repo, timeout) is treated as not-clean — conservative, matching SafetyValidator's
    deny-by-default posture — and never crashes the scan.
    """
    git_exe = shutil.which("git")
    if git_exe is None:
        return False
    try:
        # Fixed argv; repo_root was discovered by walking the scan tree itself, not
        # supplied by external/untrusted input.
        result = subprocess.run(  # noqa: S603
            [git_exe, "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if _GIT_DUBIOUS_OWNERSHIP_MARKER in stderr.lower():
            logger.debug("scan.git_dubious_ownership", repo_root=str(repo_root))
        else:
            logger.warning("scan.git_status_failed", repo_root=str(repo_root), error=str(exc))
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("scan.git_status_failed", repo_root=str(repo_root), error=str(exc))
        return False
    return result.stdout.strip() == ""


# Wave 1 finding #3 (2026-07-30 real-disk diagnosis): `build_record`'s `os.stat()` call was the
# only per-entry syscall anywhere in the scan walk with no timeout guard at all — `dedup.py`'s
# hash reads have had one since real-disk validation showed a locked/pathological file must
# never wedge the whole pipeline (`_hash_with_guard`). A stalled `os.stat()` on one top-level
# directory couldn't previously be distinguished from a genuinely long scan; combined with the
# old `executor.map()`-based consumption (submission-order-blocking — see `scan_tree`'s
# `as_completed` rewrite below), it could silently stall the entire scan's visible output with
# no error ever raised. Same "submit to a small shared pool, abandon a timed-out thread rather
# than block the caller forever" pattern as `dedup._hash_with_guard` — Python has no
# cross-platform way to preempt a blocked syscall, so a timed-out thread leaks rather than dies,
# same trade-off `dedup.py` already accepts.
#
# First version of this guard routed EVERY entry through the timeout-guarded pool unconditionally
# — correct, but measured (real A/B, same machine) at 10,696 files/sec unguarded vs 6,323
# files/sec guarded, a ~41% throughput cost paid on every single local file for a guard that has
# never actually fired on either real-disk run. Risk-targeted instead: only entries that are
# actually plausible stat-hang candidates (a reparse point — the target could be anything, a
# broken/circular junction, a slow network path; a cloud placeholder — hydration risk; or any
# entry under a network-mapped/UNC root — an unresponsive server) route through the guard.
# Everything else calls `os.stat()` directly on the worker thread, at full unguarded speed.
_STAT_TIMEOUT_SECONDS = 30.0
_STAT_TIMEOUT_WORKERS = 8
_RISK_ATTRIBUTES = FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def _entry_is_risky(entry: os.DirEntry[str], *, force_guard: bool) -> bool:
    """True if `entry` is a plausible stat-hang candidate and should route through the
    timeout-guarded pool. `force_guard` (set once per scan by `scan_tree` via
    `is_network_drive(root)`, never recomputed per-entry) short-circuits every entry under a
    network-mapped/UNC root as risky without needing a per-entry check. Otherwise, checks the
    reparse-point/cloud-placeholder attribute bits via `entry.stat(follow_symlinks=False)` — on
    Windows this reads straight from the `FindNextFile` data `os.scandir` already collected (see
    `count_entries_fast`'s docstring: "a free, already-cached attribute check, not a second
    per-entry syscall"), so this check itself costs nothing extra. A `stat()` failure here (rare
    — the entry was just listed by scandir) fails toward `True`: if the cheap check itself can't
    tell, guard the real stat rather than assume it's safe."""
    if force_guard:
        return True
    try:
        attributes = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return True
    return bool(attributes & _RISK_ATTRIBUTES)


def _guarded_stat(stat_executor: ThreadPoolExecutor | None, path: str) -> os.stat_result:
    """`os.stat(path, follow_symlinks=False)`, optionally timeout-guarded via `stat_executor`.
    `stat_executor=None` (the default — used by `build_record_for_path`'s one-off single-path
    lookups, never a per-entry scan hot loop) falls back to a plain synchronous call, zero
    behavior change for that call site. `scan_tree` passes a real shared executor so every
    `build_record` call across its whole walk shares one bounded `_STAT_TIMEOUT_WORKERS`-sized
    pool, not one pool per top-level-directory worker."""
    if stat_executor is None:
        return os.stat(path, follow_symlinks=False)  # noqa: PTH116 -- \\?\ str, not Path
    future = stat_executor.submit(os.stat, path, follow_symlinks=False)
    return future.result(timeout=_STAT_TIMEOUT_SECONDS)


class _RiskCounter:
    """Thread-safe tally of guarded-vs-fast-path stat routing (2026-07-30 telemetry addendum):
    turns "why did wall time only partly recover after risk-targeting" from a guess into a
    measurement on every real scan from here on, reported via `ScanStats.guarded_stat_count`/
    `fast_stat_count` — legitimate guarded-path volume (real reparse points/cloud placeholders/
    network roots on the scanned machine) is a different story than an unexplained residual
    cost, and this is what tells the two apart without re-running a diagnostic A/B each time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.guarded = 0
        self.fast = 0

    def record(self, *, guarded: bool) -> None:
        with self._lock:
            if guarded:
                self.guarded += 1
            else:
                self.fast += 1


def build_record(
    entry: os.DirEntry[str],
    current_dir: Path,
    git_cache: GitRepoCache,
    skipped: list[SkippedPath],
    stat_executor: ThreadPoolExecutor | None = None,
    *,
    force_guard: bool = False,
    risk_counter: _RiskCounter | None = None,
) -> tuple[FileRecord, bool] | None:
    """Builds a FileRecord for one os.scandir entry.

    Returns `(record, should_recurse)`, or `None` if the entry couldn't be stat'd (permission
    error, deleted mid-scan, a stat that timed out, etc.) — the caller skips those rather than
    crashing the scan, and this function itself appends a `SkippedPath` to `skipped` so the miss
    is visible in `ScanStats`, not just logged (D12). `should_recurse` is gated solely on the
    reparse-point attribute bit, never on `entry.is_dir()` — Windows junctions carry
    `FILE_ATTRIBUTE_DIRECTORY` alongside the reparse bit and some Python/Windows combinations
    still report them as traversable via `is_dir()`.

    `force_guard` (Wave 1 finding #3, risk-targeted revision — see the guard's own module
    comment): `True` for every entry under a network-mapped/UNC scan root; otherwise the
    per-entry reparse-point/cloud-placeholder check (`_entry_is_risky`) decides. Only entries
    classified risky pay the `stat_executor` thread-hop cost. `risk_counter`, if given, records
    which path each entry took — see `_RiskCounter`.
    """
    entry_path = current_dir / entry.name
    try:
        # os.stat() on `entry.path` (a raw string, not wrapped in `Path`), not entry.stat() and
        # not `entry_path.stat()`: DirEntry.stat() on Windows is populated straight from the
        # FindNextFile data scandir already collected, which does NOT include the file ID
        # (st_ino) or volume serial number (st_dev) — those only come from a real
        # GetFileInformationByHandle call, which os.stat() makes and entry.stat() does not.
        # Measured cost of the extra per-entry syscall this implies: ~30K files/sec on a
        # synthetic local tree, still ~18x the spec's ~1667 files/sec (100K/min) floor, so
        # trading scandir's free-stat optimization for correct hardlink dedup is worth it.
        # `entry.path` already carries whatever `\\?\` prefix the os.scandir() call that produced
        # this DirEntry was given (see `long_path`/D12) — os.stat() on that raw string is what
        # lets a real >MAX_PATH path still be statted; `Path.stat()` doesn't reliably round-trip
        # a `\\?\`-prefixed string (it mishandles the literal `?` segment), same reasoning
        # `executor.py`'s `_atomic_move`/`_tree_stats` already document. follow_symlinks=False so
        # a reparse point is stat'd as itself, not as whatever it points to — required to read
        # the reparse-point attribute bit correctly.
        risky = _entry_is_risky(entry, force_guard=force_guard)
        if risk_counter is not None:
            risk_counter.record(guarded=risky)
        executor_for_call = stat_executor if risky else None
        st = _guarded_stat(executor_for_call, entry.path)
        is_dir_entry = entry.is_dir(follow_symlinks=False)
    except (OSError, FutureTimeoutError) as exc:
        logger.warning("scan.entry_unreadable", path=str(entry_path), error=str(exc))
        skipped.append(SkippedPath(path=str(entry_path), error=str(exc)))
        return None

    attributes = st.st_file_attributes
    is_reparse_point = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    repo_search_start = entry_path if is_dir_entry else current_dir
    repo_root = git_cache.repo_root_for(repo_search_start)
    git_clean = git_cache.is_clean(repo_root) if repo_root is not None else False

    record = FileRecord(
        path=entry_path,
        is_dir=is_dir_entry,
        size_bytes=st.st_size,
        attributes=attributes,
        ext=Path(entry.name).suffix.lower() if not is_dir_entry else "",
        git_repo_root=repo_root,
        git_repo_clean=git_clean,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        dev=st.st_dev,
        ino=st.st_ino,
    )
    return record, (is_dir_entry and not is_reparse_point)


def build_record_for_path(path: Path, git_cache: GitRepoCache) -> FileRecord | None:
    r"""Reconstructs a fresh `FileRecord` for one already-known path outside of an in-progress
    `scan_tree` walk (`build_record` needs a live `os.DirEntry`, which callers here don't have)
    by re-`scandir`-ing the parent directory and delegating to `build_record` — reuses the
    exact same stat/reparse-point/git-repo logic the scanner itself uses rather than
    duplicating it. Used by `executor.py`'s pre-delete safety re-check (ADR-0001), which only
    ever has a `Path` from a `Candidate`, never a live scan in progress. Returns `None` if
    `path` no longer exists or its parent can't be listed.

    `path.parent` is `\\?\`-prefixed (D12) before the `scandir` call the same way `scan_tree`'s
    own walk is — a direct-delete candidate can itself be a deeply-nested path (dev-artifact
    caches routinely are), so this single-path lookup needs the identical MAX_PATH safety.
    `skipped` here is a throwaway, single-call list: this function reports only `FileRecord |
    None` to its caller, which already treats `None` as "path missing, not a fatal error" — the
    `SkippedPath` accounting is specific to `scan_tree`'s own aggregate report.
    """
    skipped: list[SkippedPath] = []
    # D11: NTFS stores filenames as UTF-16 without normalizing composed (NFC) vs decomposed
    # (NFD) Unicode forms -- a filename produced by one path (e.g. a macOS-authored file) can
    # round-trip back from `os.scandir` in a different normalization form than `path.name`'s
    # own in-memory string even though both denote the exact same filesystem entry, which would
    # make a real match miss here and this function wrongly return None. Comparing under NFC is
    # scoped to this equality check alone -- `entry_path`/`FileRecord.path` inside
    # `build_record` are still built from `entry.name` verbatim, never the normalized form, so
    # identity/round-trip behavior elsewhere (the index, `Candidate.path`, etc.) is unaffected.
    target_name = unicodedata.normalize("NFC", path.name)
    try:
        with os.scandir(long_path(path.parent)) as entries:
            for entry in entries:
                if unicodedata.normalize("NFC", entry.name) == target_name:
                    built = build_record(entry, path.parent, git_cache, skipped)
                    return built[0] if built is not None else None
    except OSError:
        return None
    return None


# Wave 1 finding #1 (2026-07-30 real-disk diagnosis): batched-transaction size — mirrors
# dedup.py's `_WRITE_BATCH_SIZE` cadence (that one flushes hash writes; this one flushes scan
# writes) and the spec's own "batched transactions (e.g. 5-10k inserts)" ask.
_WRITE_BATCH_SIZE = 5000


@dataclass(frozen=True, slots=True)
class _SubtreeResult:
    dirs_visited: int
    skipped: list[SkippedPath]


class _BatchIndexWriter:
    """Thread-safe, size-batched wrapper around `ScanIndex` writes, shared across every worker
    `_walk_subtree` fans out to (Wave 1 finding #1). Replaces the prior design, where every
    `FileRecord` for an entire walk was accumulated in one Python list and written once, in a
    single giant transaction, at the very end of `scan_tree` — measured on a real 2.67M-file
    scan of a live home directory at 5,085MB peak RSS (PLAN.md's 2026-07-30 checkpoint), which
    extrapolates well past typical consumer RAM on a genuine full-drive scan.

    Each worker keeps its own small, thread-local pending buffer (cheap; no cross-thread
    contention on the buffer itself) and only takes `_lock` for the actual SQLite call at flush
    time — so peak memory is bounded by `worker_count * _WRITE_BATCH_SIZE`, a fixed ceiling
    independent of total file count, not by the number of files under `root`. A crash mid-walk
    now loses at most the last unflushed batch per worker, not the entire walk's progress —
    everything flushed so far is already durable in the index.
    """

    def __init__(self, index: ScanIndex, *, scanned_at: float) -> None:
        self._index = index
        self._scanned_at = scanned_at
        self._lock = threading.Lock()
        self.written = 0
        self.touched = 0

    def flush(self, upserts: list[FileRecord], unchanged_paths: list[str]) -> None:
        """Writes one batch: `upserts` (changed/new records, or every record when
        `incremental=False`) via `upsert_records`, and every visited path this batch covers
        (both `upserts` and `unchanged_paths`) into the scan's seen-tracking table, so
        `prune_unseen_under_root` can later tell a still-present-but-unchanged file apart from
        one that's genuinely gone — without `scan_tree` ever holding either set fully in Python
        memory (see `index.py`'s `begin_scan_tracking`/`record_seen`/`prune_unseen_under_root`).
        A no-op if both lists are empty (the caller's final flush after an already-flushed,
        exactly-full last batch)."""
        if not upserts and not unchanged_paths:
            return
        seen = [record.path.as_posix() for record in upserts] + unchanged_paths
        with self._lock:
            if upserts:
                self.written += self._index.upsert_records(upserts, scanned_at=self._scanned_at)
            self._index.record_seen(seen)
            self.touched += len(unchanged_paths)


def _classify_record(
    record: FileRecord,
    stat_cache: dict[str, StoredStat] | None,
    incremental: bool,
    pending_upserts: list[FileRecord],
    pending_unchanged: list[str],
) -> None:
    """Sorts one built record into the batch it belongs to — changed/new (needs a real row
    write) or unchanged (only needs to be marked seen, never rewritten — see
    `_BatchIndexWriter.flush`'s docstring). `stat_cache=None` (passed whenever `incremental` is
    False) always classifies as changed, matching the pre-batching behavior exactly."""
    posix_path = record.path.as_posix()
    stored = stat_cache.get(posix_path) if (incremental and stat_cache is not None) else None
    if is_unchanged(stored, current_size=record.size_bytes, current_mtime=record.mtime):
        pending_unchanged.append(posix_path)
    else:
        pending_upserts.append(record)


def _walk_subtree(
    start: Path,
    *,
    stat_cache: dict[str, StoredStat] | None,
    incremental: bool,
    writer: _BatchIndexWriter,
    stat_executor: ThreadPoolExecutor | None,
    force_guard: bool,
    risk_counter: _RiskCounter,
    tracker: _ProgressTracker | None = None,
) -> _SubtreeResult:
    r"""Iterative (not recursive, to avoid Python's recursion limit on deep trees) walk of one
    top-level directory and everything reachable under it without crossing a reparse point.

    Every `os.scandir` call here is `\\?\`-prefixed (D12) — `current_dir` itself always stays an
    ordinary, unprefixed `Path` (used for the walk stack, `FileRecord.path`, and the git-repo
    cache keys), and only the string handed to `scandir` carries the prefix, matching
    `executor.py`'s own "prefix only the raw filesystem call, never the value the rest of the
    code reasons about" convention.

    `tracker` (full-drive-scan-eta): optional, shared across every concurrent invocation of this
    function `scan_tree` fans out (one call per top-level directory) — `None` (the default) when
    `scan_tree` itself was called with no `on_progress`, matching every other progress-hook
    default in this codebase. `writer`/`stat_cache`/`incremental`/`stat_executor` are likewise
    shared across every concurrent invocation (Wave 1 finding #1/#3) — records are written in
    `_WRITE_BATCH_SIZE`-sized batches as this walk progresses, never accumulated for the whole
    subtree and returned.
    """
    git_cache = GitRepoCache()
    skipped: list[SkippedPath] = []
    dirs_visited = 0
    pending_upserts: list[FileRecord] = []
    pending_unchanged: list[str] = []
    stack = [start]
    while stack:
        current_dir = stack.pop()
        dirs_visited += 1
        try:
            entries = list(os.scandir(long_path(current_dir)))
        except OSError as exc:
            logger.warning("scan.dir_unreadable", path=str(current_dir), error=str(exc))
            skipped.append(SkippedPath(path=str(current_dir), error=str(exc)))
            continue

        for entry in entries:
            built = build_record(
                entry,
                current_dir,
                git_cache,
                skipped,
                stat_executor,
                force_guard=force_guard,
                risk_counter=risk_counter,
            )
            if built is None:
                continue
            record, should_recurse = built
            _classify_record(record, stat_cache, incremental, pending_upserts, pending_unchanged)
            if tracker is not None:
                tracker.add(1)
            if should_recurse:
                stack.append(record.path)
            if len(pending_upserts) >= _WRITE_BATCH_SIZE or len(pending_unchanged) >= (
                _WRITE_BATCH_SIZE
            ):
                writer.flush(pending_upserts, pending_unchanged)
                pending_upserts = []
                pending_unchanged = []

    writer.flush(pending_upserts, pending_unchanged)
    return _SubtreeResult(dirs_visited=dirs_visited, skipped=skipped)


def scan_tree(
    root: Path,
    index: ScanIndex,
    *,
    incremental: bool = True,
    max_workers: int | None = None,
    on_progress: ScanProgressCallback | None = None,
    entries_estimated_total: int | None = None,
) -> ScanStats:
    """Walks `root`, populates `index` with a complete inventory, and prunes rows for entries
    that no longer exist. One `ThreadPoolExecutor` task per top-level directory under `root`
    (os.scandir's underlying syscalls release the GIL, so threading helps despite the
    CPU-bound-looking code); loose files directly under `root` are handled inline.

    `on_progress`/`entries_estimated_total` (full-drive-scan-eta): optional, interval-gated
    progress hook — see `ScanProgressCallback`'s docstring. Both default to `None` so every
    existing caller (this test suite's scanner tests included) is unaffected.
    `entries_estimated_total` is passed straight through to every `on_progress` call unchanged
    (`scan_tree` never computes an ETA itself; see `ScanProgressCallback`).

    Wave 1 (2026-07-30 real-disk diagnosis) rewrote this function's write path: records are
    streamed into the index in `_WRITE_BATCH_SIZE`-sized transactions via `_BatchIndexWriter` as
    the walk progresses (finding #1), never accumulated into one whole-walk list; pruning is
    computed inside SQLite via a seen-tracking temp table (finding #4) instead of two full
    Python collections; worker results are consumed via `as_completed` rather than
    submission-ordered `executor.map` (finding #3), so one slow top-level directory can't delay
    processing already-finished ones; and every `os.stat()` call on a plausible stat-hang
    candidate (a reparse point, a cloud placeholder, or anything under a network-mapped/UNC
    root) goes through a shared, timeout-guarded pool (finding #3, risk-targeted revision — see
    `_entry_is_risky`'s module comment) so a single stalled syscall can't wedge the walk
    silently, without paying a per-file thread-hop cost on the overwhelming majority of entries
    that are never actually at risk of stalling.
    """
    start_time = time.monotonic()
    scanned_at = time.time()
    tracker = _ProgressTracker(
        on_progress, entries_estimated_total=entries_estimated_total, start_time=start_time
    )
    writer = _BatchIndexWriter(index, scanned_at=scanned_at)

    # Only loaded when `incremental` — `prune_unseen_under_root` no longer needs the
    # previously-indexed path set (it queries `files` directly), so a `--full`/forced rescan of
    # an existing large index no longer pays for a stat_cache it would never consult anyway.
    stat_cache: dict[str, StoredStat] | None = index.load_stat_cache(root) if incremental else None

    # Checked once for the whole scan, not per-entry or per-directory — see
    # `_entry_is_risky`/`is_network_drive`'s docstrings.
    force_guard = is_network_drive(root)
    risk_counter = _RiskCounter()

    all_skipped: list[SkippedPath] = []
    try:
        top_level_entries = list(os.scandir(long_path(root)))
    except OSError as exc:
        logger.warning("scan.root_unreadable", path=str(root), error=str(exc))
        top_level_entries = []
        all_skipped.append(SkippedPath(path=str(root), error=str(exc)))

    root_git_cache = GitRepoCache()
    pending_upserts: list[FileRecord] = []
    pending_unchanged: list[str] = []
    recurse_into: list[Path] = []
    stat_executor = ThreadPoolExecutor(max_workers=_STAT_TIMEOUT_WORKERS)
    try:
        for entry in top_level_entries:
            built = build_record(
                entry,
                root,
                root_git_cache,
                all_skipped,
                stat_executor,
                force_guard=force_guard,
                risk_counter=risk_counter,
            )
            if built is None:
                continue
            record, should_recurse = built
            _classify_record(record, stat_cache, incremental, pending_upserts, pending_unchanged)
            tracker.add(1)
            if should_recurse:
                recurse_into.append(record.path)

        index.begin_scan_tracking()
        try:
            writer.flush(pending_upserts, pending_unchanged)

            dirs_visited = 1  # root itself
            if recurse_into:
                worker_count = max_workers or min(32, (os.cpu_count() or 4) * 4)
                walk_subtree_with_progress = functools.partial(
                    _walk_subtree,
                    stat_cache=stat_cache,
                    incremental=incremental,
                    writer=writer,
                    stat_executor=stat_executor,
                    force_guard=force_guard,
                    risk_counter=risk_counter,
                    tracker=tracker,
                )
                # as_completed, not executor.map: map()'s iterator yields in SUBMISSION order,
                # blocking on an earlier-submitted-but-still-pending directory even once every
                # later one has already finished (Wave 1 finding #3) — as_completed consumes
                # whichever result is ready next, so one slow top-level directory no longer
                # holds up processing (dirs_visited/skipped accounting, progress) for the rest.
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [executor.submit(walk_subtree_with_progress, p) for p in recurse_into]
                    for future in as_completed(futures):
                        result = future.result()
                        dirs_visited += result.dirs_visited
                        all_skipped.extend(result.skipped)

            files_pruned = index.prune_unseen_under_root(root)
        except (OSError, sqlite3.OperationalError) as exc:
            if _is_disk_full(exc):
                raise ScanDiskFullError("disk is full — free up space and try again") from exc
            raise
        finally:
            index.end_scan_tracking()
    finally:
        stat_executor.shutdown(wait=False, cancel_futures=False)

    return ScanStats(
        root=root,
        dirs_visited=dirs_visited,
        entries_total=writer.written + writer.touched,
        files_written=writer.written,
        files_unchanged=writer.touched,
        files_pruned=files_pruned,
        elapsed_seconds=time.monotonic() - start_time,
        skipped_unreadable_count=len(all_skipped),
        skipped_unreadable_paths=tuple(
            skipped_path.path for skipped_path in all_skipped[:_SKIPPED_PATHS_SAMPLE_LIMIT]
        ),
        guarded_stat_count=risk_counter.guarded,
        fast_stat_count=risk_counter.fast,
    )
