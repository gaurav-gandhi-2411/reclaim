from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from reclaim.scanner import long_path


class FreshStat(NamedTuple):
    """A live `os.stat` of one path: `size` bytes, `mtime` (float seconds, byte-identical to
    what the index stores for the same file) and the same instant as integer nanoseconds."""

    size: int
    mtime: float
    mtime_ns: int


def fresh_stat_signature(path: Path) -> FreshStat | None:
    """Real `os.stat` of `path` (size, mtime), or `None` if it cannot be read.

    ADR-0035: the scan takes size/mtime from the NTFS directory listing, which NTFS refreshes
    lazily -- a file that is OPEN FOR WRITE shows its last-close size/mtime there. Decisions
    that hinge on size/mtime exactness (age thresholds, hash-cache reuse) call this for their
    candidate set only, never for the whole index. The listing can only be stale toward an
    OLDER mtime (the write has not been flushed to the entry yet), so a fresh stat can only
    move a decision toward "keep"; `None` (missing, access denied) carries no evidence of a
    live writer and callers keep the listing's value rather than inventing one.
    """
    try:
        st = Path(long_path(path)).stat()
    except OSError:
        return None
    return FreshStat(size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns)
