# ADR-0042: dependency trees are opaque in the scan index (one aggregate row per root)

Status: proposed (2026-10-17). Implementation lands as its own PR(s); this ADR is the contract.

## Context

A whole-profile scan of `C:\Users\gaura` (2026-10-15, installed build 7) produced 12,795,878 rows and an
11.0 GB index (13.8 GB after the dedup warm-up persisted hashes), 948 bytes per row. Measured on that
index (read-only, `D:\reclaim-build\index_kinds.json`): rows that sit BELOW a directory named

| ancestor kind | rows | share |
|---|---|---|
| `node_modules` | 6,928,499 | 54.1% |
| `.venv` | 2,132,735 | 16.7% |
| `site-packages` (not inside a `.venv`) | 1,037,462 | 8.1% |
| `.git` | 167,318 | 1.3% |
| `__pycache__` | 58,828 | 0.5% |
| `venv` | 3,881 | 0.03% |

No detector or UI ever needs the individual files under these trees: the dev-artifact detectors act on
the directory, and the only per-file consumers are size/age aggregates, the apply-time re-verification
and the hardlink guard. The cost of keeping them is the index size, the scan write volume and the
warm-up/summary queries over millions of rows.

## Decision

1. **Opaque roots.** While scanning, a directory whose name (case-insensitive) is in
   `OPAQUE_DIR_NAMES = {node_modules, __pycache__, .git, .venv, venv, site-packages}` is recorded as ONE
   row and its contents are walked only to compute aggregates, never recorded. The kinds are one
   constant so a kind can be dropped without touching the machinery. Reparse points are never followed
   (unchanged rule) and are counted as entries, not descended.
2. **Aggregates.** Three new columns on `files` (additive migration in the existing
   `ALTER TABLE ... ADD COLUMN` pattern, default 0/NULL so every old row is "not opaque"):
   `opaque INTEGER NOT NULL DEFAULT 0`, `agg_files INTEGER NOT NULL DEFAULT 0` (non-directory entries
   below, reparse entries included), `agg_bytes INTEGER NOT NULL DEFAULT 0` (logical sum of file sizes),
   `agg_newest_mtime REAL` (newest mtime of any entry below), plus `agg_physical_bytes INTEGER NOT NULL
   DEFAULT 0` (sum of sizes counting each file id once inside the tree). The root's own `size` stays 0
   and `is_dir` stays 1, so every existing `is_dir = 0` sum is unchanged by construction (the audit's
   top-3 "silent under-count" class cannot occur by accident; consumers that should see the aggregate
   are changed explicitly, below).
3. **Re-scan sheds the old rows.** The incremental scan's "not seen this scan" prune already deletes
   rows that are no longer visited; the opaque root row itself is always written (never marked
   unchanged on a size/mtime compare, because a directory's mtime does not move when a deep file
   changes), so a rescan converts an existing index in place.
4. **Consumers (each with a test).**
   * `subtree_size_bytes` = file sizes + `agg_bytes` of opaque roots in range; `subtree_newest_mtime` =
     max over rows and `agg_newest_mtime`; `subtree_entry_count` = rows + `agg_files` (so the
     apply-time entry-count guard that converts a huge delete to the vault still trips).
   * `physical_size_bytes_total` / reconciliation: add `SUM(agg_physical_bytes)` of opaque roots as a
     separate additive term. Documented loss: a hardlink between an opaque tree and a file outside it is
     no longer de-duplicated across the boundary (old behaviour de-duplicated by `(dev, ino)` across all
     rows); the measured delta on the real index is reported in the PR.
   * `newest_files_under` (ADR-0035 recheck): for an opaque root the recheck re-walks the live tree
     (bounded to the existing recheck budget) and compares its newest mtime with `agg_newest_mtime`;
     anything newer, or an unreadable tree, counts as "changed since scan" (fail closed).
   * `executor._direct_delete_directory_mismatch` (M1): for an opaque candidate the "present now but
     never recorded" rule is replaced by an aggregate comparison: a live recount of (files, bytes,
     newest mtime) must equal the recorded aggregates, else refuse. The per-file hardlink loop
     (`check_hardlink_shared_active_install` on every live file with `nlink > 1`) still runs over the
     LIVE tree, and the top-level `(dev, ino)` identity check is unchanged.
   * `_reclaimable_bytes_for_candidate` (package/model caches only): files below an opaque root are not
     rows, so the hardlink-aware estimate can only under-estimate (safe direction); asserted by a test.
   * Dedup, large-file and hash-cache paths see no rows below an opaque root: those files are excluded
     from duplicate detection by construction (they already were excluded by safety rules for
     `node_modules`/environments).
5. **Venv family is conditional.** `.venv`, `venv`, `site-packages` are collapsed ONLY because the
   hardlink-into-an-active-install guard does not read index rows: `preflight._enumerate_hardlink_siblings`
   calls Win32 `FindFirstFileNameW`/`FindNextFileNameW` on the live path and `dedup._environment_root`
   detects environments from filesystem markers (`pyvenv.cfg`, `conda-meta`, interpreter, `Lib\site-packages`).
   M1's per-file loop enumerates `_live_subtree_records` (a live `scandir`), not the index. A teeth test
   must show an opaque venv whose file is hardlinked into a different environment is still refused by the
   direct-delete re-verification. If that test cannot be made to pass, the venv kinds are removed from
   `OPAQUE_DIR_NAMES` and the rest ships.
6. **Agent scratch (separate PR).** Session directories directly under `%TEMP%\claude\<project>\` become
   opaque too (one row per session with the same aggregates) because the agent-artifacts spec
   (`docs/specs/agent-artifacts.md`) only needs size and newest-content mtime per session. Measured gain
   beyond rule 1 is small (about 0.23M rows) because 3.30M of the 3.65M scratch rows are already under a
   `node_modules`.

## Consequences

* Expected index on the owner's profile: the kinds above are 10.33M of 12.80M rows (80.7%), so about 2.5M
  rows and roughly 2.4 GB remain. Measured numbers replace this estimate in the PR.
* Per-file visibility under these trees is gone for the dashboard (treemap shows the root with the
  aggregate size) and for AI analysis. Intended.
* An existing index keeps its old child rows until the next scan prunes them.
* Older builds opening a migrated index ignore the new columns (additive), but would show the root with
  size 0 in sums; downgrade is not supported across this change.
* The apply-time safety net for an opaque direct-delete shifts from per-file recorded identity to
  aggregate equality plus the live hardlink guard. That is a deliberate, tested trade; vault and
  recycle-bin applies are unaffected (they never used the per-file comparison).

## Alternatives

* Skip the trees entirely (no row): loses the candidate itself and its size.
* Store aggregates in `size` with `is_dir = 0`: every file-oriented query would treat the root as a file.
* Collapse only `%TEMP%\claude`: removes 28.5% of rows but misses the same trees elsewhere.
* Schema change to drop the redundant `idx_files_path_lower` index (2.8 GB): separate plan, below.

## Plan only: duplicate case-insensitive path index

`idx_files_path_lower` (2.80 GB, 25% of the index) duplicates the `path` primary-key index
(2.79 GB). Replace it with `path COLLATE NOCASE` lookups served by one NOCASE-collated index (or make
the PK itself NOCASE). It is a migration of every query that uses `path_lower`, so it needs its own ADR
and a query-plan review (`tests/test_query_plan_coverage.py` pins the plans). Not part of this change.
