from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from reclaim.models import FileRecord

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
    full_hash TEXT
);
"""
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
        dev=row["dev"],
        ino=row["ino"],
    )


def _record_to_row(record: FileRecord, scanned_at: float) -> tuple[object, ...]:
    return (
        record.path.as_posix(),
        record.size_bytes,
        record.mtime,
        record.ctime,
        record.ext,
        record.attributes,
        record.dev,
        record.ino,
        int(record.is_dir),
        int(record.is_cloud_placeholder),
        int(record.is_reparse_point),
        record.git_repo_root.as_posix() if record.git_repo_root is not None else None,
        int(record.git_repo_clean),
        scanned_at,
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
        for statement in _INDEXES:
            self._conn.execute(statement)
        self._conn.commit()

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
            prefix = _escape_like_prefix(root.as_posix().rstrip("/"))
            cursor = self._conn.execute(
                "SELECT path, size, mtime FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                (prefix, f"{prefix}/%"),
            )
        return {row["path"]: StoredStat(size=row["size"], mtime=row["mtime"]) for row in cursor}

    def load_hash_cache(self, root: Path | None = None) -> dict[str, HashCacheEntry]:
        """Loads path -> cached hash entry for every row with a computed hash (optionally
        scoped under `root`), mirroring `load_stat_cache`'s one-query-not-per-file shape."""
        base = "SELECT path, hash_size, hash_mtime, partial_hash, full_hash FROM files"
        if root is None:
            cursor = self._conn.execute(f"{base} WHERE hash_size IS NOT NULL")
        else:
            prefix = _escape_like_prefix(root.as_posix().rstrip("/"))
            cursor = self._conn.execute(
                f"{base} WHERE hash_size IS NOT NULL AND (path = ? OR path LIKE ? ESCAPE '\\')",
                (prefix, f"{prefix}/%"),
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

    def subtree_size_bytes(self, root: Path) -> int:
        """Sum of `size` for every non-directory row at or under `root` — the aggregate size a
        directory-level candidate (e.g. a `node_modules` dir) represents.

        Logical sum (matches `logical_size_bytes` semantics), not hardlink-deduped: package/
        dependency-cache trees are vanishingly unlikely to contain internal hardlinks, so the
        simpler prefix-sum SQL query is preferred here over a second physical-size code path.
        """
        prefix = _escape_like_prefix(root.as_posix().rstrip("/"))
        cursor = self._conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS total FROM files "
            "WHERE (path = ? OR path LIKE ? ESCAPE '\\') AND is_dir = 0",
            (prefix, f"{prefix}/%"),
        )
        row = cursor.fetchone()
        return int(row["total"])

    def full_inventory(self, under: Path | None = None) -> list[FileRecord]:
        """Everything the scanner has seen, including cloud placeholders — for the treemap
        and total-usage display, which must reflect real disk (and cloud-footprint) usage."""
        return self._query_inventory(under, candidates_only=False)

    def candidate_inventory(self, under: Path | None = None) -> list[FileRecord]:
        """Everything except cloud placeholders — the only inventory Stage 3's candidate
        generation should ever read from."""
        return self._query_inventory(under, candidates_only=True)

    def _query_inventory(self, under: Path | None, *, candidates_only: bool) -> list[FileRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if under is not None:
            prefix = _escape_like_prefix(under.as_posix().rstrip("/"))
            clauses.append("(path = ? OR path LIKE ? ESCAPE '\\')")
            params.extend([prefix, f"{prefix}/%"])
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
