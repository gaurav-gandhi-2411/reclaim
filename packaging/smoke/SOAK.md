# Frozen `reclaim serve` soak test

`soak_serve.py` runs `reclaim serve` for hours under a repeating scan + API workload, samples the
server process tree every minute, and gives a leak verdict. It exists because Windows Error
Reporting filed two `RADAR_PRE_LEAK_64` (memory-leak) reports against reclaim.exe (2026-07-25,
2026-08-26) with no recorded cause (see `docs/crash-inventory-2026-10-01.md`).

Stdlib only (ctypes / urllib / csv / json / argparse / subprocess): the system Python or the venv
Python both work. Windows only (samples via kernel32/psapi).

## Run against the frozen build

```powershell
python packaging\smoke\soak_serve.py --exe <path to reclaim.exe> --duration-minutes 120
```

Output goes to `%TEMP%\reclaim_soak\<timestamp>\` (override with `--out-dir`): `soak_report.md`
(verdict + numbers), `memory_curve.svg` (private MB and handle count), `soak_samples.csv` (raw
samples), `soak_analysis.json`, server stdout/stderr logs.

Exit code: 0 PASS, 1 FAIL, 2 INCONCLUSIVE, 3 harness error.

## What it does

- Launches `reclaim serve` on a free loopback port with `--db/--vault-dir/--manifest/--mode-log/
  --first-run-state/--log-path` all inside the output dir: the installed app's index and vault are
  never touched.
- Scans a generated, deterministic fixture (20,000 files of 64 B..4 KB, 200 dirs, ~10% duplicate
  content, seed 42) by default; `--scan-root C:\Users\gaura\ml-projects` scans a real tree
  instead. The scan is `POST /api/scan` (an outside-home root gets the single-use confirm token
  automatically).
- Every `--cycle-minutes` (10): scan, wait for completion, then `--api-rounds` (5) rounds of GET
  `/api/summary`, `/api/treemap`, `/api/candidates`, `/api/clean/one-click-summary`,
  `/api/settings/notifications`, `/api/diagnostics`; then idle until the next cycle. With
  `--include-regenerable-preview`, also `POST /api/clean/regenerable {"apply": false}` (skipped
  on 404, i.e. on builds without that tier).
- Every `--sample-seconds` (60): sums PrivateUsage / WorkingSet / PagefileUsage /
  handle count / thread count over reclaim.exe and all its children, one CSV row each.
- Cleanup: the whole process tree is terminated and confirmed gone on normal end, Ctrl-C, or error.

Attach to an already-running server instead of launching: `--pid <pid> --url http://127.0.0.1:8421`
(not terminated at the end; its data dir is whatever you started it with).

## Verdict rules and why these thresholds

Only post-scan idle samples after the warm-up are analysed, so the within-cycle sawtooth of a scan
(memory rises during the scan, is returned afterwards) is not read as growth.

| rule | default | why |
|---|---|---|
| warm-up excluded | 15 min | allocator, SQLite page cache and import-time growth settle |
| private-bytes slope (OLS) | FAIL if > 5 MB/h | over a 2 h run that is ~10 MB, above the sampling noise of a stable process; a real per-scan leak is far larger |
| cycle floors | FAIL if min private rises > 0.5 MB for >= 5 consecutive cycles | catches a slow creep under the slope limit; the 0.5 MB tolerance stops noise producing a false run |
| handle slope | FAIL if > 25 /h | a per-request or per-scan handle leak is hundreds per hour |
| thread slope | FAIL if > 5 /h | thread pools are bounded, steady growth is a leak |
| minimum data | >= 5 points over >= 30 min after warm-up | below that the verdict is INCONCLUSIVE, never PASS |

The 95% bootstrap CI of the slope (seeded, 1000 resamples) is reported but the verdict uses the
point estimate. All thresholds are command-line flags (`--slope-threshold-mb-per-hour` etc.).
These defaults are chosen by reasoning, not calibrated against a known-leaking and a known-clean
frozen build; treat the first real run as calibration. PASS means "no growth detected at this
sensitivity, window and workload", not proof of absence.

## Tests

`tests/test_soak_analysis.py` covers the verdict logic (flat+noise PASS, +50 MB/h FAIL, sawtooth
PASS, warm-up exclusion, handle growth, thin data INCONCLUSIVE).
