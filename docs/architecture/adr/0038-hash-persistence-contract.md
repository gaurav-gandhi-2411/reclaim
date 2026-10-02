# ADR-0038: Dedup hash cache persistence contract (and why the installed index had zero hashes)

Status: Accepted (2026-10-02). Code change: none. Tests only.

## Context

A copy of the installed index (`...\Programs\Reclaim\data\reclaim_index.sqlite3`, 4,893,581,312 B,
5,862,980 rows, 5,251,953 files, no `-wal` at copy time) had
`SELECT COUNT(*) FROM files WHERE hash_size IS NOT NULL` = 0. Suspected cause: the dedup warm-up
(`service._cached_all_candidates` -> `dedup.find_duplicate_clusters`) losing its hashes.

## Investigation (hypothesis -> verdict)

- H1 warm-up never reaches a persist point: REFUTED. Frozen-build code (33ce814) run against the
  copy persisted 3,500 rows ~100 s in, 28,500 rows after ~220 s (batches of 500). Main flushes
  per hashed window (2,048 files).
- H2 a scan upsert wipes hash columns: REFUTED. `_COLUMNS`/the `ON CONFLICT DO UPDATE` set exclude
  the four hash columns (identical on 33ce814 and main). Fixture experiment on both: 30 hashed
  rows before and after an incremental rescan, a full (`incremental=False`) rescan, and a rescan
  with one changed file; the changed file keeps a row whose `(hash_size, hash_mtime)` no longer
  matches, so `cached_*_hash` refuses it.
- H3 uncommitted/lost writes: REFUTED. `store_*` commit per batch; a `Stop-Process -Force` of the
  33ce814 run left 28,500 rows readable from the WAL afterwards.
- H4 missing columns / migration: REFUTED. `PRAGMA table_info(files)` on the copy lists all four.
- H5 warm-up crashed in the frozen app: NOT SUPPORTED, not provable. The install-dir log has zero
  `dedup.*` and zero `api.candidates_warm_failed` lines (nor any real-disk `scan.*` lines), so it
  records neither a start nor a failure.
- H6 explicit reset: no code path deletes the index (only `DELETE FROM files` is path-scoped
  pruning). Every row's `last_scanned` lies in one 48-minute window on 2026-09-23 (UTC
  16:48-17:37), i.e. the index is the product of a single fresh scan. This is consistent with (BELIEVED,
  not proven) the documented install-verification procedure ("fresh install, two scans",
  RESUME.md), which scans from the CLI and never calls `POST /api/candidates/warm`.

## Decision

Root cause: dedup was never run against that index. Zero hashes is the correct state of a scanned,
never-deduplicated index, not a persistence defect. No production change. The contract is pinned by
`tests/test_hash_persistence.py`: rescans keep hashes; a hard-killed run keeps every flushed
window; a restart reuses them instead of recomputing.

## Consequences

- A user who scans but never opens the dashboard has no hash cache; first warm-up pays the whole
  hashing cost. Pre-hashing as part of scan would change scan latency and is not done here.
- A kill loses at most the in-flight window (<= ~2,048 files plus pending full-hash writes).

## Alternatives

- Persist per file / smaller batches: more commits for no measured benefit.
- Hash during scan: rejected for scan-time cost; separate decision.
