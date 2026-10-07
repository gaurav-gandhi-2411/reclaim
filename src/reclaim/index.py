from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from reclaim.models import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileRecord,
)

# Migration/backfill batch size for `_backfill_name_and_path_lower` — streamed via
# `fetchmany`/`executemany` in chunks rather than loading every legacy row at once, so
# backfilling a multi-million-row pre-existing index doesn't itself materialize the whole
# table into memory (the exact anti-pattern this schema change exists to eliminate elsewhere).
_MIGRATION_BATCH_SIZE = 5000

# Column order shared by the CREATE TABLE, INSERT, and row-reconstruction code so the three
# stay in sync by construction rather than by three separately-maintained lists.
_COLUMNS = (
    "path",
    "size",
    "mtime",
    "ctime",
    "ext",
    "attributes",
    "dev",
    "ino",
    "is_dir",
    "is_cloud_placeholder",
    "is_reparse_point",
    "git_repo_root",
    "git_repo_clean",
    "last_scanned",
    "name",
    "path_lower",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ctime REAL NOT NULL,
    ext TEXT NOT NULL,
    attributes INTEGER NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    is_dir INTEGER NOT NULL,
    is_cloud_placeholder INTEGER NOT NULL,
    is_reparse_point INTEGER NOT NULL,
    git_repo_root TEXT,
    git_repo_clean INTEGER NOT NULL,
    last_scanned REAL NOT NULL,
    hash_size INTEGER,
    hash_mtime REAL,
    partial_hash TEXT,
    full_hash TEXT,
    name TEXT,
    path_lower TEXT
);
"""
# `name` (lowercased basename) and `path_lower` (lowercased posix path) exist purely so Stage
# 3/4 candidate generation can query for matches via an index instead of materializing every
# row into a Python `FileRecord` and filtering in-process (see `files_by_name`,
# `files_matching_path_pattern`, `duplicate_size_candidates` below). Nullable, like the hash
# columns above: a pre-existing index created before this schema version starts with both NULL
# and gets backfilled once by `_backfill_name_and_path_lower` (SQLite's `ALTER TABLE ADD COLUMN`
# can't retroactively populate a NOT NULL column on a non-empty table without a fixed default,
# and a fixed default here would be wrong for every existing row).
# hash_size/hash_mtime record the (size, mtime) a row's hash columns were computed against —
# the Stage 4 dedup pipeline's cache-validity check (`cached_partial_hash`/`cached_full_hash`)
# compares them to the row's *current* size/mtime, same invalidation logic as `is_unchanged`.
# Deliberately not part of `_COLUMNS`/`upsert_records`: a scanner upsert (new size/mtime after a
# real content change) must never silently carry a stale hash forward, and keeping these four
# columns out of the generic upsert path means they only ever change via the dedicated
# `store_partial_hashes`/`store_full_hashes` writes below.
# `path TEXT PRIMARY KEY` already builds an implicit unique index on path, which is what the
# brief's "at least an index on path" asks for — a second explicit index on the same column
# would be a dead duplicate, so it's deliberately omitted here.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_files_dev_ino ON files(dev, ino);",
    "CREATE INDEX IF NOT EXISTS idx_files_is_cloud_placeholder ON files(is_cloud_placeholder);",
    "CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);",
    "CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);",
    "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);",
    # COLLATE NOCASE lets SQLite's LIKE-to-index-range-scan optimization fire under the
    # default case-insensitive LIKE semantics (empirically confirmed: without this collation,
    # SQLite falls back to a full `SCAN files` for every `path_lower LIKE ?` query, even though
    # both sides are already lowercased) — see `files_matching_path_pattern`.
    "CREATE INDEX IF NOT EXISTS idx_files_path_lower ON files(path_lower COLLATE NOCASE);",
)

# P0-5: directories/entries the scanner could not fully account for -- see `InaccessibleEntry`
# and `ScanIndex.replace_inaccessible_under_root`. A brand-new, separate table (not a column on
# `files`) because these paths were never actually indexed as filesystem entries -- there is no
# `files` row to attach the estimate to, and a directory that becomes listable again on a later
# scan must disappear from here entirely, which `replace_inaccessible_under_root` implements as
# a straightforward delete-then-insert (see its docstring) rather than the `files` table's
# streaming seen-tracking machinery -- this set is always small (a handful to a few hundred real
# skips, never millions), so that complexity buys nothing here.
_INACCESSIBLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inaccessible_paths (
    path TEXT PRIMARY KEY,
    error TEXT NOT NULL,
    size_estimate_bytes INTEGER,
    size_estimate_is_lower_bound INTEGER NOT NULL,
    last_scanned REAL NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class StoredStat:
    """The two fields an incremental rescan compares against — nothing else is needed to
    decide whether a file changed, and atime is deliberately never part of this (NTFS
    access-time updates are disabled by default, per spec)."""

    size: int
    mtime: float


def is_unchanged(stored: StoredStat | None, *, current_size: int, current_mtime: float) -> bool:
    """True if a previously-indexed file's (size, mtime) still matches the current scandir
    stat, meaning it can be skipped from this scan's write workload."""
    if stored is None:
        return False
    return stored.size == current_size and stored.mtime == current_mtime


@dataclass(frozen=True, slots=True)
class InaccessibleEntry:
    """One `inaccessible_paths` row (P0-5) -- the persisted form of `scanner.SkippedPath`.

    Deliberately duplicated here rather than importing `SkippedPath` directly: `scanner.py`
    already imports FROM `index.py` (`ScanIndex`, `StoredStat`, `is_unchanged`), so the reverse
    import would be circular -- the same reasoning `scanner._due`'s own docstring documents for
    its own small, deliberate duplication rather than a new inter-module dependency.
    """

    path: str
    error: str
    size_estimate_bytes: int | None
    size_estimate_is_lower_bound: bool


@dataclass(frozen=True, slots=True)
class InaccessibleSummary:
    """Aggregate view over `inaccessible_paths` (P0-5) -- computed in SQL so
    `SummaryResponse`/the CLI reconciliation diagnostic never need to materialize every row just
    to total them.

    `known_bytes` sums `size_estimate_bytes` for rows where it isn't `None` -- an unknown
    estimate contributes nothing to this total by design (fabricating a number for it would be
    exactly the dishonesty this feature exists to avoid), which is why `unknown_count` is always
    reported alongside it: the residual gap between a reconciliation delta and zero is only
    honestly explained by *this* number, not silently absorbed into `known_bytes`.
    """

    path_count: int
    known_bytes: int
    unknown_count: int


@dataclass(frozen=True, slots=True)
class HashCacheEntry:
    """Cached hash values for one path, plus the (size, mtime) they were computed against.
    Valid only as long as those match the path's *current* size/mtime — see
    `cached_partial_hash`/`cached_full_hash`."""

    hash_size: int
    hash_mtime: float
    partial_hash: str | None
    full_hash: str | None


def cached_partial_hash(
    entry: HashCacheEntry | None, *, current_size: int, current_mtime: float
) -> str | None:
    """Returns the cached partial hash if `entry` is still valid for (current_size,
    current_mtime); otherwise None, meaning the caller must recompute."""
    if entry is None or entry.partial_hash is None:
        return None
    if entry.hash_size != current_size or entry.hash_mtime != current_mtime:
        return None
    return entry.partial_hash


def cached_full_hash(
    entry: HashCacheEntry | None, *, current_size: int, current_mtime: float
) -> str | None:
    """Same validity check as `cached_partial_hash`, for the full-file hash."""
    if entry is None or entry.full_hash is None:
        return None
    if entry.hash_size != current_size or entry.hash_mtime != current_mtime:
        return None
    return entry.full_hash


