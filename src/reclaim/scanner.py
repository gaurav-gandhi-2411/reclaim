from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import structlog

from reclaim.index import ScanIndex, StoredStat, is_unchanged
from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT, FileRecord

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


class _GitRepoCache:
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
        directory visited along the way so sibling files/dirs resolve in O(1)."""
        visited: list[Path] = []
        current = search_start
        while True:
            cached = self._repo_root_cache.get(current, self._UNSET)
            if cached is not self._UNSET:
                result: Path | None = cached  # type: ignore[assignment]
                break
            visited.append(current)
            if (current / ".git").is_dir():
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
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("scan.git_status_failed", repo_root=str(repo_root), error=str(exc))
        return False
    return result.stdout.strip() == ""


def _build_record(
    entry: os.DirEntry[str], current_dir: Path, git_cache: _GitRepoCache
) -> tuple[FileRecord, bool] | None:
    """Builds a FileRecord for one os.scandir entry.

    Returns `(record, should_recurse)`, or `None` if the entry couldn't be stat'd (permission
    error, deleted mid-scan, etc.) — the caller logs and skips those rather than crashing the
    scan. `should_recurse` is gated solely on the reparse-point attribute bit, never on
    `entry.is_dir()` — Windows junctions carry `FILE_ATTRIBUTE_DIRECTORY` alongside the
    reparse bit and some Python/Windows combinations still report them as traversable via
    `is_dir()`.
    """
    entry_path = Path(entry.path)
    try:
        # Path.stat(), not entry.stat(): DirEntry.stat() on Windows is populated straight from
        # the FindNextFile data scandir already collected, which does NOT include the file ID
        # (st_ino) or volume serial number (st_dev) — those only come from a real
        # GetFileInformationByHandle call, which Path.stat()/os.stat() make and entry.stat()
        # does not. Measured cost of the extra per-entry syscall this implies: ~30K files/sec
        # on a synthetic local tree, still ~18x the spec's ~1667 files/sec (100K/min) floor, so
        # trading scandir's free-stat optimization for correct hardlink dedup is worth it.
        # follow_symlinks=False so a reparse point is stat'd as itself, not as whatever it
        # points to — required to read the reparse-point attribute bit correctly.
        st = entry_path.stat(follow_symlinks=False)
        is_dir_entry = entry.is_dir(follow_symlinks=False)
    except OSError as exc:
        logger.warning("scan.entry_unreadable", path=entry.path, error=str(exc))
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


@dataclass(frozen=True, slots=True)
class _SubtreeResult:
    records: list[FileRecord]
    dirs_visited: int


def _walk_subtree(start: Path) -> _SubtreeResult:
    """Iterative (not recursive, to avoid Python's recursion limit on deep trees) walk of one
    top-level directory and everything reachable under it without crossing a reparse point.
    """
    git_cache = _GitRepoCache()
    records: list[FileRecord] = []
    dirs_visited = 0
    stack = [start]
    while stack:
        current_dir = stack.pop()
        dirs_visited += 1
        try:
            entries = list(os.scandir(current_dir))
        except OSError as exc:
            logger.warning("scan.dir_unreadable", path=str(current_dir), error=str(exc))
            continue

        for entry in entries:
            built = _build_record(entry, current_dir, git_cache)
            if built is None:
                continue
            record, should_recurse = built
            records.append(record)
            if should_recurse:
                stack.append(record.path)

    return _SubtreeResult(records=records, dirs_visited=dirs_visited)


def scan_tree(
    root: Path,
    index: ScanIndex,
    *,
    incremental: bool = True,
    max_workers: int | None = None,
) -> ScanStats:
    """Walks `root`, populates `index` with a complete inventory, and prunes rows for entries
    that no longer exist. One `ThreadPoolExecutor` task per top-level directory under `root`
    (os.scandir's underlying syscalls release the GIL, so threading helps despite the
    CPU-bound-looking code); loose files directly under `root` are handled inline.
    """
    start_time = time.monotonic()
    scanned_at = time.time()

    # Always loaded (regardless of `incremental`): prune_missing needs the previously-indexed
    # path set to detect deletions either way. `incremental` only controls whether it's also
    # used below to skip writing unchanged records.
    stat_cache: dict[str, StoredStat] = index.load_stat_cache(root)

    try:
        top_level_entries = list(os.scandir(root))
    except OSError as exc:
        logger.warning("scan.root_unreadable", path=str(root), error=str(exc))
        top_level_entries = []

    root_git_cache = _GitRepoCache()
    top_level_records: list[FileRecord] = []
    recurse_into: list[Path] = []
    for entry in top_level_entries:
        built = _build_record(entry, root, root_git_cache)
        if built is None:
            continue
        record, should_recurse = built
        top_level_records.append(record)
        if should_recurse:
            recurse_into.append(record.path)

    dirs_visited = 1  # root itself
    all_records: list[FileRecord] = list(top_level_records)
    if recurse_into:
        worker_count = max_workers or min(32, (os.cpu_count() or 4) * 4)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for result in executor.map(_walk_subtree, recurse_into):
                all_records.extend(result.records)
                dirs_visited += result.dirs_visited

    to_write: list[FileRecord] = []
    unchanged_paths: list[str] = []
    seen_paths: list[str] = []
    for record in all_records:
        posix_path = record.path.as_posix()
        seen_paths.append(posix_path)
        stored = stat_cache.get(posix_path) if incremental else None
        if is_unchanged(stored, current_size=record.size_bytes, current_mtime=record.mtime):
            unchanged_paths.append(posix_path)
        else:
            to_write.append(record)

    files_written = index.upsert_records(to_write, scanned_at=scanned_at)
    files_pruned = index.prune_missing(stat_cache.keys(), seen_paths)

    return ScanStats(
        root=root,
        dirs_visited=dirs_visited,
        entries_total=len(all_records),
        files_written=files_written,
        files_unchanged=len(unchanged_paths),
        files_pruned=files_pruned,
        elapsed_seconds=time.monotonic() - start_time,
    )
