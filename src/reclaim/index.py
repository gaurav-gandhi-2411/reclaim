from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from reclaim.models import FileRecord

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


class ScanIndex:
    """SQLite-backed inventory of every filesystem entry the scanner has seen.

    Deliberately does not import or call SafetyValidator — that boundary belongs to Stage 3's
    candidate generation. `candidate_inventory()` only ever filters out cloud placeholders
    (deleting a placeholder frees no local space and destroys the cloud copy; it's a fact
    about the entry, not a safety-policy decision), never anything policy-driven.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
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

    def close(self) -> None:
        self._conn.close()

    def upsert_records(self, records: Iterable[FileRecord], *, scanned_at: float) -> int:
        """Full upsert (all columns) for new or changed records. Returns rows written."""
        rows = [_record_to_row(record, scanned_at) for record in records]
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
        rows = [(size, mtime, digest, path.as_posix()) for path, size, mtime, digest in entries]
        if not rows:
            return 0
        self._conn.executemany(
            "UPDATE files SET hash_size = ?, hash_mtime = ?, partial_hash = ? WHERE path = ?",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def store_full_hashes(self, entries: Iterable[tuple[Path, int, float, str]]) -> int:
        """Batch-writes `(path, size, mtime, full_hash)` tuples; see `store_partial_hashes`."""
        rows = [(size, mtime, digest, path.as_posix()) for path, size, mtime, digest in entries]
        if not rows:
            return 0
        self._conn.executemany(
            "UPDATE files SET hash_size = ?, hash_mtime = ?, full_hash = ? WHERE path = ?",
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

    def duplicate_size_candidates(self, *, min_reclaim_bytes: int) -> Iterator[FileRecord]:
        """Streams every non-directory, non-empty, non-cloud-placeholder file whose `size`
        collides with at least one other such file *and* whose bucket clears the materiality
        floor — the SQL-pushed equivalent of the old in-memory `_size_buckets()` prefilter in
        `dedup.py`. A unique-size file is never selected by this query, let alone loaded into a
        Python `FileRecord`.

        `min_reclaim_bytes` is the materiality gate (2026-07-17 real-disk finding): a bucket's
        *theoretical* best-case reclaim is `(member_count - 1) * size` (every non-kept member
        turning out to be an exact duplicate) — below `min_reclaim_bytes`, the bucket is
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
        sql = """
            SELECT * FROM files
            WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
            AND size IN (
                SELECT size FROM files
                WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
                GROUP BY size HAVING COUNT(*) >= 2 AND (COUNT(*) - 1) * size >= ?
            )
            ORDER BY size
        """
        for row in self._conn.execute(sql, (min_reclaim_bytes,)):
            yield _row_to_record(row)

    def duplicate_size_candidate_count(self, *, min_reclaim_bytes: int) -> int:
        """A cheap `COUNT(*)` over the same filter `duplicate_size_candidates()` streams —
        logged once up front so a heartbeat can report "N of M processed" instead of just a
        running count with no sense of how much work remains."""
        sql = """
            SELECT COUNT(*) AS total FROM files
            WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
            AND size IN (
                SELECT size FROM files
                WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
                GROUP BY size HAVING COUNT(*) >= 2 AND (COUNT(*) - 1) * size >= ?
            )
        """
        row = self._conn.execute(sql, (min_reclaim_bytes,)).fetchone()
        return int(row["total"])

    def immaterial_duplicate_bucket_stats(self, *, min_reclaim_bytes: int) -> tuple[int, int]:
        """Returns `(bucket_count, theoretical_bytes)` for size buckets that collide (>= 2
        members) but were excluded from `duplicate_size_candidates()` for falling below
        `min_reclaim_bytes` — surfaced so the report can show what was skipped and why, rather
        than the exclusion being silent. `theoretical_bytes` is a labeled upper bound (every
        member turning out to be an exact duplicate), never a claim about real measured
        reclaim — this tool never fabricates confidence it hasn't earned by actually hashing.
        """
        sql = """
            SELECT COUNT(*) AS bucket_count, COALESCE(SUM((c - 1) * size), 0) AS theoretical_bytes
            FROM (
                SELECT size, COUNT(*) AS c FROM files
                WHERE is_dir = 0 AND size > 0 AND is_cloud_placeholder = 0
                GROUP BY size
                HAVING COUNT(*) >= 2 AND (COUNT(*) - 1) * size < ?
            )
        """
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
            clauses.append("is_cloud_placeholder = 0")
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