def _escape_like_prefix(value: str) -> str:
    """Escapes SQLite LIKE wildcards in a path prefix before it's used with `LIKE ... ESCAPE
    '\\'` — prefixes come from scan roots (our own CLI args), not untrusted network input, but
    escaping costs nothing and keeps prefix matching correct for paths containing literal
    `%`/`_`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prefix_range(prefix: str) -> tuple[str, str]:
    """Returns `(lower, upper)` bounds for an indexed range scan matching every `path` that
    starts with `prefix + '/'` — the fix for a real performance bug found on the actual
    real-disk run: `LIKE 'prefix/%' ESCAPE '\\'` can *never* become an index range scan once an
    ESCAPE clause is present (confirmed empirically — same query, same index, only the ESCAPE
    clause differs, and the plan degrades from an index `SEARCH` to a full `SCAN`). On a
    3.1M-row real index, `direct_children()` alone was measured at ~1.5 seconds *per call*
    doing a full scan — and `detect_archive_pairs` calls it once per archive file (thousands on
    a real disk), which is what turned "candidate generation is fast" into a 20+ minute stall.

    `'0'` (0x30) is the next ASCII code point after `'/'` (0x2F), so `prefix + '0'` is a tight
    exclusive upper bound: any real path starting with `prefix + '/'` compares less than it
    (the strings first differ at the character right after `prefix`, where `/` < `0`),
    regardless of what follows. Unlike the LIKE-based approach, this needs no escaping *for the
    range bounds themselves* — a plain `BINARY`-collation range comparison treats every
    character literally, including a literal `%`/`_` in a real directory name (which a real
    disk has: e.g. `.../immutable/_app`) — `_escape_like_prefix` is still used for any
    *residual* LIKE clause layered on top of this range (see `direct_children`), since that one
    isn't a range comparison and still needs its wildcards escaped.
    """
    return f"{prefix}/", f"{prefix}0"


_SQLITE_INT64_MAX = 2**63 - 1


def _to_db_int64(value: int) -> int:
    """Maps an unsigned 64-bit filesystem identifier (`st_dev`/`st_ino`) into SQLite's signed
    64-bit `INTEGER` range via two's-complement wraparound, at the DB write boundary.

    Windows' `st_ino`/`st_dev` are unsigned 64-bit values that can exceed
    `2**63 - 1` on ReFS volumes, dev drives, and (confirmed in CI) GitHub's own Windows
    runners — `sqlite3` raises `OverflowError: Python int too large to convert to SQLite
    INTEGER` the moment such a value is bound as a query parameter, aborting the whole scan.
    dev/ino are only ever used for EQUALITY (hardlink-identity grouping — ADR-0006's
    `physical_size_bytes`, `idx_files_dev_ino`), never ordering or arithmetic, so a bit-for-bit
    reversible wraparound (undone by `_from_db_int64`) preserves every semantic that matters:
    two equal unsigned values wrap to the same signed value and stay equal; two distinct
    values wrap to distinct signed values and stay distinct.
    """
    if value > _SQLITE_INT64_MAX:
        return value - 2**64
    return value


def _from_db_int64(value: int) -> int:
    """Inverse of `_to_db_int64` — restores the original unsigned 64-bit `st_dev`/`st_ino`
    value from what's stored in SQLite, so a `FileRecord` read back from the index compares
    equal to a live `os.stat()` value for the same file, not just to other DB-sourced records."""
    if value < 0:
        return value + 2**64
    return value


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    git_repo_root = row["git_repo_root"]
    return FileRecord(
        path=Path(row["path"]),
        is_dir=bool(row["is_dir"]),
        size_bytes=row["size"],
        attributes=row["attributes"],
        ext=row["ext"],
        git_repo_root=Path(git_repo_root) if git_repo_root is not None else None,
        git_repo_clean=bool(row["git_repo_clean"]),
        mtime=row["mtime"],
        ctime=row["ctime"],
        dev=_from_db_int64(row["dev"]),
        ino=_from_db_int64(row["ino"]),
    )


def _record_to_row(record: FileRecord, scanned_at: float) -> tuple[object, ...]:
    posix_path = record.path.as_posix()
    return (
        posix_path,
        record.size_bytes,
        record.mtime,
        record.ctime,
        record.ext,
        record.attributes,
        _to_db_int64(record.dev),
        _to_db_int64(record.ino),
        int(record.is_dir),
        int(record.is_cloud_placeholder),
        int(record.is_reparse_point),
        record.git_repo_root.as_posix() if record.git_repo_root is not None else None,
        int(record.git_repo_clean),
        scanned_at,
        record.path.name.lower(),
        posix_path.lower(),
    )


def file_row(
    *,
    posix_path: str,
    name: str,
    size: int,
    mtime: float,
    ctime: float,
    ext: str,
    attributes: int,
    dev: int,
    ino: int,
    is_dir: bool,
    git_repo_root_posix: str | None,
    git_repo_clean: bool,
    scanned_at: float,
) -> tuple[object, ...]:
    """The `files` row `_record_to_row` would produce, built from plain strings/numbers so the
    scanner's hot path never has to construct a `Path` + `FileRecord` per file just to have them
    taken apart again. Same column order as `_COLUMNS` by construction (one layout, two entry
    points); tests/test_scanner_listing.py asserts row-for-row equality with `_record_to_row`."""
    return (
        posix_path,
        size,
        mtime,
        ctime,
        ext,
        attributes,
        _to_db_int64(dev),
        _to_db_int64(ino),
        int(is_dir),
        int(bool(attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)),
        int(bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)),
        git_repo_root_posix,
        int(git_repo_clean),
        scanned_at,
        name.lower(),
        posix_path.lower(),
    )


# One row per distinct on-disk file (`dev`, `ino`) within each size, so a bucket's member count is
# the number of DISTINCT files, not of path names: N names hardlinked to one inode are one file and
# can reclaim nothing. `ino = 0` means "identity unknown" (synthetic rows; `dedup._hash_stage`
# never collapses them either), so each such row keeps its own `rowid` as identity. SQLite has no
# `COUNT(DISTINCT dev, ino)`, hence the GROUP BY subquery. Consumers add `HAVING` on the count.
#
# `NOT INDEXED`: a deliberate plan pin. This is a whole-table aggregate (every row is read exactly
# once), and the only plan that is cheap for that is a sequential table scan. Without `sqlite_stat1`
# SQLite happens to pick one (the one-valued `idx_files_is_cloud_placeholder`, which walks rows in
# rowid order); once the scan-end full ANALYZE (#117) exists it prefers `idx_files_size (size>?)`,
# a NON-covering index, so every one of the ~5.8M entries costs a random row lookup for
# `is_dir`/`dev`/`ino`. Measured on a copy of the real 4.89 GB index (CPU-contended box, so wall
# times are inflated; CPU seconds are the cleaner signal): the candidate count went 53 s ->
# 147-174 s wall (45 -> 120-144 s CPU) after ANALYZE, and 29 s -> 1,163 s (cold cache) for the
# pre-#131 `GROUP BY size` form. Pinned, it is 39-63 s wall / 35-43 s CPU with or without stats.
_DISTINCT_INODES_PER_SIZE_SQL = """
    SELECT size FROM files NOT INDEXED
    WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
    GROUP BY size, (ino = 0), CASE WHEN ino = 0 THEN 0 ELSE dev END,
             CASE WHEN ino = 0 THEN rowid ELSE ino END
