from __future__ import annotations

import os
import stat
import time
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog

from reclaim.index import ScanIndex
from reclaim.scanner import long_path

logger = structlog.get_logger(__name__)

# Rows fetched per keyset page: bounds memory (a page of (path, size, is_dir) tuples) and the
# size of each delete transaction, independent of how large the index is.
_PAGE_SIZE = 50_000
# Directory listings kept in memory for the `deep` pass. Rows of one directory are mostly
# adjacent in path order, so a small LRU makes each directory be listed about once.
_LISTING_CACHE_SIZE = 512
# Segments of the path used to group the report ("C:/Users/gaura/ml-projects").
_REPORT_PREFIX_SEGMENTS = 4


class _Dir(Enum):
    """What the filesystem says about one directory."""

    EXISTS = "exists"
    MISSING = "missing"  # the OS positively reported the path is gone
    UNKNOWN = "unknown"  # anything else: access denied, offline share, I/O error -> keep rows


@dataclass(slots=True)
class PruneReport:
    """Outcome of one `prune_dead_rows` pass (a dry run reports what `apply` would delete)."""

    applied: bool
    rows_examined: int = 0
    dead_rows: int = 0
    dead_bytes: int = 0
    dirs_checked: int = 0
    dirs_missing: int = 0
    unknown_rows_kept: int = 0
    seconds: float = 0.0
    dead_by_prefix: Counter[str] = field(default_factory=Counter)


def _os_path(posix_dir: str) -> str:
    """`C:/a/b` -> `\\\\?\\C:\\a\\b` (long-path form; a >260-char directory must never look
    missing just because the legacy API refused it)."""
    # A bare drive ("C:") would mean "current directory on C:" -- force the root form.
    if len(posix_dir) == 2 and posix_dir[1] == ":":
        posix_dir += "/"
    return long_path(Path(posix_dir))


def _probe_dir(posix_dir: str) -> _Dir:
    """Classifies one directory. MISSING only on a definitive not-found / not-a-directory."""
    try:
        # PTH116: os.stat on the raw long-path string, same as `_entry_confirmed_missing`'s
        # os.lstat; no pathlib round-trip of the `\\?\` prefix.
        mode = os.stat(_os_path(posix_dir)).st_mode  # noqa: PTH116
    except (FileNotFoundError, NotADirectoryError):
        return _Dir.MISSING
    except OSError:
        return _Dir.UNKNOWN
    return _Dir.EXISTS if stat.S_ISDIR(mode) else _Dir.MISSING


def _entry_confirmed_missing(posix_path: str) -> bool:
    """True only on a definitive not-found for the entry ITSELF (lstat, so a junction/symlink
    whose target is gone still counts as present -- `_probe_dir` follows links)."""
    try:
        os.lstat(_os_path(posix_path))
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _anchor(posix_path: str) -> str | None:
    """Drive (`C:`) or UNC share (`//server/share`) the path lives on; None if neither."""
    drive, _ = os.path.splitdrive(posix_path.replace("/", "\\"))
    return drive.replace("\\", "/") or None


def _name_variants(name: str) -> set[str]:
    """Spellings under which a directory entry may legitimately be compared (exact, lower,
    NFC) -- erring toward 'present' because a wrong 'missing' deletes a row of a real file."""
    return {name, name.lower(), unicodedata.normalize("NFC", name)}


