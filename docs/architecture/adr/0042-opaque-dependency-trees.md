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

> **Superseded by ADR-0045 (2026-10-24):** measured, the index is not a duplicate. Its key is Unicode-folded and the primary key's is not. It is 240 MB (16%) after the opaque collapse, and dropping it makes the detectors' path patterns about 140x slower. It stays. The text below is kept as it was written.

`idx_files_path_lower` (2.80 GB, 25% of the index) duplicates the `path` primary-key index
(2.79 GB). Replace it with `path COLLATE NOCASE` lookups served by one NOCASE-collated index (or make
the PK itself NOCASE). It is a migration of every query that uses `path_lower`, so it needs its own ADR
and a query-plan review (`tests/test_query_plan_coverage.py` pins the plans). Not part of this change.

## Implementation notes (2026-10-17, decisions the contract left open)

These refine, and do not change, the decisions above.

* **Incompletely measured trees are never collapsed.** If the aggregate walk of an opaque-named
  directory meets an unreadable subdirectory, an entry it cannot stat, a cancellation, or (for the
  recheck) the entry budget, it returns "no result" and the scanner walks that directory the
  ordinary way: every unreadable path then lands in the existing skipped / `inaccessible_paths`
  accounting and the per-file rows keep their lower-bound meaning. An opaque row therefore always
  means "fully measured at scan time".
* **Always rewritten.** `load_stat_cache` omits opaque rows, so a stored opaque row can never
  compare as unchanged (also when a kind is later dropped from `OPAQUE_DIR_NAMES`: the stale
  aggregate row is overwritten by the ordinary directory row). The writer deletes any per-file rows
  below a root in the same flush that writes it (`ScanIndex.delete_descendants`), so a cancelled
  scan cannot leave child rows counted next to the aggregate.
* **Physical total.** Implemented as one table scan (`WHERE is_dir = 0 OR opaque = 1`, an opaque
  row contributes `agg_physical_bytes` as its own group) rather than a second `SUM`; the result is
  the additive term the decision describes and keeps the single-scan plan pin.
* **Live recounts use the scandir walk with a real per-file stat** (`aggregate_tree(use_listing=
  False)`), not the directory listing, so a write the directory entry has not caught up with
  (ADR-0035) is seen. M1 compares (files, bytes, newest mtime) for equality; it also judges an
  opaque tree nested inside a larger direct-delete candidate, whose files are exempt from the
  "present now, never recorded" rule.
* **Recheck budget.** The temp-child recheck re-walks an opaque tree up to 200,000 entries and
  treats anything larger, or unreadable, as "modified just now" (fail closed: the child is not
  proposed).
* **`_reclaimable_bytes_for_candidate`** returns `None` (consumers use the logical size, as for
  every category without a link-aware estimate) instead of `0` when a directory's only content is
  opaque; with indexed members present it is the under-estimate over those members.
* **Known weaker spot.** An aggregate cannot see a file swapped for a same-size one whose mtime is
  not newer than the tree's newest (e.g. a `robocopy /COPY:DAT` over an existing file). The per-file
  identity M1 used to compare is gone for opaque trees; the live hardlink loop is unchanged. A
  content-digest column would close this and is the first thing to add if it matters.
* **Scan root inside an opaque tree.** Scanning `...\node_modules` (or a path below it) indexes
  its contents as ordinary rows, so an opaque row for that tree left from an earlier scan would be
  counted next to them. `scan_tree` therefore deletes the opaque row of its root and of every
  ancestor up front (`ScanIndex.drop_opaque_rows`); the candidate reappears, as one row, when the
  enclosing root is rescanned. Until then sizes under-count and never over-count. Characterised in
  `tests/test_scanner_opaque.py`.

## Addendum 2 (2026-10-17, after independent review)

