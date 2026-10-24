# ADR-0045: keep `idx_files_path_lower` -- it is not a duplicate of the primary key

Status: accepted (2026-10-24)

## Context

ADR-0042's "Plan only" section said `idx_files_path_lower` (2.80 GB, 25% of a 12.8M-row index) "duplicates the `path` primary-key index" and should be replaced by one NOCASE index. The October autonomous run queued this as D2: a migration with measured savings.

Measured on a scratch copy of the owner's real index (`scripts/scratch_index.py`). It had 2,039,021 rows after ADR-0042's opaque collapse and a file size of 1,475,534,848 bytes. The command: `python scripts/adr0045/measure.py sizes {DB}`.

| Object | Bytes | Share |
|---|---|---|
| `files` table | 819,220,480 | 55.5% |
| `idx_files_path_lower` | 240,103,424 | 16.3% |
| `sqlite_autoindex_files_1` (primary key `path`) | 240,099,328 | 16.3% |
| the other five indexes | 176,021,504 | 11.9% |

The two path indexes are the same size, but they are not the same key:
- `path_lower` is Python's `str.lower()` of the path, which folds Unicode (ADR-0042 addendum 4).
- SQLite's `NOCASE` folds ASCII only, and the primary key is `BINARY`.

A query one index serves cannot in general be served by the other with the same results.

## Decision

Keep the index, and do no migration. Each candidate replacement was measured, or rejected for a stated reason. Timings are the best of 3 within a run, from two separate runs on the same copy.

1. **Drop the index and keep the column** (`measure.py like-cost`).
   - All 31 default detector patterns use the index (`SEARCH files USING INDEX idx_files_path_lower`).
   - With it: 0.22-0.28 s in total. Without it (`NOT INDEXED`): 31.6-37.9 s.
   - The row sets were identical for 31/31 patterns.
   - `drop_opaque_rows`' equality lookup goes from under 0.1 ms to 1.0-1.2 s per path.
   - Rejected: about 30-40 s added to every candidate generation, to save 240 MB.
2. **`files` as a `WITHOUT ROWID` table keyed on `path`** (`measure.py without-rowid`). This removes the autoindex and leaves semantics unchanged.
   - With the same six secondary indexes, the total is 2,804,264,960 bytes vs 1,475,444,736 (1.90x larger). Every secondary index stores the ~100-byte path instead of an 8-byte rowid.
   - A full-scan `SUM(size)` is 2.1-3.3x slower.
   - It would also need the rowid-based physical-size grouping rewritten.
   - Rejected.
3. **Make `path_lower` the primary key, or replace both with `path COLLATE NOCASE`.** Not measured; rejected on semantics.
   - A unique case-insensitive key merges two paths that differ only in case. Such paths exist in case-sensitive directories (WSL, `fsutil file setCaseSensitiveInfo`). An upsert would then silently overwrite one file's row, and that file would vanish from the inventory.
   - `NOCASE` also stops matching non-ASCII case variants that the current code matches.
4. **A generated column `lower(path)`.** Same ASCII-only folding as 3; rejected.

## Consequences

- The index stays at about 16% of the file. On the owner's machine the index lives on D: (ADR-0041).
- ADR-0042's "duplicates" sentence is wrong. This ADR supersedes it, and the size it quoted predates the opaque collapse.
- Revisit if a measured index again shows this index above 25% of a real index, or if SQLite gains a Unicode case-folding collation usable without a loadable extension.

## Alternatives

Listed under Decision. The measurements are reproducible with `scripts/adr0045/measure.py`. It runs only on a `scratch_index.py` copy, and its writing mode refuses any other path.
