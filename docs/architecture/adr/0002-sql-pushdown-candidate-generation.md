# 0002. SQL-pushdown candidate generation

## Context

The first real-disk dry run against `C:\` (3.1M files, `data/real-disk-run/index.sqlite3`)
stalled a second time after ADR-independent fixes to the duplicate-hash pipeline (heartbeat,
incremental commits, per-file timeout guard — see the 2026-07-16 PLAN.md checkpoint). Killing
the stuck process and inspecting it (`tasklist`) showed it alive and burning CPU — 21+ minutes,
~4.9GB RSS — but with zero rows written to `partial_hash`/`full_hash`, meaning it hadn't even
reached the duplicate-hash stage yet.

Root cause: `detectors.py::generate_candidates()` and `dedup.py::generate_duplicate_candidates()`
each independently called `ScanIndex.candidate_inventory()` — a full `SELECT * FROM files WHERE
is_cloud_placeholder = 0`, materializing every one of 3.1M rows into a `FileRecord` dataclass
and building two whole-inventory dicts (`InventoryContext.by_path`/`children_by_dir`) before a
single rule detector ran. On a real disk-scale index this cost ~5GB of RAM and 20+ minutes of
pure Python object construction, run **twice** (once per caller), before any detector or the
duplicate-size prefilter got to discard the 99%+ of rows that were never going to be candidates
anyway.

## Decision

Every rule detector in `detectors.py`, and the duplicate-size prefilter in `dedup.py`, now
queries `ScanIndex` directly through a narrow, indexed method instead of iterating an
in-memory copy of the whole inventory:

- `ScanIndex` gained two new indexed columns: `name` (lowercased basename) and `path_lower`
  (lowercased posix path), plus indexes on `ext`, `size`, `name`, and `path_lower COLLATE
  NOCASE`. A pre-existing index (created before this ADR) is migrated in place —
  `ALTER TABLE ADD COLUMN` + a streamed (`fetchmany`, never `fetchall`) backfill — the first
  time it's opened; no `--full` rescan is required.
- New query methods, each a single indexed `SEARCH`, verified via `EXPLAIN QUERY PLAN` in
  `tests/test_index.py` (captured from the *actual* SQL each method issues via
  `sqlite3.Connection.set_trace_callback`, so the test can't drift from the implementation):
  `get_record` / `record_exists` (primary-key point lookups), `files_by_name` (dev-artifact
  directory names), `files_by_ext` (installer/crash-dump/archive extensions),
  `files_larger_than` (large-log size threshold), `files_matching_path_pattern`
  (package/browser/temp-root glob patterns, translated to SQL `LIKE`), and
  `duplicate_size_candidates` (the dedup prefilter: `GROUP BY size HAVING COUNT(*) >= 2`,
  joined back to return only the actual candidate rows).
- `ScanIndex.candidate_inventory()`/`full_inventory()` still exist (the FastAPI dashboard's
  treemap/category views still use them, always scoped by `under=<subdirectory>`) but are now
  documented as deprecated for whole-index use — no detector or dedup code may call either
  with `under=None` again.
- `InventoryContext`/`build_inventory_context()` are deleted outright: nothing calls them
  anymore, and keeping dead code that "used to be the fast path" invites a future regression
  back to it.
- Rule detection and duplicate-candidate generation still each run their own query (they read
  different projections — e.g. `duplicate_size_candidates()` needs `size`/`is_cloud_placeholder`
  filtering the rule detectors don't), but neither one loads the *whole* table anymore, which is
  what actually mattered: the shared cost this ADR removes was the full materialization, not
  the fact that there were two call sites.

## Honest limits — not everything reduces to a clean indexed `WHERE`

A few checks genuinely cannot be pushed into SQL without losing correctness or silently
changing behavior. These still run in Python, but only over the already-indexed-narrowed
candidate set each query returns — never over the whole table:

- **Archive-pair fuzzy matching** (`detect_archive_pairs`): `difflib.SequenceMatcher` ratio
  between an archive's stem and a sibling directory's name has no SQL equivalent. Narrowed via
  `files_by_ext` (archive extensions) first; the fuzzy comparison then runs only against that
  one archive's own siblings (`direct_children`, an indexed prefix query), never the whole
  inventory.
- **"Under a Downloads/log-named" substring checks** (`detect_old_installers`,
  `detect_large_logs`): a path-segment check and a "log" substring-in-filename check aren't
  single indexed predicates. Narrowed via `files_by_ext`/`files_larger_than` first (extension
  and size are indexed; on a real disk, size alone eliminates the overwhelming majority of rows
  for the large-logs case), then the substring/segment check runs only on that small remainder.
- **`ext` column stores `Path.suffix` (last component only)**: a `backup.tar.gz` file is stored
  with `ext='.gz'`, not `'.tar.gz'`. The archive-pair prefilter therefore includes `.gz` in its
  extension set — a superset that also matches non-tar `.gz` files — and relies on
  `_archive_stem()`'s exact `.tar.gz`-suffix check (unchanged from before this ADR) to reject
  those afterward. Covered by
  `test_bare_gz_file_without_tar_is_not_proposed`/`test_tar_gz_compound_suffix_is_stripped_before_matching`.
- **`files_matching_path_pattern` never escapes literal `%`/`_`**: SQLite disables its
  LIKE-to-index-range-scan optimization the moment an `ESCAPE` clause is present — confirmed
  empirically (identical query and index, only the `ESCAPE` clause differs, and the plan
  degrades from `SEARCH ... USING INDEX` to a full `SCAN`). None of this project's actual
  `config.categories.*.paths`/`cache_paths`/`temp_roots` defaults contain a literal `%`/`_`
  that would need escaping, and `fnmatch` itself has no escape mechanism for its own
  `*`/`?`/`[]` metacharacters either — this is a different instance of the same pre-existing
  class of limitation (a user-supplied custom pattern containing a literal `%`/`_`/`[...]`
  won't behave exactly like `fnmatch` would have), not a new regression against default
  behavior.

## Consequences

- `generate_candidates()`/`generate_duplicate_candidates()` keep their exact external
  signatures — the CLI (`cli.py`) and FastAPI service layer (`api/service.py`) needed zero
  changes and their existing tests pass unmodified.
- Peak Python memory during candidate generation is now bounded by the number of real
  candidates, not by total row count — proven by
  `evals/test_candidate_generation_perf.py`: a 500K-row synthetic index (all but 4 rows
  deliberately unique) produces a peak memory delta of ~1MB, not the several-hundred-MB a full
  materialization of 500K `FileRecord` objects would cost.
- `tests/test_detectors.py` was rewritten to seed a real `ScanIndex` (via `upsert_records`)
  instead of building an in-memory `InventoryContext` — every detector test now exercises the
  actual SQL-pushdown query path, not a stand-in.
- `evals/test_candidate_generation.py` (the existing scanner → index → detector →
  SafetyValidator golden-fixture test, written and passing against the old
  `InventoryContext`-based implementation) passes unmodified after this rewrite — the
  strongest available evidence of behavioral parity on real detector logic, since its
  assertions were fixed before this change existed and still hold after it.

## Alternatives considered

- **Keep `candidate_inventory()` but paginate it (fetch in batches, still Python-side
  filtering).** Rejected: still constructs a `FileRecord` for every row eventually, just spread
  over more, smaller lists — doesn't fix the actual cost (millions of dataclass/`Path`
  constructions), only smooths its memory profile.
- **Turn on `PRAGMA case_sensitive_like` connection-wide instead of a `COLLATE NOCASE` index.**
  Rejected: verified this also makes the LIKE-to-index-scan optimization fire, but it would
  silently change the case-sensitivity of every *other* existing `LIKE`-based query on the same
  connection (`direct_children`, `subtree_size_bytes`, `_query_inventory`, `load_stat_cache`),
  which currently rely on the default case-insensitive behavior for path-prefix matching. A
  per-column `COLLATE NOCASE` index achieves the same optimization for exactly the new
  `path_lower` queries without touching any existing query's semantics.
- **Full parity harness re-running the deleted `InventoryContext`-based implementation
  side-by-side.** Rejected as unnecessary ceremony: `evals/test_candidate_generation.py`
  already encodes the expected candidate set per category from real fixture data and was
  written against the old implementation; it passing unmodified after the rewrite is direct
  evidence of parity without needing to keep dead code around just to diff against it.