* **Long paths.** `check_hardlink_shared_active_install` and M1's per-file hardlink loop used a bare
  `Path.stat()` / bare string, which fail past MAX_PATH (`LongPathsEnabled=0`) and read as "not
  shared": the guard that justifies collapsing `.venv`/`venv`/`site-packages` failed open exactly on
  deep site-packages paths. Both now use the `\\?\` form (Path values stay unprefixed), and M1 now
  refuses when a live file is present but cannot be inspected (a vanished file is still skipped).
* **Sixth column.** Decision 2 listed three columns plus `agg_physical_bytes`; a review showed that
  renaming a direct child directory of an opaque root, or adding and removing an empty directory,
  changes no file count, size or file mtime (the child keeps its own mtime). `agg_newest_dir_mtime`
  (REAL, newest mtime of any directory in the tree, root included) closes that: it is compared by M1
  and folded into the ADR-0035 recheck. `agg_newest_mtime` stays "newest non-directory entry";
  `subtree_newest_mtime` takes the max of both. Directory mtimes come from a real `stat` of each
  directory (the entry a parent's listing carries lags), at scan time and live alike.
* **M1 compares five values:** files, logical bytes, physical bytes, newest file mtime, newest
  directory mtime. Each is pinned by a test that changes only that value.
* **Case-insensitive invalidation.** `drop_opaque_rows` matches `path_lower COLLATE NOCASE` (the
  existing `idx_files_path_lower`, plan pinned in `tests/test_query_plan_coverage.py`), because NTFS
  paths are case-insensitive while index paths keep the case they were walked with. It removes
  only rows with `opaque = 1`. Each call commits on its own, independently of the scan's batch
  flushes: a crash after the drop leaves the candidate missing until the next scan, never stale.
  Other index paths (child rows, prune, stat cache) keep their pre-existing case-sensitive keying;
  a scan rooted with a different case than a previous one can leave case-variant rows for the same
  file, which predates this change.
* **Fail-closed vanished-vs-unreadable table.** Only a definitive not-found (`FileNotFoundError`,
  `NotADirectoryError` on an `lstat` in the long form) counts as vanished. Anything else on a live
  file in an opaque tree (M1 hardlink loop), on an opaque root, or on a candidate's identity check
  (`check_identity_unchanged_since_scan`, now long-path aware) refuses. `os.path.lexists` was
  replaced because it reports False for every OSError. `aggregate_tree` also refuses a reparse-point
  root (it would measure the target); the scanner never collapses a junction named like an opaque
  kind (`should_recurse` is false for reparse points), pinned for both walk paths.
* **Candidate pools.** An opaque root is a directory row. Dedup, large-file and extension pools
  filter on `is_dir = 0`, so they never see it however large its size or aggregates
  (`test_directory_rows_are_excluded_from_the_file_candidate_pools`).

## Addendum 4 (third review pass, 2026-10-17)

* **Case folding is Python's, not SQLite's.** `path_lower` is `str.lower()` of the path on write and on
  query, so `drop_opaque_rows` folds like Python; SQLite's `COLLATE NOCASE` is ASCII-only and does no
  work for non-ASCII names. A non-ASCII directory differing only in case (`Ébène`/`ébène`) was verified
  to match. NTFS up-case pairs that Python's `lower()` folds differently were not exhaustively tested.
* **An actively written opaque tree refuses every direct-delete apply until it is rescanned.** The
  aggregate comparison is exact (no tolerance), so a tree that changes between scan and apply (for
  example a running `npm install`) is refused each time and clears after the next scan records the new
  aggregates. Fail closed, not a permanent deadlock; not measured against a real `npm install`.
* **A stat fault refuses the whole MCP delete batch.** `check_identity_unchanged_since_scan` now reports
  a candidate whose stat fails for any reason other than not-found as changed, and
  `api/service.py:stale_selected_candidates` (used by the MCP `delete` tool) then refuses the batch
  ("N selected file(s) changed on disk"). Triggers measured on CPython: winerror 5/21 (PermissionError),
  123/1920/1117 (OSError). A locked-for-write file and an ACL deny-read file do not trigger it (lstat
  succeeds). The refusal detail now says the item could not be inspected rather than that it was swapped.