def prune_dead_rows(
    index: ScanIndex,
    *,
    apply: bool = False,
    root_prefixes: Sequence[str] | None = None,
    deep: bool = False,
    probe_dir: Callable[[str], _Dir] = _probe_dir,
) -> PruneReport:
    """Finds (and with `apply=True` deletes) index rows that no longer exist on disk, checking
    DIRECTORIES rather than files: a row is dead when its parent directory is positively gone
    (one `stat` per distinct directory, not per file). With `deep=True` an existing parent is
    also listed once and rows whose name is no longer in the listing are dead.

    Fail closed: a row is kept whenever existence cannot be established -- the drive/share is
    unreachable, the directory stat or listing fails for any reason other than "not found". A
    row is never deleted on the strength of an error.

    `root_prefixes` limits the pass to rows at/under those POSIX prefixes (default: every row).
    This only covers rows by path; it knows nothing about scan roots (the index doesn't record
    them) -- callers who just rescanned a root need not exclude it, since its rows are already
    reconciled and dead ones were removed by the scan itself.
    """
    start = time.monotonic()
    report = PruneReport(applied=apply)
    dir_state: dict[str, _Dir] = {}
    anchor_ok: dict[str | None, bool] = {}
    listings: OrderedDict[str, set[str] | None] = OrderedDict()

    def anchor_reachable(path: str) -> bool:
        anchor = _anchor(path)
        if anchor not in anchor_ok:
            anchor_ok[anchor] = anchor is not None and probe_dir(anchor) is _Dir.EXISTS
        return anchor_ok[anchor]

    def state_of(parent: str) -> _Dir:
        cached = dir_state.get(parent)
        if cached is not None:
            return cached
        # A vanished drive/share also reports "not found" for every path on it; only believe
        # MISSING when the anchor itself is reachable.
        state = probe_dir(parent) if anchor_reachable(parent) else _Dir.UNKNOWN
        dir_state[parent] = state
        report.dirs_checked += 1
        if state is _Dir.MISSING:
            report.dirs_missing += 1
        return state

    def listing_of(parent: str) -> set[str] | None:
        if parent in listings:
            listings.move_to_end(parent)
            return listings[parent]
        names: set[str] | None
        try:
            with os.scandir(_os_path(parent)) as it:
                names = set()
                for entry in it:
                    names |= _name_variants(entry.name)
        except OSError:
            names = None  # cannot list -> every row in it is kept
        listings[parent] = names
        if len(listings) > _LISTING_CACHE_SIZE:
            listings.popitem(last=False)
        return names

    prefixes: Sequence[str | None] = list(root_prefixes) if root_prefixes else [None]
    for prefix in prefixes:
        after = ""
        while True:
            page = index.page_rows_after(after, limit=_PAGE_SIZE, prefix=prefix)
            if not page:
                break
            after = page[-1][0]
            dead: list[str] = []
            for path, size, is_dir in page:
                report.rows_examined += 1
                parent, sep, name = path.rpartition("/")
                if not sep or not parent:
                    report.unknown_rows_kept += 1
                    continue
                state = state_of(parent)
                is_dead = state is _Dir.MISSING
                if state is _Dir.UNKNOWN:
                    report.unknown_rows_kept += 1
                elif deep and state is _Dir.EXISTS:
                    names = listing_of(parent)
                    if names is None:
                        report.unknown_rows_kept += 1
                    else:
                        is_dead = not (_name_variants(name) & names)
                if not is_dead and is_dir and state is _Dir.EXISTS:
                    # A directory row whose own directory is gone from a parent that still
                    # exists (the parent check above cannot see this). `state_of` only says
                    # MISSING when the drive is reachable; the lstat then rules out a link
                    # whose target vanished. The result is cached for this directory's children.
                    is_dead = state_of(path) is _Dir.MISSING and _entry_confirmed_missing(path)
                if is_dead:
                    dead.append(path)
                    report.dead_rows += 1
                    if not is_dir:
                        report.dead_bytes += size
                    report.dead_by_prefix["/".join(path.split("/")[:_REPORT_PREFIX_SEGMENTS])] += 1
            if apply and dead:
                index.delete_paths(dead)
    report.seconds = time.monotonic() - start
    logger.info(
        "index.prune_dead_rows",
        applied=apply,
        deep=deep,
        rows_examined=report.rows_examined,
        dead_rows=report.dead_rows,
        dead_bytes=report.dead_bytes,
        unknown_rows_kept=report.unknown_rows_kept,
        seconds=round(report.seconds, 2),
    )
    return report
