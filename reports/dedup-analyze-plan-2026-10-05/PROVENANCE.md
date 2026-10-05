# Provenance: dedup candidate queries slowed after a full ANALYZE (2026-10-05)

Input: a byte copy of the installed index (`%LOCALAPPDATA%\Programs\Reclaim\data\reclaim_index.sqlite3`,
4,893,581,312 B, 5,862,980 `files` rows, last scanned 2026-09-23). The copy was deleted afterwards.

| File | What it is |
|---|---|
| `measure-before-after.log` | **The headline numbers.** `ScanIndex.duplicate_size_candidate_count`, `immaterial_duplicate_bucket_stats`, `duplicate_size_candidates` (floor 1 MiB), each run with stats then (after `DELETE FROM sqlite_stat1`) without stats, before = main `8bb8fc0`, after = this branch `c84b3fb`. Python 3.12 + SQLite 3.50.4 (the app's). Produced by `measure_dedup_queries.py.txt`. |
| `qprobe-plans-and-with-stats-timings.log` | `EXPLAIN QUERY PLAN` for each query without stats, then after `ANALYZE` (171.4 s here), and the pre-#131 `GROUP BY size` form timed with stats (1,162.9 s wall / 208.8 s CPU, cold cache). Python 3.45.3 SQLite (anaconda), so plans may differ from the app's engine; timings there are NOT comparable with `measure-before-after.log`. |
| `qprobe-variants-with-stats.log` | Variant sweep with stats: inner-pinned (`v1`), inner+outer-pinned (`v2`) vs current. Pinning the outer query adds nothing, so only the inner scan is pinned. |
| `qprobe.py.txt`, `measure_dedup_queries.py.txt` | The scripts, as `.txt` so ruff does not lint them. |

Caveats (VERIFIED unless marked): the machine was at ~95-100% CPU from other sessions for every
run (system CPU% is printed in the log), so wall seconds are inflated and noisy; CPU seconds are
the cleaner signal. Each condition was run once (the qprobe variants ran twice). Whether the
production 135 s seen by the owner on the real run is the same effect is BELIEVED (same query,
same index, same magnitude: 110-154 s here), not separately measured.