"""
# Grouping for `ScanIndex.physical_size_bytes_total`: one group per (dev, ino); the CASE gives every
# dev == ino == 0 row its own group (identity unknown -- never deduplicated, as in the Python
# `physical_size_bytes`). The bare `size` next to MIN(...) is the size of that first row.
_PHYSICAL_GROUP_BY = "GROUP BY dev, ino, CASE WHEN dev = 0 AND ino = 0 THEN rowid ELSE 0 END"
# Sizes whose bucket clears the materiality floor at distinct-inode level (param: the floor).
# S608: only module-level constants are interpolated, never a caller-supplied value.
_QUALIFYING_SIZES_SQL = f"""
    SELECT size FROM ({_DISTINCT_INODES_PER_SIZE_SQL})
    GROUP BY size HAVING COUNT(*) >= 2 AND (COUNT(*) - 1) * size >= ?
"""  # noqa: S608


class ScanIndex:
    """SQLite-backed inventory of every filesystem entry the scanner has seen.

    Deliberately does not import or call SafetyValidator — that boundary belongs to Stage 3's
    candidate generation. `candidate_inventory()` only ever filters out cloud placeholders
    (deleting a placeholder frees no local space and destroys the cloud copy; it's a fact
    about the entry, not a safety-policy decision), never anything policy-driven.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Wave 1 finding #4 (2026-07-30 real-disk diagnosis): default rollback-journal mode
        # fsyncs on every commit, which made the batched-transaction rewrite in scanner.py's
        # `_BatchIndexWriter` (finding #1 — see its docstring) far less effective than it should
        # be, since each of those batch commits would still pay a full fsync. WAL also lets a
        # concurrent reader (e.g. the dashboard's `/api/scan/status` poll) proceed against the
        # index while a scan's writes are still in flight, which DELETE-mode journaling blocks.
        # `synchronous=NORMAL` is the documented safe pairing with WAL (only FULL survives an OS
        # crash with zero possible loss; NORMAL can lose the last few not-yet-checkpointed
        # commits on a power-loss/OS crash specifically, never on an application crash — an
        # acceptable trade for a rebuildable local scan index, never applied to the quarantine
        # manifest/vault, which stay on their own separate, still-fsync-durable write paths).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 2026-09-23 audit finding: a single long scan session commits enough WAL traffic to
        # cross `wal_autocheckpoint`'s default 1000-page threshold many times over, but a PASSIVE
        # auto-checkpoint (what that threshold triggers) reclaims frames logically without ever
        # shrinking the *file* back down -- so the WAL's on-disk size permanently reflects its
        # all-time peak, not its current content. Found stuck at 4.85GB (vs. a 3GB main DB) on a
        # real, month-old index from one big scan, long after every frame in it had already been
        # checkpointed (confirmed: `wal_checkpoint(TRUNCATE)` against it returned 0 log_frames).
        # `journal_size_limit` caps how large the file is allowed to remain after SQLite's own
        # checkpoints; `close()` below adds an explicit TRUNCATE checkpoint so a session that
        # never crosses the threshold still leaves a near-zero WAL on disk when it ends.
        self._conn.execute("PRAGMA journal_size_limit=67108864")  # 64MB
        self._conn.execute(_SCHEMA)
        self._conn.execute(_INACCESSIBLE_SCHEMA)
        self._ensure_name_and_path_lower_columns()
        for statement in _INDEXES:
            self._conn.execute(statement)
        self._conn.commit()

    def _ensure_name_and_path_lower_columns(self) -> None:
        """Migration for an index created before `name`/`path_lower` existed: `_SCHEMA`'s
        `CREATE TABLE IF NOT EXISTS` is a no-op against an already-existing `files` table, so a
        pre-existing DB needs an explicit `ALTER TABLE` + one-time backfill. A brand-new DB
        already has both columns from `_SCHEMA` and this returns immediately."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(files)")}
        if "name" in columns and "path_lower" in columns:
            return
        if "name" not in columns:
            self._conn.execute("ALTER TABLE files ADD COLUMN name TEXT")
        if "path_lower" not in columns:
            self._conn.execute("ALTER TABLE files ADD COLUMN path_lower TEXT")
        self._conn.commit()
        self._backfill_name_and_path_lower()

    def _backfill_name_and_path_lower(self) -> None:
        """Streams every row missing `name`/`path_lower` via `fetchmany` (never `fetchall`) and
        writes the backfill in batches — a multi-million-row legacy index must not be
        materialized into memory just to migrate it, which would defeat the entire point of
        this schema change before a single detector query even runs."""
        select_cursor = self._conn.execute(
            "SELECT rowid, path FROM files WHERE name IS NULL OR path_lower IS NULL"
        )
        batch = select_cursor.fetchmany(_MIGRATION_BATCH_SIZE)
        while batch:
            updates = [
                (Path(row["path"]).name.lower(), row["path"].lower(), row["rowid"]) for row in batch
            ]
            self._conn.executemany(
                "UPDATE files SET name = ?, path_lower = ? WHERE rowid = ?", updates
            )
            self._conn.commit()
            batch = select_cursor.fetchmany(_MIGRATION_BATCH_SIZE)

    def __enter__(self) -> ScanIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def has_planner_stats(self) -> bool:
        """True iff `ANALYZE` has populated `sqlite_stat1` for the `files` table."""
        has_table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'"
        ).fetchone()
        if has_table is None:
            return False
        row = self._conn.execute("SELECT 1 FROM sqlite_stat1 WHERE tbl = 'files' LIMIT 1")
        return row.fetchone() is not None

    def refresh_planner_stats(self, *, only_if_missing: bool = False) -> float:
        """Runs a FULL `ANALYZE files` and returns the seconds it took (0.0 if skipped).

        Why: with no `sqlite_stat1`, SQLite assumes every index is equally selective and picked
        `idx_files_is_cloud_placeholder` (one value in practice) over the primary-key path range
        for prefix-scoped queries, walking most of a 5.86M-row table (8.8 s per call; see
        `subtree_entry_count`). Measured on a copy of the real 4.89 GB index: full ANALYZE
        11-17 s, after which the plan flips to the PK range even without the unary `+` hint.

        Deliberately NOT `PRAGMA optimize` / `analysis_limit`: bounded analysis samples the first
        N entries of each index, and a low-cardinality index (`is_cloud_placeholder`, every value
        0) then reports ~1000 rows per key instead of ~5.8M -- measured, the bad plan survived
        both `PRAGMA optimize` and `analysis_limit=1000`. Full ANALYZE is the only variant that
        fixed the plans. An index that has never been analyzed (e.g. one built before this
        existed) therefore gets its statistics at the end of its next scan.
        """
        if only_if_missing and self.has_planner_stats():
            return 0.0
        start = time.perf_counter()
        # An explicit limit of 0 = unbounded, in case a caller/connection configured one.
        self._conn.execute("PRAGMA analysis_limit=0")
        self._conn.execute("ANALYZE files")
        self._conn.commit()
        return time.perf_counter() - start

    @property
    def db_path(self) -> Path:
        """Where this index lives (the volume dedup's free-space guard measures)."""
        return self._db_path

    def wal_size_bytes(self) -> int:
        """Current size of the `-wal` sidecar file on disk (0 if absent)."""
        try:
            return self._db_path.with_name(self._db_path.name + "-wal").stat().st_size
        except OSError:
            return 0

    def checkpoint_wal(self, *, truncate_above_bytes: int) -> int:
        """Bounded-WAL housekeeping for a long write loop (ADR-0040 addendum). Runs a cheap,
        non-blocking `PASSIVE` checkpoint, then -- only if the WAL file still exceeds
        `truncate_above_bytes` -- a `TRUNCATE` one, which also shrinks the file. CALLER CONTRACT:
        no read cursor of this connection may be open (an open cursor pins its snapshot and no
        checkpoint can pass it, which is exactly the 13 GB WAL incident). Never raises on a busy
        checkpoint (another connection reading): it just leaves the WAL for the next call.
        Returns the WAL size afterwards."""
        for mode in ("PASSIVE", "TRUNCATE"):
            if mode == "TRUNCATE" and self.wal_size_bytes() <= truncate_above_bytes:
                break
            try:
                self._conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchall()
            except sqlite3.OperationalError:
                break  # locked by a reader: retry at the next window boundary
        return self.wal_size_bytes()

    def close(self) -> None:
        # TRUNCATE checkpoint reclaims whatever the session's writes left in the WAL (see the
        # `journal_size_limit` comment in __init__) -- without this, closing never shrinks the
        # file, only SQLite's own automatic checkpoints do, and those don't truncate either.
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()

    def upsert_records(self, records: Iterable[FileRecord], *, scanned_at: float) -> int:
        """Full upsert (all columns) for new or changed records. Returns rows written."""
        return self.upsert_rows([_record_to_row(record, scanned_at) for record in records])

    def upsert_rows(self, rows: Sequence[tuple[object, ...]]) -> int:
        """`upsert_records` for rows already in `_COLUMNS` order (`_record_to_row`/`file_row`)."""
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in _COLUMNS)
        update_clause = ", ".join(f"{col}=excluded.{col}" for col in _COLUMNS if col != "path")
        # S608: every interpolated fragment here comes from the module-level `_COLUMNS`
        # constant, never from caller/user input — nothing here is attacker-controlled.
        self._conn.executemany(
            f"INSERT INTO files ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT(path) DO UPDATE SET {update_clause}",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def prune_missing(self, indexed_paths: Iterable[str], seen_paths: Iterable[str]) -> int:
        """Deletes rows for every path in `indexed_paths` (what a prior scan of this root
        found) that isn't also in `seen_paths` (what this scan actually walked) — i.e. files
        that were indexed previously but no longer exist. Callers pass an already root-scoped
        `indexed_paths` (e.g. from `load_stat_cache(root).keys()`) so a scan of one subtree
        never deletes rows outside it.

        Deliberately a set-difference against what was actually walked, not a `last_scanned`
        timestamp comparison: an unchanged file's row is never rewritten just to "prove" it's
        still there (see `upsert_records`), so a timestamp-based staleness check would treat
        every unchanged file as stale after its first scan. `last_scanned` is still persisted
        (updated whenever a row is written) for observability, but pruning never depends on it.
        """
        stale = set(indexed_paths) - set(seen_paths)
        if not stale:
            return 0
        self._conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in stale])
        self._conn.commit()
        return len(stale)

    # --- Wave 1 finding #1/#4: streaming-safe staleness tracking --------------------------------
    #
    # `prune_missing` above needs two full Python collections (`indexed_paths`/`seen_paths`) held
    # in memory to compute its set-difference — fine at unit-test scale, but `scan_tree` used to
    # build `seen_paths` by accumulating one `FileRecord` per visited entry across an entire
    # multi-million-file walk before ever calling it (the exact bug the 2026-07-30 real-disk
    # diagnosis measured at 5,085MB peak RSS for 2.67M files — see PLAN.md's Wave 1 checkpoint).
    # `begin_scan_tracking`/`record_seen`/`prune_unseen_under_root` move that same set-difference
    # INTO SQLite (a temp table, batch-appended as the walk progresses) so `scan_tree` never
    # holds more than one small batch of paths in Python memory at a time, while still preserving
    # `prune_missing`'s own documented choice to evaluate an EXACT set-difference rather than a
    # `last_scanned` timestamp comparison (a timestamp approach would require rewriting every
    # unchanged file's row just to bump its timestamp, undoing the whole point of the incremental
    # skip below — see `upsert_records`' docstring reasoning, unchanged here).

    def begin_scan_tracking(self) -> None:
        """Starts a new staleness-tracking session for one `scan_tree` call: drops any leftover
        temp table from a prior session on this same connection (e.g. an aborted scan) and
        creates a fresh one. Temp tables are connection-scoped and never touch the real `files`
        table or the on-disk `.sqlite3` file."""
        self._conn.execute("DROP TABLE IF EXISTS temp.scan_seen")
        self._conn.execute("CREATE TEMP TABLE scan_seen (path TEXT PRIMARY KEY)")

    def record_seen(self, paths: Sequence[str]) -> None:
        """Batch-appends paths visited so far this scan into the tracking table — called once
        per `_BatchIndexWriter` flush (a bounded batch, never the whole walk) for both changed
        AND unchanged entries, since `prune_unseen_under_root` needs to know everything that
        still exists on disk, not just what got rewritten."""
        if not paths:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO scan_seen (path) VALUES (?)", [(p,) for p in paths]
        )
        self._conn.commit()

    def prune_unseen_under_root(self, root: Path) -> int:
        """Deletes every row under `root` (same `path = ? OR (path >= lo AND path < hi)` scoping
        `load_stat_cache`/`direct_children` already use, via `_prefix_range`) whose path was
        never passed to `record_seen` this session — the exact set-difference `prune_missing`
        computes, evaluated entirely inside SQLite via an anti-join against the temp table
        instead of two full Python collections. Must be called after every real entry under
        `root` has reached `record_seen` at least once."""
        prefix = root.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        cursor = self._conn.execute(
            "DELETE FROM files WHERE (path = ? OR (path >= ? AND path < ?)) "
            "AND path NOT IN (SELECT path FROM temp.scan_seen)",
            (prefix, lower, upper),
        )
        self._conn.commit()
        return cursor.rowcount

    def protect_under(self, paths: Iterable[str]) -> int:
        """Marks every existing row at or under each of `paths` (POSIX form) as seen, so
        `prune_unseen_under_root` keeps it. Returns the number of path prefixes protected.

        Fail-closed pruning: a directory the scan could not list (permission change, offline
        network root, I/O fault) was never walked, so its rows are absent from `scan_seen`
        for a reason that says nothing about whether the files still exist. Without this the
        prune would delete every row under it. Same prefix-range scoping as the prune itself."""
        count = 0
        for path in paths:
            prefix = path.rstrip("/")
            lower, upper = _prefix_range(prefix)
            self._conn.execute(
                "INSERT OR IGNORE INTO scan_seen (path) SELECT path FROM files "
                "WHERE path = ? OR (path >= ? AND path < ?)",
                (prefix, lower, upper),
            )
            count += 1
        self._conn.commit()
        return count

    def page_rows_after(
        self, after: str, *, limit: int, prefix: str | None = None
    ) -> list[tuple[str, int, bool]]:
        """Up to `limit` `(path, size, is_dir)` rows with `path > after`, in path order
        (keyset pagination over the primary key: stable while rows are deleted behind the
        cursor), optionally limited to `prefix` itself and everything under it."""
        if prefix is None:
            cursor = self._conn.execute(
                "SELECT path, size, is_dir FROM files WHERE path > ? ORDER BY path LIMIT ?",
                (after, limit),
            )
        else:
            stripped = prefix.rstrip("/")
            lower, upper = _prefix_range(stripped)
            cursor = self._conn.execute(
                "SELECT path, size, is_dir FROM files "
                "WHERE path > ? AND (path = ? OR (path >= ? AND path < ?)) "
                "ORDER BY path LIMIT ?",
                (after, stripped, lower, upper, limit),
            )
        return [(row["path"], int(row["size"]), bool(row["is_dir"])) for row in cursor]

    def delete_paths(self, paths: Sequence[str]) -> int:
        """Deletes the rows whose exact `path` is in `paths` (primary-key point deletes)."""
        if not paths:
            return 0
        self._conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])
        self._conn.commit()
        return len(paths)

    def vacuum(self) -> None:
        """Rebuilds the database file so pages freed by deletes are returned to the OS. Needs
        free disk roughly equal to the database size and an exclusive lock (fails with
        `sqlite3.OperationalError` if another connection holds the database open)."""
        self._conn.commit()
        self._conn.execute("VACUUM")
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def end_scan_tracking(self) -> None:
        """Drops the temp table — always safe to call (even if `begin_scan_tracking` never ran,
        e.g. an early failure), since a dropped connection-scoped temp table costs nothing to
        recreate next time."""
        self._conn.execute("DROP TABLE IF EXISTS temp.scan_seen")

    # --- P0-5: persisted accounting for directories/entries the scanner couldn't fully account
    # for --------------------------------------------------------------------------------------

    def replace_inaccessible_under_root(
        self, root: Path, entries: Sequence[InaccessibleEntry], *, scanned_at: float
    ) -> None:
        """Replaces every `inaccessible_paths` row under `root` with `entries` -- the full,
        authoritative set a COMPLETE `scan_tree` walk of `root` actually produced, never a
        merge. A previously-inaccessible path that's listable again now (an ACL was relaxed, the
        directory was deleted) must not linger in this table forever -- unlike `files`'
        incremental skip-unchanged design, an `inaccessible_paths` set is always small (a real
        scan skips a handful to a few hundred paths, never millions), so a full
        delete-then-insert under `root` on every completed scan is simplest and correct, with no
        need for `files`' streaming seen-tracking machinery (Wave 1 finding #1/#4). Callers must
        never call this for a CANCELLED scan -- see `scan_tree`'s own docstring for why (same
        reasoning as `prune_unseen_under_root` being skipped there).
        """
        prefix = root.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        self._conn.execute(
            "DELETE FROM inaccessible_paths WHERE path = ? OR (path >= ? AND path < ?)",
            (prefix, lower, upper),
        )
        if entries:
            self._conn.executemany(
                "INSERT INTO inaccessible_paths "
                "(path, error, size_estimate_bytes, size_estimate_is_lower_bound, last_scanned) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET error=excluded.error, "
                "size_estimate_bytes=excluded.size_estimate_bytes, "
                "size_estimate_is_lower_bound=excluded.size_estimate_is_lower_bound, "
                "last_scanned=excluded.last_scanned",
                [
                    (
                        entry.path,
                        entry.error,
                        entry.size_estimate_bytes,
                        int(entry.size_estimate_is_lower_bound),
                        scanned_at,
                    )
                    for entry in entries
                ],
            )
        self._conn.commit()

    def inaccessible_summary(self, under: Path | None = None) -> InaccessibleSummary:
        """`(path_count, known_bytes, unknown_count)` over `inaccessible_paths`, optionally
        scoped under `under` -- see `InaccessibleSummary`'s docstring for what `known_bytes`
        deliberately excludes."""
        where = ""
        params: list[object] = []
        if under is not None:
            prefix = under.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            where = " WHERE path = ? OR (path >= ? AND path < ?)"
            params = [prefix, lower, upper]
        # S608: `where` is built only from the fixed literal string above -- no caller-supplied
        # value is ever interpolated into the SQL text, only bound as a `?` parameter.
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS path_count, "  # noqa: S608
            "COALESCE(SUM(size_estimate_bytes), 0) AS known_bytes, "
            "SUM(CASE WHEN size_estimate_bytes IS NULL THEN 1 ELSE 0 END) AS unknown_count "
            f"FROM inaccessible_paths{where}",
            params,
        )
        row = cursor.fetchone()
        return InaccessibleSummary(
            path_count=int(row["path_count"]),
            known_bytes=int(row["known_bytes"]),
            unknown_count=int(row["unknown_count"] or 0),
        )

    def inaccessible_paths_sample(
        self, *, limit: int, under: Path | None = None
    ) -> list[InaccessibleEntry]:
        """Up to `limit` `inaccessible_paths` rows (ordered by path, for a deterministic sample),
        optionally scoped under `under` -- mirrors `ScanStats.skipped_unreadable_paths`' own
        "count is exact, sample is capped" shape at the persisted-index layer."""
        where = ""
        params: list[object] = []
        if under is not None:
            prefix = under.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            where = " WHERE path = ? OR (path >= ? AND path < ?)"
            params = [prefix, lower, upper]
        cursor = self._conn.execute(
            "SELECT path, error, size_estimate_bytes, size_estimate_is_lower_bound "  # noqa: S608
            f"FROM inaccessible_paths{where} ORDER BY path LIMIT ?",
            [*params, limit],
        )
        return [
            InaccessibleEntry(
                path=row["path"],
                error=row["error"],
                size_estimate_bytes=row["size_estimate_bytes"],
                size_estimate_is_lower_bound=bool(row["size_estimate_is_lower_bound"]),
            )
            for row in cursor
        ]

    def load_stat_cache(self, root: Path | None = None) -> dict[str, StoredStat]:
        """Loads path -> (size, mtime) for every indexed row (optionally scoped under `root`)
        in one query, so the scanner's per-entry incremental compare never round-trips to
        SQLite per file."""
        if root is None:
            cursor = self._conn.execute("SELECT path, size, mtime FROM files")
        else:
            prefix = root.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            cursor = self._conn.execute(
                "SELECT path, size, mtime FROM files WHERE path = ? OR (path >= ? AND path < ?)",
                (prefix, lower, upper),
            )
        return {row["path"]: StoredStat(size=row["size"], mtime=row["mtime"]) for row in cursor}

    def load_hash_cache(self, root: Path | None = None) -> dict[str, HashCacheEntry]:
        """Loads path -> cached hash entry for every row with a computed hash (optionally
        scoped under `root`), mirroring `load_stat_cache`'s one-query-not-per-file shape."""
        base = "SELECT path, hash_size, hash_mtime, partial_hash, full_hash FROM files"
        if root is None:
            cursor = self._conn.execute(f"{base} WHERE hash_size IS NOT NULL")
        else:
            prefix = root.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            cursor = self._conn.execute(
                f"{base} WHERE hash_size IS NOT NULL AND (path = ? OR (path >= ? AND path < ?))",
                (prefix, lower, upper),
            )
        return {
            row["path"]: HashCacheEntry(
                hash_size=row["hash_size"],
                hash_mtime=row["hash_mtime"],
                partial_hash=row["partial_hash"],
                full_hash=row["full_hash"],
            )
            for row in cursor
        }

    def store_partial_hashes(self, entries: Iterable[tuple[Path, int, float, str]]) -> int:
        """Batch-writes `(path, size, mtime, partial_hash)` tuples for rows that already exist
        (the dedup pipeline only ever hashes files already present in the index). Leaves
        `full_hash` untouched so a partial-hash pass never clobbers a previously cached
        full-hash value for the same row."""
        # The sibling hash column is kept ONLY if it was computed against this same (size, mtime):
        # hash_size/hash_mtime are shared by both hashes, so re-stamping them for a changed file
        # would otherwise make the OLD full_hash look valid for the NEW content on the next load.
        rows = [
            (size, mtime, digest, path.as_posix(), size, mtime)
            for path, size, mtime, digest in entries
        ]
        if not rows:
            return 0
        self._conn.executemany(
            "UPDATE files SET full_hash = CASE WHEN hash_size IS ?5 AND hash_mtime IS ?6 "
            "THEN full_hash ELSE NULL END, hash_size = ?1, hash_mtime = ?2, partial_hash = ?3 "
            "WHERE path = ?4",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def store_full_hashes(self, entries: Iterable[tuple[Path, int, float, str]]) -> int:
        """Batch-writes `(path, size, mtime, full_hash)` tuples; see `store_partial_hashes`."""
        rows = [
            (size, mtime, digest, path.as_posix(), size, mtime)
            for path, size, mtime, digest in entries
        ]
        if not rows:
            return 0
        self._conn.executemany(
            "UPDATE files SET partial_hash = CASE WHEN hash_size IS ?5 AND hash_mtime IS ?6 "
            "THEN partial_hash ELSE NULL END, hash_size = ?1, hash_mtime = ?2, full_hash = ?3 "
            "WHERE path = ?4",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def get_record(self, path: Path) -> FileRecord | None:
        """Single indexed point lookup on the primary key. The SQL-pushdown replacement for
        looking a path up in an in-memory `{path: FileRecord}` dict built from a full-table
        load — used once a detector has already decided, from its own narrow indexed query, to
        propose a path, and just needs the full record back to build a `Candidate` from it."""
        cursor = self._conn.execute("SELECT * FROM files WHERE path = ?", (path.as_posix(),))
        row = cursor.fetchone()
        return _row_to_record(row) if row is not None else None

    def record_exists(self, path: Path) -> bool:
        """Cheap existence check (e.g. "is there a `package.json` at this exact parent
        directory") without even reconstructing a `FileRecord` — the point-lookup replacement
        for `path in ctx.by_path`."""
        cursor = self._conn.execute(
            "SELECT 1 FROM files WHERE path = ? LIMIT 1", (path.as_posix(),)
        )
        return cursor.fetchone() is not None

    def files_by_name(
        self, names: Sequence[str], *, is_dir: bool | None = None
    ) -> Iterator[FileRecord]:
        """Streams every row whose lowercased basename is in `names` via the indexed `name`
        column — O(matches), never a full-table load. Case-insensitive, matching the
        directory-name-keyed dev-artifact detectors (`node_modules`, `.venv`, ...)."""
        if not names:
            return
        placeholders = ", ".join("?" for _ in names)
        # S608: placeholders are `?` markers only; every value is bound as a parameter below.
        sql = f"SELECT * FROM files WHERE name IN ({placeholders})"  # noqa: S608
        params: list[object] = [name.lower() for name in names]
        if is_dir is not None:
            sql += " AND is_dir = ?"
            params.append(int(is_dir))
        for row in self._conn.execute(sql, params):
            yield _row_to_record(row)

    def files_by_ext(
        self, exts: Sequence[str], *, is_dir: bool | None = None
    ) -> Iterator[FileRecord]:
        """Streams every row whose extension is in `exts` via the indexed `ext` column."""
        if not exts:
            return
        placeholders = ", ".join("?" for _ in exts)
        sql = f"SELECT * FROM files WHERE ext IN ({placeholders})"  # noqa: S608
        params: list[object] = [ext.lower() for ext in exts]
        if is_dir is not None:
            sql += " AND is_dir = ?"
            params.append(int(is_dir))
        for row in self._conn.execute(sql, params):
            yield _row_to_record(row)

    def files_larger_than(
        self, min_size_bytes: int, *, is_dir: bool = False
    ) -> Iterator[FileRecord]:
        """Streams every row at or above `min_size_bytes` via the indexed `size` column — most
        files on a real disk are far smaller than a large-log threshold (default 50MB), so this
        narrows a whole-index scan down to a small minority before any Python-side filtering."""
        cursor = self._conn.execute(
            "SELECT * FROM files WHERE size >= ? AND is_dir = ?", (min_size_bytes, int(is_dir))
        )
        for row in cursor:
            yield _row_to_record(row)

    def files_matching_path_pattern(
        self, glob_pattern: str, *, is_dir: bool | None = None
    ) -> Iterator[FileRecord]:
        """Streams rows whose posix path matches `glob_pattern` (an `fnmatch`-style pattern
        with `*`/`?` wildcards — the same patterns `config.categories.*.paths`/`cache_paths`/
        `temp_roots` already use), translated to a SQL `LIKE` pattern against the indexed
        `path_lower COLLATE NOCASE` column.

        Deliberately does *not* escape a literal `%`/`_` in the pattern before translating:
        SQLite disables its LIKE-to-index-range-scan optimization the moment an `ESCAPE` clause
        is present, even on this exact index (confirmed empirically — identical query and
        index, only the `ESCAPE` clause differs, and the plan degrades from
        `SEARCH ... USING INDEX` to a full `SCAN`). None of this project's actual category-config
        patterns contain a literal `%`/`_` that would need escaping, and `fnmatch` itself has no
        escape mechanism for its own `*`/`?`/`[]` metacharacters either — this is a different
        instance of the same pre-existing class of limitation, not a new regression.
        """
        like_pattern = glob_pattern.lower().replace("*", "%").replace("?", "_")
        sql = "SELECT * FROM files WHERE path_lower LIKE ?"
        params: list[object] = [like_pattern]
        if is_dir is not None:
            sql += " AND is_dir = ?"
            params.append(int(is_dir))
        for row in self._conn.execute(sql, params):
            yield _row_to_record(row)

    def duplicate_size_candidates(
        self, *, min_reclaim_bytes: int
    ) -> Generator[FileRecord, None, None]:
        """Streams every non-directory, non-empty, non-cloud-placeholder file whose `size`
        collides with at least one other such file *and* whose bucket clears the materiality
        floor — the SQL-pushed equivalent of the old in-memory `_size_buckets()` prefilter in
        `dedup.py`. A unique-size file is never selected by this query, let alone loaded into a
        Python `FileRecord`.

        `min_reclaim_bytes` is the materiality gate (2026-07-17 real-disk finding): a bucket's
        *theoretical* best-case reclaim is `(distinct_inodes - 1) * size` (every non-kept file
        turning out to be an exact duplicate; names hardlinked to one inode are ONE file, so a
        bucket made only of hardlink names has a best case of 0 -- ADR-0002 addendum) — below
        `min_reclaim_bytes`, the bucket is
        excluded from this stream entirely, before a single byte is read. On one real `C:\\`,
        80% of files shared a size with another file, but the collision list was dominated by
        empty/near-empty files (333K zero-byte, thousands of 2/4/17-byte files) whose full
        bucket could never reclaim anything material even in the best case — hashing them
        wasted I/O for zero possible benefit. `size > 0` alone (already present below) already
        excludes zero-byte files; `min_reclaim_bytes` extends the same idea to any bucket whose
        upper-bound reclaim is still negligible. See `immaterial_duplicate_bucket_stats` for the
        excluded side of this filter, surfaced to the report rather than silently dropped.

        `ORDER BY size` costs nothing extra here (confirmed via `EXPLAIN QUERY PLAN`: no
        separate "USE TEMP B-TREE FOR ORDER BY" step appears, since scanning `idx_files_size`
        already visits rows in size order) and lets `dedup.py` consume this stream one size
        bucket at a time (`itertools.groupby`) instead of collecting every candidate row into
        memory before processing any of them.
        """
        sql = f"""
            SELECT * FROM files
            WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
            AND size IN ({_QUALIFYING_SIZES_SQL})
            ORDER BY size
        """  # noqa: S608 -- constants only; floor is a bound parameter
        for row in self._conn.execute(sql, (min_reclaim_bytes,)):
            yield _row_to_record(row)

    def duplicate_qualifying_sizes(self, *, min_reclaim_bytes: int) -> list[int]:
        """The ascending list of sizes `duplicate_size_candidates()` would stream. Runs the
        expensive qualifying-sizes subquery ONCE (15-60 s on a real index) and is fully consumed
        before returning, so no read cursor stays open."""
        sql = f"""
            SELECT size FROM ({_QUALIFYING_SIZES_SQL}) ORDER BY size
        """  # noqa: S608 -- constants only; floor is a bound parameter
        return [int(row["size"]) for row in self._conn.execute(sql, (min_reclaim_bytes,))]

    def duplicate_candidates_for_sizes(self, sizes: Sequence[int]) -> list[FileRecord]:
        """The same rows `duplicate_size_candidates()` yields, restricted to `sizes` and ordered
        `(size, rowid)`. This is NOT claimed to be the streaming query's visit order: on an
        un-ANALYZEd index the old plan used `idx_files_is_cloud_placeholder` plus a temp b-tree
        for ORDER BY; the rowid order within a size was identical empirically. Fully
        consumed (`fetchall`) before returning: dedup writes hashes between calls, and an open
        cursor would pin a read snapshot so the WAL could not be checkpointed (13 GB incident,
        2026-10-08). `sizes` must be a bounded chunk (SQLite's bound-variable limit applies)."""
        if not sizes:
            return []
        placeholders = ", ".join("?" for _ in sizes)
        sql = f"""
            SELECT * FROM files
            WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
            AND size IN ({placeholders})
            ORDER BY size, rowid
        """  # noqa: S608 -- placeholders are `?` markers only; every value is bound
        return [_row_to_record(row) for row in self._conn.execute(sql, list(sizes)).fetchall()]

    def duplicate_size_candidate_count(self, *, min_reclaim_bytes: int) -> int:
        """A cheap `COUNT(*)` over the same filter `duplicate_size_candidates()` streams —
        logged once up front so a heartbeat can report "N of M processed" instead of just a
        running count with no sense of how much work remains."""
        sql = f"""
            SELECT COUNT(*) AS total FROM files
            WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
            AND size IN ({_QUALIFYING_SIZES_SQL})
        """  # noqa: S608 -- constants only; floor is a bound parameter
        row = self._conn.execute(sql, (min_reclaim_bytes,)).fetchone()
        return int(row["total"])

    def immaterial_duplicate_bucket_stats(self, *, min_reclaim_bytes: int) -> tuple[int, int]:
        """Returns `(bucket_count, theoretical_bytes)` for size buckets that collide (>= 2
        distinct files, not merely >= 2 hardlink names of one) but were excluded from
        `duplicate_size_candidates()` for falling below `min_reclaim_bytes` — surfaced so the
        report can show what was skipped and why, rather
        than the exclusion being silent. `theoretical_bytes` is a labeled upper bound (every
        member turning out to be an exact duplicate), never a claim about real measured
        reclaim — this tool never fabricates confidence it hasn't earned by actually hashing.
        """
        sql = f"""
            SELECT COUNT(*) AS bucket_count, COALESCE(SUM((c - 1) * size), 0) AS theoretical_bytes
            FROM (
                SELECT size, COUNT(*) AS c FROM ({_DISTINCT_INODES_PER_SIZE_SQL})
                GROUP BY size
                HAVING COUNT(*) >= 2 AND (COUNT(*) - 1) * size < ?
            )
        """  # noqa: S608 -- constants only; floor is a bound parameter
        row = self._conn.execute(sql, (min_reclaim_bytes,)).fetchone()
        return int(row["bucket_count"]), int(row["theoretical_bytes"])

    def subtree_size_bytes(self, root: Path) -> int:
        """Sum of `size` for every non-directory row at or under `root` — the aggregate size a
        directory-level candidate (e.g. a `node_modules` dir) represents.

        Logical sum (matches `logical_size_bytes` semantics), not hardlink-deduped: package/
        dependency-cache trees are vanishingly unlikely to contain internal hardlinks, so the
        simpler prefix-sum SQL query is preferred here over a second physical-size code path.
        """
        prefix = root.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        cursor = self._conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS total FROM files "
            "WHERE (path = ? OR (path >= ? AND path < ?)) AND is_dir = 0",
            (prefix, lower, upper),
        )
        row = cursor.fetchone()
        return int(row["total"])

    def subtree_newest_mtime(self, root: Path) -> float | None:
        """Newest `mtime` of `root` itself or any row under it, or None if nothing is indexed
        there. A directory's own mtime changes only when its direct entries are added, removed,
        or renamed (NTFS), not when a file deeper down is rewritten -- so "how recently was this
        tree used" must be answered from the subtree, never from the directory row alone."""
        prefix = root.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        cursor = self._conn.execute(
            "SELECT MAX(mtime) AS newest FROM files WHERE path = ? OR (path >= ? AND path < ?)",
            (prefix, lower, upper),
        )
        row = cursor.fetchone()
        return None if row["newest"] is None else float(row["newest"])

    def newest_files_under(self, root: Path, *, limit: int) -> list[Path]:
        """Paths of up to `limit` non-directory rows under `root`, newest indexed `mtime`
        first. Bounded by `limit`, so a caller that re-`stat`s the result (the ADR-0035
        decision-point re-check) pays O(limit) per subtree however large the subtree is."""
        prefix = root.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        cursor = self._conn.execute(
            "SELECT path FROM files WHERE path >= ? AND path < ? AND is_dir = 0 "
            "ORDER BY mtime DESC LIMIT ?",
            (lower, upper, limit),
        )
        return [Path(row["path"]) for row in cursor]

    def subtree_entry_count(self, under: Path) -> int:
        """Cheap `COUNT(*)` over the same rows `candidate_inventory(under=...)` would return,
        without materializing a single `FileRecord` -- used purely to decide whether a live
        full-subtree re-walk (`executor._direct_delete_directory_mismatch`, gated by
        `apply_batch`'s entry-count guard -- P0-K1a/M1 cost-budget follow-up) is affordable
        BEFORE attempting it, never as a safety-relevant count itself. The re-walk, when it does
        run, is still the actual source of truth for identity comparisons -- this is a pure
        cost-estimation query over the last scan's own recorded shape of this subtree, which is
        by definition all this decision can see before paying the re-walk's own live-filesystem
        cost. Counts both files and directories (unlike `subtree_size_bytes`, which sums only
        file rows) because the re-walk itself visits and compares both.
        """
        prefix = under.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS total FROM files "
            # Unary `+` on `is_cloud_placeholder` is load-bearing, not noise: it makes that term
            # non-indexable so the planner uses the primary-key path range. Without it (no
            # ANALYZE stats on this DB) SQLite picked `idx_files_is_cloud_placeholder` -- ~every
            # row has value 0 -- and walked most of the table: measured 5.04s vs 0.00s for the same
            # 397-row prefix on the real 5.86M-row index. Same rows either way.
            "WHERE (path = ? OR (path >= ? AND path < ?)) AND +is_cloud_placeholder = 0",
            (prefix, lower, upper),
        )
        row = cursor.fetchone()
        return int(row["total"])

    def has_any_records(self) -> bool:
        """Cheap existence check for the UI's "no scan yet" empty state — `EXISTS(...)` short-
        circuits on the first row instead of materializing the whole inventory just to check
        non-emptiness (Stage 6 addition, additive only)."""
        cursor = self._conn.execute("SELECT EXISTS(SELECT 1 FROM files) AS has_rows")
        return bool(cursor.fetchone()["has_rows"])

    def direct_children(self, parent: Path) -> list[FileRecord]:
        """Immediate children of `parent` only (files and directories one level down) — a real
        SQL prefix query, not a Python filter over `full_inventory`, so the Stage 6 treemap can
        list a directory's contents without materializing an entire (potentially whole-disk)
        subtree into memory just to discard everything below the first level. A row is a direct
        child iff its path starts with `parent/` and contains no further `/` after that prefix.
        """
        prefix = parent.as_posix().rstrip("/")
        lower, upper = _prefix_range(prefix)
        # The primary bound (an indexed range scan) needs no escaping — see _prefix_range's
        # docstring. The residual "exclude grandchildren" check is still a LIKE clause (there's
        # no clean range-comparison equivalent for "contains another '/' after this point"), so
        # it still needs `_escape_like_prefix` for a `prefix` containing a literal `%`/`_`
        # (which real directory names have — e.g. `.../immutable/_app` on a real disk).
        escaped_prefix = _escape_like_prefix(prefix)
        # LIKE-ESCAPE-OK: residual per-row filter over rows the range scan above already
        # narrowed to `parent`'s subtree — not a primary lookup, so the ESCAPE-defeats-index
        # cost doesn't apply here (there's nothing left to scan a full table for). Any *new*
        # `LIKE ... ESCAPE` used as a primary filter should use `_prefix_range` instead — see
        # `tests/test_query_plan_coverage.py`, which greps for unmarked occurrences of this
        # pattern in CI.
        cursor = self._conn.execute(
            "SELECT * FROM files WHERE path >= ? AND path < ? AND path NOT LIKE ? ESCAPE '\\'",
            (lower, upper, f"{escaped_prefix}/%/%"),
        )
        return [_row_to_record(row) for row in cursor]

    def full_inventory(self, under: Path | None = None) -> list[FileRecord]:
        """Everything the scanner has seen, including cloud placeholders — for the treemap
        and total-usage display, which must reflect real disk (and cloud-footprint) usage."""
        return self._query_inventory(under, candidates_only=False)

    def physical_size_bytes_total(self, under: Path | None = None) -> int:
        """SQL-side twin of `physical_size_bytes(self.full_inventory(under))`: the same number,
        computed as one aggregate query without materializing a single `FileRecord` (on a
        6.9M-row index the Python form took minutes per `/api/summary` request).

        Mirrors the Python rule exactly: directories are skipped; cloud placeholders are NOT
        filtered (`full_inventory` includes them); a (dev, ino) pair counts once, first-seen row
        winning (`rowid` order unscoped, `path` order scoped -- the order `full_inventory`
        iterates in for each form); only dev == 0 AND ino == 0 rows each count separately
        (note: ino == 0 with a non-zero dev IS deduplicated by the Python function, unlike
        `_DISTINCT_INODES_PER_SIZE_SQL`, which keys on ino alone).
        """
        if under is None:
            # NOT INDEXED: same whole-table-aggregate plan pin as `_DISTINCT_INODES_PER_SIZE_SQL`
            # (see its comment) -- a sequential scan whether or not `sqlite_stat1` exists.
            sql = f"""
                SELECT COALESCE(SUM(size), 0) AS total FROM (
                    SELECT size, MIN(rowid) FROM files NOT INDEXED WHERE is_dir = 0
                    {_PHYSICAL_GROUP_BY}
                )
            """  # noqa: S608 -- only module-level constants are interpolated
            params: tuple[object, ...] = ()
        else:
            prefix = under.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            # Unary `+` keeps the primary-key path range as the access path (see
            # `subtree_entry_count`); MIN(path) = first row in that range's iteration order.
            sql = f"""
                SELECT COALESCE(SUM(size), 0) AS total FROM (
                    SELECT size, MIN(path) FROM files
                    WHERE (path = ? OR (path >= ? AND path < ?)) AND +is_dir = 0
                    {_PHYSICAL_GROUP_BY}
                )
            """  # noqa: S608 -- only module-level constants are interpolated
            params = (prefix, lower, upper)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["total"])

    def candidate_inventory(self, under: Path | None = None) -> list[FileRecord]:
        """Everything except cloud placeholders, fully materialized into a `list[FileRecord]`.

        Deprecated for whole-index candidate generation: `detectors.py`/`dedup.py` used to call
        this with `under=None` to load the *entire* inventory into memory before running any
        detector — on a real disk-scale index (millions of rows) that means materializing
        millions of Python objects before a single candidate is proposed, which is exactly the
        cost this method's callers were redesigned to avoid (see `files_by_name`/`files_by_ext`/
        `files_larger_than`/`files_matching_path_pattern`/`duplicate_size_candidates` — narrow,
        indexed queries that return only actual matches). No detector or dedup code may call
        this with `under=None` again. Still legitimate for the dashboard's already
        directory-scoped views (`under=<specific subdirectory>`), where the result is bounded by
        that subdirectory's size, not the whole index.
        """
        return self._query_inventory(under, candidates_only=True)

    def _query_inventory(self, under: Path | None, *, candidates_only: bool) -> list[FileRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if under is not None:
            prefix = under.as_posix().rstrip("/")
            lower, upper = _prefix_range(prefix)
            clauses.append("(path = ? OR (path >= ? AND path < ?))")
            params.extend([prefix, lower, upper])
        if candidates_only:
            # Unary `+` ONLY when `under` scopes the query: same planner trap as
            # `subtree_entry_count` (see its comment) -- keeps the primary-key range as the access
            # path. Unscoped (`under=None`) keeps the plain term, whose index is the only filter.
            placeholder_term = (
                "+is_cloud_placeholder = 0" if under is not None else ("is_cloud_placeholder = 0")
            )
            clauses.append(placeholder_term)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # S608: `clauses` is built only from the fixed literal strings above and `?`
        # placeholders — no caller-supplied value is ever interpolated into the SQL text.
        cursor = self._conn.execute(f"SELECT * FROM files{where}", params)  # noqa: S608
        return [_row_to_record(row) for row in cursor]


def logical_size_bytes(records: Iterable[FileRecord]) -> int:
    """Sum of `size_bytes` across every file record — double-counts hardlinks (same on-disk
    allocation, multiple path entries), which is exactly what "logical size" means here."""
    return sum(record.size_bytes for record in records if not record.is_dir)


def physical_size_bytes(records: Iterable[FileRecord]) -> int:
    """Sum of `size_bytes` counting each (dev, ino) allocation exactly once (first-seen wins)
    — the real free-space number, since two hardlinked paths don't cost double the bytes.

    Records with dev == ino == 0 (the FileRecord default for anything not populated by a real
    scan — e.g. Stage 1 fixtures) are never deduped against each other: treating that sentinel
    as a real inode identity would incorrectly collapse unrelated records sharing the default.
    """
    seen: set[tuple[int, int]] = set()
    total = 0
    for record in records:
        if record.is_dir:
            continue
        key = (record.dev, record.ino)
        if key == (0, 0):
            total += record.size_bytes
            continue
        if key in seen:
            continue
        seen.add(key)
        total += record.size_bytes
    return total
