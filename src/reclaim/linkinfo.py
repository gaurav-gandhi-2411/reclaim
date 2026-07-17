from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.models import FILE_ATTRIBUTE_REPARSE_POINT

# ADR-0006: the uv/cache purge measured logical size (14.3GB) against real disk-free delta
# (5.21GB) and found a large gap — uv hardlinks cache blobs into live venvs, so deleting the
# cache's own name for a blob drops its link count without freeing the shared blocks as long as
# another name (the venv's own copy) still points to them. `exact_duplicate`'s keep-heuristic
# candidates are the same shape in reverse: a "duplicate" that's actually a hardlink to the kept
# copy shares the SAME blocks already — deleting it frees nothing, and BYTE-IDENTICAL content is
# exactly what a hardlink produces, so this isn't a rare edge case for that category specifically.


@dataclass(frozen=True, slots=True)
class LinkIdentity:
    """A real file's Windows hardlink identity, read via a direct `os.stat()` call — never a
    cached `DirEntry.stat()`, which does not populate `st_ino`/`st_dev` on Windows (the same
    gotcha this project's scanner already discovered once; see `PLAN.md`'s "Gotchas
    discovered"). `nlink` is the TOTAL number of names pointing to this inode across the whole
    filesystem, not just however many happen to be in any particular candidate set.

    ADR-0008: `is_reparse_point` is a separate, independent signal from `nlink` — a real
    Windows hardlink is NOT a reparse point (multiple directory entries pointing at the same MFT
    record, no reparse tag involved), but some OTHER storage-sharing mechanisms (Windows Server
    Data Deduplication's chunk store, `IO_REPARSE_TAG_DEDUP`; possibly others in the future) DO
    use a reparse point and would still report `st_nlink == 1` since they aren't hardlinks —
    `st_nlink` alone can't see that kind of sharing. Investigated against this project's real,
    reported HF-hub blob/snapshot pairs: confirmed via direct `os.stat()` that those specific
    pairs are genuinely separate inodes with `nlink == 1` and NOT reparse points (a symlink-
    privilege fallback produced full copies, not a link) — this field does not change that
    finding, it's an independent defensive check for a different, adjacent risk class.
    """

    dev: int
    ino: int
    nlink: int
    is_reparse_point: bool


def read_link_identity(path: Path) -> LinkIdentity | None:
    """`None` if `path` no longer exists or can't be stat'd (a race between candidate
    generation and reclaimability estimation, or a permission error) — callers must treat this
    as "unknown, fails closed" rather than assuming a standalone file."""
    try:
        st = path.stat()
    except OSError:
        return None
    return LinkIdentity(
        dev=st.st_dev,
        ino=st.st_ino,
        nlink=st.st_nlink,
        is_reparse_point=bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT),
    )


@dataclass(frozen=True, slots=True)
class ReclaimEstimate:
    """Per-candidate-path outcome of `estimate_reclaimable_bytes`."""

    logical_bytes: int
    # Real, hardlink-aware estimate of bytes this candidate's deletion would actually free,
    # given every OTHER candidate path in the same `estimate_reclaimable_bytes` call. Always
    # `<= logical_bytes`; `0` for every member of a shared inode except the one credited with
    # the (single, not-double-counted) reclaim once every name pointing to it is covered.
    reclaimable_bytes: int
    # `False` if this path's link identity couldn't be determined at estimation time (vanished,
    # permission error) — `reclaimable_bytes` is conservatively `0` in that case, not a guess in
    # either direction (never claims a reclaim that might not materialize).
    resolved: bool


def estimate_reclaimable_bytes(
    candidates: Sequence[tuple[Path, int]],
) -> dict[Path, ReclaimEstimate]:
    """For a set of candidate `(path, logical_size_bytes)` pairs being considered for deletion
    TOGETHER, estimates real reclaimable bytes accounting for Windows hardlinks: deleting one
    name pointing to a shared inode only drops the link count — the underlying blocks stay
    allocated as long as ANY other name, in this set or not, still points to them.

    Groups candidates by `(dev, ino)` identity (one direct `os.stat()` call per path — only
    ever run against a bounded candidate set, never the whole inventory, so this stays cheap
    even though it's a live filesystem touch rather than an indexed query). For an inode only
    reachable via a single name (`nlink == 1`), or whose identity couldn't be determined at all,
    the candidate's full logical size counts as reclaimable (`resolved=False` for the latter,
    signaling this is an assumption, not a lookup that came back negative).

    For an inode shared by multiple candidates in this set: if the number of candidates sharing
    it equals that inode's own `nlink` (every name pointing to it is present in this call's
    input), the size counts ONCE — credited to one arbitrary-but-deterministic member, `0` for
    the rest of that group, so summing `reclaimable_bytes` across every returned estimate gives
    the correct total without the caller needing to know about the grouping itself. If some
    OTHER name outside this set still holds a link (fewer sharing candidates than `nlink`), the
    whole group's `reclaimable_bytes` is `0` — deleting every member of this candidate set alone
    would not free those blocks (the classic "duplicate is actually a hardlink to the kept
    copy" case: the kept file's own name is exactly that "other name outside this set").

    ADR-0008: a path that looks like a standalone file by `st_nlink` (`nlink == 1`) but IS a
    reparse point is treated the same as an unresolvable path — `resolved=False`,
    `reclaimable_bytes=0` — rather than confidently claiming its full logical size. A real
    hardlink is never a reparse point, so this never fires for the case this module was built
    for; it exists for storage-sharing mechanisms `st_nlink` cannot see at all (e.g. Windows
    Data Deduplication's chunk store), where claiming the full size is reclaimable could
    overclaim in a way this module's whole purpose is to avoid.
    """
    results: dict[Path, ReclaimEstimate] = {}
    groups: dict[tuple[int, int], list[tuple[Path, int]]] = defaultdict(list)
    group_nlink: dict[tuple[int, int], int] = {}

    for path, size in candidates:
        identity = read_link_identity(path)
        if identity is None:
            results[path] = ReclaimEstimate(logical_bytes=size, reclaimable_bytes=0, resolved=False)
            continue
        if identity.nlink <= 1:
            if identity.is_reparse_point:
                results[path] = ReclaimEstimate(
                    logical_bytes=size, reclaimable_bytes=0, resolved=False
                )
            else:
                results[path] = ReclaimEstimate(
                    logical_bytes=size, reclaimable_bytes=size, resolved=True
                )
            continue
        key = (identity.dev, identity.ino)
        groups[key].append((path, size))
        group_nlink[key] = identity.nlink

    for key, members in groups.items():
        nlink = group_nlink[key]
        if len(members) >= nlink:
            first_path, first_size = members[0]
            results[first_path] = ReclaimEstimate(
                logical_bytes=first_size, reclaimable_bytes=first_size, resolved=True
            )
            for path, size in members[1:]:
                results[path] = ReclaimEstimate(
                    logical_bytes=size, reclaimable_bytes=0, resolved=True
                )
        else:
            # Some name pointing to this inode is outside our candidate set (e.g. a duplicate
            # cluster's kept member) -- deleting everything in this set still leaves that
            # external name holding the blocks open.
            for path, size in members:
                results[path] = ReclaimEstimate(
                    logical_bytes=size, reclaimable_bytes=0, resolved=True
                )
    return results
