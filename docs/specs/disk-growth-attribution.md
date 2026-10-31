# Spec: disk growth attribution ("where did my disk space go?")

**Target user:** a Windows developer whose drive fills with tool caches, AI models, agent scratch and build output faster than they can track.
**Pain point:** when the disk fills, finding *what grew* takes hours of manual folder sizing. The owner lost most time this autumn this way: another session's scratch at 81.5 GiB, a 58.5 GiB overnight drop, the pagefile moving between drives, uv cache growth, an HF download.
**Success metric:** on replayed real data, a growth event of ≥ 5 GiB is detected and attributed to the right folder (recall ≥ 0.8) with ≤ 1 false alarm per week, against a plain threshold baseline. Each snapshot costs ≤ 2 min of IO and the store stays ≤ 5 MB a year.
**Who pays:** Reclaim's users (free desktop product); it is also the explanation that makes the existing 80% alert actionable.

Source: GG's 31 Oct round, §3. Read-only by design: nothing here deletes, moves or changes a file outside Reclaim's own data dir.

## 1. Behaviour

1. **Snapshot** (`reclaim growth snapshot`, also run by the existing 4-hourly `check-disk-space` task when the last snapshot is ≥ 3 h old). It records:
   - per fixed volume: total and free bytes;
   - system files at each volume root (`pagefile.sys`, `hiberfil.sys`, `swapfile.sys`), from the root directory listing (no file open);
   - known caches (uv, pip, npm, Hugging Face hub/datasets, Ollama models, torch, conda pkgs, Docker/WSL disk images, `%TEMP%`), the same paths the regenerable detectors already know;
   - top-level folders: the children of the user profile, of the profile's `ml-projects`-style project roots (one level), and of each volume root;
   - agent scratch per session: `%TEMP%\claude\<project>\<session>`, labelled with project and session;
   - opaque roots already in the index (`node_modules`, `.venv`...): their stored aggregate, no IO, stamped with the index scan time.
2. **Bounded IO:** one shared budget per snapshot, by default 120 s wall and 3M listed entries, using the ID-less fast directory listing (sizes come with the listing; no per-file stat).
   - Roots are visited stalest-first, so every root is refreshed over successive snapshots.
   - A root not finished within the budget is stored as `partial` (a lower bound) and never used as a growth endpoint.
   - The walk never follows a reparse point.
3. **Exclusions:** a path matching the configured exclusions (`[exclusions] project_names` and the protected list) is never walked and never named. Its share appears as one line, "Excluded projects", taken from the last index scan, so no new IO happens inside them.
4. **"What changed":**
   - `reclaim growth report --window 1d|7d` and `GET /api/growth?window=1d|7d` rank the keys by growth over the window. Growth = latest complete value − the latest complete value at or before the window start.
   - Each row carries its attribution (folder, plus project/session where known) and a link to the matching review screen: the category, or candidates filtered by path.
   - The volume's own change is shown with the sum of attributed changes and the remainder as "not attributed".
   - Read-only: the view links to review screens and never offers a delete itself.
5. **Anomaly alert:**
   - For each key, the growth rates (bytes/hour) between consecutive complete samples form a series. A window is anomalous when both hold:
     - its growth is ≥ `abs_floor` (default 5 GiB) and ≥ `share_floor` (default 2%) of the volume;
     - its rate's robust z-score over the key's past 14 days is ≥ 4, with z = (rate − median) / (1.4826·MAD + ε), and ε = 0.1 GiB/day keeps a flat series from alarming on noise.
   - Volume-level check: used space grew ≥ `abs_floor` within ≤ 3 days.
   - The toast names the biggest contributing keys, e.g. "C: grew 20 GB in 3 days — 18 GB from %TEMP%\claude\gg-portfolio". Its debounce state is separate from the 80% toast's.
   - When the 80% toast fires, its body gains the same explanation line (the top contributors over the last 3 days), so it says why the disk is full.
6. **Store:** `data/growth.sqlite3`, with tables `keys(id, path, kind, label, project, session)`, `snapshots(id, taken_at, budget_s, complete)` and `sizes(snapshot_id, key_id, bytes, partial)`. Snapshots older than 400 days are dropped. At ~250 keys and 6 snapshots a day this is about 550k rows a year (BELIEVED ≤ 15 MB; measured in the eval).

## 2. Candidates considered

| Option | Why not chosen |
|---|---|
| Diff successive full index scans | A full scan takes 7-80 min and happens rarely; it can't explain an overnight drop |
| NTFS USN change journal | Volume handle needs admin; Reclaim runs unelevated |
| Per-process write counters (the old sampler) | Writes ≠ growth (overwrites, temp files); no folder attribution |
| **Budgeted, stalest-first folder sizing + volume residual (chosen)** | Simple, read-only, bounded cost; honest "partial" and "not attributed" lines instead of guesses |

## 3. Eval (`evals/growth/`)

- **Data** (this engagement, real):
  - `D:\reclaim-build\c_attrib{6,7,8}.csv`: three 30-40 min windows on 13 and 15 Oct, with per-path sizes every ~60 s and per-process writers;
  - `c_census.csv` / `c_census_20261012.csv`: two per-folder snapshots, 9 and 12 Oct;
  - `watchdog*.csv`: C:/D: free during builds, 8-24 Oct;
  - `jobs\*\runner.log`: C:/D: free every 15 s, 30 Oct - 1 Nov.
  - The richer 9-10 Oct sampler (`%TEMP%\reclaim_soak\2026-10-09-attrib\sizes.csv`, which held the gg-portfolio +43 GiB event) no longer exists.
- **Ground truth:** only events already attributed in the run logs and RESUME history, each cited by file and line, in `evals/fixtures/growth_events.json`. No manual labelling.
- **Metrics:** event-detection precision/recall, attribution accuracy (top-1 key = documented folder), and false alarms per week of data, each for the detector and for a baseline that alerts on any key whose window growth ≥ 5 GiB.
- **Honesty rule:** the report states how many hours of data each source covers. If there are fewer than about 4 weeks of continuous snapshots, the per-week figures are labelled as not meaningful, and the report says what is needed: ≥ 4 weeks of 4-hourly snapshots on this machine, with the documented events inside them.
