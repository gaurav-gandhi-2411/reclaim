# Resume checkpoint — 2026-09-23 (post-reboot reconstruction: crash-harness track pushed clean, WAL leak found+fixed, rebuild in flight)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`. Always
`git fetch origin` + `gh pr list` before trusting any claim below, including this one (rule 118a).

## CHECKPOINT 2026-10-05 -- READ THIS FIRST (section 1 of the owner's resume prompt; supersedes the 10-02 section where they differ)

**Main** = `8bb8fc0` (#129 merged; #132 #116 #127 #131 merged 2026-10-02). `scripts/verify.py` on main and on
main+#129: 1508 / 1529 passed, 35 skipped, coverage 89.72 / 89.74 %. C: free fluctuates 68-80 GB with
pagefile/commit pressure from other sessions (pagefile.sys 15 GB, commit 33.9 GB on 31.2 GB RAM at 13:50).

**Section 1 PRs (each from main `8bb8fc0`; none merged -- merge order below):**
- **(a) `perf/dedup-analyze-plan`**: root cause of the post-ANALYZE dedup slowdown: with `sqlite_stat1` the
  planner moves the distinct-inode GROUP BY from a rowid scan to the non-covering `idx_files_size`, one random row
  lookup per entry. Fix = `FROM files NOT INDEXED` on the inner scan (#117's subtree-count gain is a different
  query, untouched). Real-index copy, SQLite 3.50.4, CPU-contended (95-100 %): with stats count/immaterial/stream
  153.6 / 110.1 / 126.5 s -> 36.0 / 15.6 / 43.7 s; no stats 21.6 / 12.2 / 30.6 -> 16.3 / 11.7 / 27.3 s; results
  identical. Provenance: `reports/dedup-analyze-plan-2026-10-05/`.
- **(b) #134 `feat/background-dedup-warmup`** (ADR-0040): auto warm-up after a full scan (home or an ancestor),
  Windows background mode on the worker and hash-pool threads, cancel endpoint, resume from per-window hashes.
- **(c) `chore/scratch-index-helper`**: `scripts/scratch_index.py` (copy the real index only with guaranteed cleanup)
  + CLAUDE.md 3a. The sweep of this workstream's own paths found exactly one index copy (mine, 4.56 GiB, deleted);
  the recurring "~14 GB drops" are BELIEVED to be pagefile/commit pressure, not copies (pagefile.sys is 15 GB).
- **(e) #133 `fix/autoclean-uv-lock-retry`** (ADR-0034 addendum): VERIFIED that any live `uv run` holds a shared
  lock on `%LOCALAPPDATA%\uv\cache\.lock` (exclusive `uv cache prune` blocks for its lifetime; Restart Manager names
  the holder). Fix = delayed LogonTrigger + state file (`data/autoclean_state.json`) so a sign-in run retries only the
  tools left pending. NOT verified: that a real sign-in obtains the lock; who held it during the 3,300 s failure
  (unrecoverable, BELIEVED other sessions' `uv run`). Existing registered tasks keep one trigger until the
  Settings toggle is switched off and on.

**Worktrees (d):** 36 -> 24 (before this round's new ones). Removed 12 clean, fully-pushed ones of this workstream
(branches kept). 22 pre-existing kept: each holds commits on no remote ref (mostly pre-squash/pre-rebase
shas of merged work, content NOT individually verified) and/or dirty files (LFS `.onnx`), plus
`rebase-mcp-q3` (another session, untouched) and the main checkout. Per-worktree table: see the section 1 report.

**Still to do (section 2, after the owner merges section 1):** rebuild, smoke tests + DLL check, 2 h soak, install +
exclusions in the installed `config.toml`, fresh full scan (+ confirm warm-up starts), real-browser 409 check, owner
reboots BEFORE Phase C (pagefile reset), Phase C one-click, weekly task Ready, B6 toast, how-to paragraph.

## CHECKPOINT 2026-10-02 (owner shutting down) -- superseded where it differs from the section above

**State at this checkpoint (VERIFIED by `git`/`gh` at the time of writing; re-check with `git fetch origin`
+ `gh pr list` before trusting it):** `origin/main` = `aba6043`. Nothing of this workstream is running:
no subagent, no build, no scan, no soak, no verify, no benchmark (checked: process list; the only
python/pytest processes belong to `fr-en-wt-eval`, another session's work on an EXCLUDED project, not touched).
Docker Desktop is running (started for the prune; quit it if you want the RAM). C: free 90.04 GB, pagefile 8,704 MB.

**Merged to main this round (all by the owner):** #112 Nuitka pin 4.2.2, #115 bytes_freed vs bytes_moved,
#113 regenerable tier (ADR-0034) + one-click, #114 docs, #117 review-queue perf + full ANALYZE at scan end,
#118 scan file IDs from the NTFS listing (ADR-0035), #120 soak harness, #119 pyjwt 2.15.1, #123/#124/#125
dedup (stale-hash fix, env-root memo, parallel hashing), #122 apply identity compares size+mtime (ADR-0036),
#121 path-scoped apply via the warm candidate cache + warm-status `stale`, #128 hash-persistence tests
(ADR-0038), #126 repo `CLAUDE.md` + exclusion record, #130 `reclaim index-prune` + unlistable-subtree prune fix.

**Open PRs (all rebased onto `aba6043`, all required checks green, CLEAN):**
| PR | Branch | Base | State | Notes |
|---|---|---|---|---|
| #116 | `feat/weekly-autoclean` | main | ready | weekly auto-clean task, `reclaim auto-clean`, Settings toggle, typed `Get-ScheduledTaskInfo` query, PT45M limit |
| #127 | `fix/warm-check-all-views` | main | ready | every cache reader checks warm status; cold/stale read = typed 409 `candidates_not_warm` (ADR-0037) |
| #129 | `feat/cleanup-exclusion-list` | `feat/weekly-autoclean` | ready, STACKED | `[exclusions] project_names` honoured by every delete path, `excluded_applied` reported (ADR-0039); after #116 merges: `git rebase --onto origin/main <old #116 tip>` then `gh pr edit 129 --base main`, re-run CI |
| #131 | `perf/dedup-inode-floor` | main | DRAFT, owner decision | see "Decisions" |
A scratch merge of #116 + #127 + #129 + #131 onto main merged without conflicts and the full `scripts/verify.py`
on the combined tree exited 0 (run before main moved to `aba6043`; re-verify after each merge: "green at
handoff" is not enough for a train). #116 and #127 are independent of each other; #129 depends on #116.

**Decisions waiting for the owner**
1. **#131 inode-level floor.** Premise did not hold: of 6,570 dropped buckets only 178 (482 names) are
   hardlink-only; 6,392 are 2+ distinct inodes whose real best-case reclaim is under 1 MiB (about 5.1 GB total)
   that extra hardlink names had inflated over the floor. Candidates 1,967,289 -> 914,970 rows, hashed inodes
   1,482,690 -> 713,252 (about 13 min saved, ESTIMATE). A = keep (recommended), B = exclude only the 178
   single-inode buckets. Also unexplained: after a full ANALYZE the prefilter query slows from ~15-20 s to ~135 s
   for BOTH old and new SQL (it runs 3x per dedup pass): investigate.
2. **Run `reclaim index-prune --apply --vacuum` on the real installed index?** About 10-11 min, dashboard must be
   closed (VACUUM needs an exclusive lock and ~1x index size free). Measured on a copy: 5,862,980 -> 3,773,256 rows,
   4.89 GB -> 2.56 GB; `--deep` removes 71,293 more. 35.6% of rows were dead only because the index is one
   un-rescanned scan from 2026-09-23.
3. **Hash during scan?** A user who scans but never opens the dashboard has no hash cache (the installed index's
   zero hashes were NOT a bug: dedup was simply never run on it; ADR-0038).
4. **Warm-up still needs the whole dedup pass** (measured: new pipeline ~48 min cumulative on the real index copy
   vs >2 h unfinished before; ESTIMATE ~1 h whole warm-up). Streaming/cancellable warm-up not built.

**Next steps, in order (nothing below has started)**
1. Owner merges #116, #127 (any order); I rebase+retarget #129, re-verify; owner merges #129 (and #131 if A).
2. REBUILD from main: `pwsh packaging/build_installer.ps1` (~25 min warm EXPECTED, BELIEVED: Nuitka 4.2.2 is the
   version of the current install so the ccache should be reused; my earlier "~5 h" was a stale figure; report the
   actual and confirm cache reuse). Then both smoke tests (`packaging/test_packaged_serve.ps1`,
   `test_packaged_safe_mode.ps1`) + `scripts/check_dist_dll_closure.py` (#108).
3. Frozen-exe soak (2 h, FIRST RUN IS CALIBRATION; thresholds 5 MB/h private, 25/h handles, 5/h threads, 15 min
   warm-up are reasoned, not calibrated): `python packaging\smoke\soak_serve.py --exe <reclaim.exe> --duration-minutes 120`.
   Closes the two undiagnosed WER `RADAR_PRE_LEAK_64` reports (docs/crash-inventory-2026-10-01.md).
4. Install on the owner's account; put `[exclusions] project_names = ["fr-en-transformer", "shipdoc-extract",
   "intent-router"]` in the INSTALLED `config.toml` (the product default is empty; the dashboard reads it at
   startup, the CLI/weekly task on each run). Phase C: one-click on the real drive (wall-clock, GB freed, measured
   free before/after, skips and why, and `excluded_applied: 0`), weekly task registered and Ready, fresh full scan
   (wall-clock, index size; run `index-prune` first if the owner approves).
5. B6: enable the 80% notification on the OWNER's account (not ReclaimSmokeTest), tell the owner when to watch the
   screen, trigger it (temporarily lower the threshold, restore 80), report `PeriodicNotificationCount` before/after
   next to the owner's yes/no. Never confirmed by a human yet.
6. One-paragraph "how to use it" for the owner.

**Still open / not done**
- Docker: the 55 anonymous volumes and 5 stopped containers and the build cache were pruned; `docker_data.vhdx`
  went 62.30 -> 16.09 GB after the owner's diskpart compaction (46.21 GB, inside the 42-48 GB estimate); another
  compaction would return ~2.1 GB. Ubuntu `ext4.vhdx` 26.37 GB (25 GB used; `/home/gaurav` 23 GB), nothing done.
- `uv cache prune` (26.7 GB cache): a background prune with a 3,300 s lock timeout FAILED to get the lock (other
  sessions held it the whole time) -- the 30-minute bounded wait in the regenerable tier may therefore also expire
  on a busy workstation; the weekly run reports `skipped_in_use` with "waited N s". Never `--force`.
- `fr-en-transformer` / `shipdoc-extract` / `intent-router`: HARD EXCLUSION stands (a session the owner starts on one
  of them may work on that project only; see global CLAUDE.md 55e). The `v0.2.2-colab` tag task was dropped; nothing
  was written there.
- 34 git worktrees exist (`git worktree list`): ~19 `.claude/worktrees/agent-*` and `reclaim-wt-*` from this workstream
  (all clean of unpushed work as far as known, NOT verified one by one) plus `rebase-mcp-q3` which is another
  session's. Next session: per worktree check `git status`/`git log origin/main..HEAD`, read the output, THEN remove
  (check-then-delete, never one command); several hold unstaged real `.onnx` files over LFS pointers. A stray 0-byte
  scratch file from a subagent was already removed from the main checkout.
- Unverified/limits to remember: the UI changes (Simple-mode one-click, Settings toggle, warm-check states) were
  only tested with jsdom, never in a real browser (before/after screenshots owed per rule 15c); the compiled-exe
  path of the weekly task and a real toast were never exercised; yarn was not installed so its lock behaviour is
  NOT MEASURED (pip: no lock, conda: per-repodata byte lock not taken by `clean`; ADR-0034).
- ADR numbers in use: 0034 regenerable tier, 0035 scan listing, 0036 apply identity, 0037 warm check (#127),
  0038 hash persistence, 0039 exclusion list (#129); #131 only appends to ADR-0002.

## UPDATE 2026-10-01 (evening) — PR hygiene, owner decisions 1a-1d, cleanup executed

**PR hygiene (VERIFIED via `gh pr view`):** `pip-audit` was red on EVERY open PR because eight
advisories were published against pyjwt 2.13.0 (transitive via mcp) after main's last green run
(2026-09-24). Not caused by any PR's diff. Fix = PR #119 (`uv.lock` pyjwt 2.13.0 -> 2.15.1; local
`pip-audit` clean, MCP tests pass). All other PRs go green only after #119 merges and they are
rebased onto main. #112 had wrongly been left non-draft; converted back to draft.
**Stack:** #116 is based on #113's branch; after #113 merges, rebase #116 `--onto main` and
`gh pr edit 116 --base main` (squash-merge leaves #113's old commits in #116's history).

**Owner decisions applied (branches pushed; PR bodies updated):** 1a uv waits <= 30 min on its own
lock (`UV_LOCK_TIMEOUT`), never `--force`, async job + status endpoint (#113); 1c task query via
`Get-ScheduledTaskInfo` typed JSON, `PT45M` limit (#116); 1d listing for the walk + live re-stat at
age/size/hash-cache decisions (#118); 1b ANALYZE/optimize after scan (branch `perf/analyze-after-scan`
in flight when this was written).

**Cleanup executed (owner-approved list; measured with `Win32_PageFileUsage` pagefile 4,608 MB at
every reading):** C: free 53.17 -> 85.04 GB. Deleted: 7 `C:\adk*` venvs 4.68 GB logical, `C:\tmp_keras_wt_venv`
2.77, `reclaim-emergency-quarantine-20260820-232958` 3.78, July quarantine batch 5.12 + real-disk-run
`index.sqlite3` 5.90 (manifests/logs cited by CASE_STUDY kept), HF xet cache 9.95, HF
`models--stabilityai--stable-diffusion-2-1` 5.16 (AetherArt code loads only
`sd2-community/stable-diffusion-2-1`), `C:\src\flutter` 3.22 (not on PATH, unreferenced; the
`sdks\flutter` one is referenced by mindmeld's local.properties). NOT deleted per the owner's rule:
8 verification clones (each has 9 modified `artifacts/*.txt` retrained outputs), `adk6725fix`
(dirty=3), `adk6725_repro`/`adkrel060-scratch` (tiny, no git), `triage-iq-wt-groq-model-fix`.
Stray 0-byte `scratch_patch.py` appeared in the main checkout at 19:25 (not mine; left).

## HARD EXCLUSION (permanent, owner-set 2026-10-02) -- read first

**Do not touch `fr-en-transformer`, `shipdoc-extract` or `intent-router`**: no deletions, no
cache/data/model removal, no git operations (not even fetch/status), no tags, no worktree changes.
They are the owner's active work. The `v0.2.2-colab` tag task is DROPPED. Every reclaim cleanup run
(one-click, weekly auto-clean, review apply) keeps them on its exclusion list, and its report must
state that none of their paths appear among applied candidates (the product-side list is
implemented in `feat/cleanup-exclusion-list`; the installed `config.toml` carries the list).
Other sessions must work in their own worktrees, never in reclaim's main checkout (see `CLAUDE.md`).
Earlier in this session (before the directive) read-only `git status/fetch/tag -l/ls-remote` and
`gh` queries were run against `fr-en-transformer`; nothing was written, pushed or tagged there.

## WORKING RULES LEARNED 2026-10-01 (read before touching this repo)

- **Never work in reclaim's MAIN checkout (`C:\Users\gaura\ml-projects\reclaim`).** Every session and
  every subagent uses its own `git worktree` (`git worktree add ..\reclaim-wt-<slug> -b <branch>
  origin/main`). A stray 0-byte `scratch_patch.py` appeared untracked in the main checkout at
  2026-10-01 19:25:18; provenance (VERIFIED from the subagent transcript): a subagent running from
  `.claude\worktrees\agent-*` executed `cat > ../../../scratch_patch.py`, whose `../../..` is the main
  checkout, and the `cat` blocked on stdin until the task was killed (the following `rm -f` never ran).
  It was provably empty and untracked, so it was deleted.
- **A deletion's check and the deletion never run in the same command; an empty check output is a failed
  check.** Incident: a duplicate HF SD 2.1 copy was deleted in the same command as a completeness check
  of the kept copy whose filter printed nothing; the kept copy was intact only by luck. Written into
  `C:\Users\gaura\.claude\agents\executor.md`.
- **pip / conda / yarn have no cache lock to wait on (measured).** pip 25.1: no lock, purge with one
  file held exits 2 with `PermissionError` (now `skipped_in_use`); conda 25.5.1: per-repodata byte lock
  not taken by `clean --tarballs --index-cache`; yarn not installed (NOT MEASURED). See ADR-0034.
- **Post-reboot state 2026-10-01 21:36 IST:** `hiberfil.sys` gone (C: free 104.17 GB, pagefile 4,608 MB),
  but `LastBootUpTime` is 09:58 today (uptime 11 h 38 m, no 6006/6005 events since) and
  `HypervisorPresent=False`, `wsl --status` still says WSL2 unsupported: the reboot after the admin
  steps has NOT been observed yet (the last hypervisor-init event is 2026-09-28 21:32).

## SAFE POINT 2026-10-01 — all agents finished, everything pushed; waiting on owner merges + admin window

Branches: #119 `fix/pyjwt-advisories` (ready, CLEAN), #112 Nuitka pin, #113 regenerable tier (+1a uv wait,
async job), #116 weekly auto-clean (+1c typed task query, based on #113), #115 bytes_freed/moved, #117
review-queue perf (+1b full ANALYZE at scan end, archive-pairs memo), #118 scan listing IDs (+1d live
re-stat at decision points), #114 docs, soak harness `test/frozen-serve-soak` (PR opened after this).
1b: full `ANALYZE files` fixes the un-hinted plans (52.1 s -> 0.476 s); `PRAGMA optimize` /
`analysis_limit` do NOT. Path-scoped apply 202.3 -> 132.3 s (remaining = whole detector suite per
request; needs the warm-candidate-cache behaviour change, NOT done, owner decision).
Next: owner merges #119 -> rebase all others on main -> re-run checks -> table; admin window (Docker/WSL
steps 1-3 + `powercfg /hibernate off`, reboot); then Docker prune; rebuild (~25 min warm BELIEVED);
soak 2 h; Phase C; B6.

## PRIORITY CHANGE 2026-10-01 — "daily driver" plan (Phases A/B/C), read this first

Supersedes the AC3-trip priority below. #110 and #111 are merged (`origin/main` = `3deb05d`).
Owner's brief: clean C: now (A), make Reclaim safe/fast/self-maintaining on the owner's account (B),
rebuild + install + run for real (C). Never self-merge; admin/UAC steps are routed with numbered steps.

**Phase A (VERIFIED unless marked):**
- `$R21C2JW` (Recycle Bin item, 899,224 files) deleted, 609 s. C: free 61.02 -> 69.91 GB over that
  window (+8.89 GB; includes Docker Desktop start, so not purely this delete).
- TEMP: 11.06 GB total, of which 8.88 GB is `%TEMP%\claude` (live sessions' scratchpads, left alone);
  eligible by #110's recursive-newest-mtime rule (>7 d, no `.git`/venv): ~0 GB. Crash dumps 0 GB.
  Browser caches: Chrome and Edge were running (skipped, Edge ~0.43 GB); Brave dir empty.
- uv: `uv cache prune` is blocked by other sessions' running `uv` processes (cache lock); a background
  prune with `UV_LOCK_TIMEOUT=3300` was queued; result UNKNOWN at this checkpoint (BELIEVED to block
  until those `uv run` processes exit). `--force` deliberately not used.
- Docker engine will not start: `wsl --status` says "WSL2 is not supported with your current machine
  configuration"; `HypervisorPresent=False` although firmware virtualization is enabled. Needs admin.
  `docker_data.vhdx` = 62.3 GB; prune/compaction therefore NOT done.
- C: free drifted 69.9 -> 55.8 GB during the session; pagefile allocation grew 4,608 -> 12,821 MB
  (+8.2 GB, `Win32_PageFileUsage`), the rest is concurrent sessions' writes (BELIEVED, unattributed).

**Phase B (in flight; branches pushed, PRs draft):** B8 = PR #112. B1/B2-targeted = branch
`feat/regenerable-safe-tier` (ADR-0034; `reclaim.regenerable`, `POST /api/clean/regenerable`, UI).
B5 = `feat/weekly-autoclean`. B7 = `fix/bytes-freed-vs-moved`. B4 = `perf/review-queue-dry-run`.
B2-full-scan levers = `perf/scan-listing-ids`. B3 inventory: `docs/` (see crash-inventory section in
the PR that carries it); 3 APPCRASH events (MSVCP140 14.29 vs 14.50 mismatch) fixed by #108,
2 RADAR_PRE_LEAK_64 events (2026-07-25, 2026-08-26) undiagnosed.
**Phase C** not started: needs B1-B8 merged by the owner, then rebuild from main (~5 h).

## TRIP STAGED 2026-09-24 03:05 IST — read this first, it supersedes everything below

**State (VERIFIED):** `origin/main` = `33ce814` (#109). CI is green on it; `scale-nightly` failed once
on its 5,000 entries/s throughput floor (4,103/s) and passed on re-run, which matches that job's
known flakiness (`f64e2df` failed at 2,852/s before). The installer in `packaging\dist` was built
from `33ce814` (`.buildsha`; SHA-256 `00eca09b…f43c`, 306,167,824 B), and is installed at
`%LOCALAPPDATA%\Programs\Reclaim` (the Aug-26 copy is renamed to `Reclaim.stale-20260826`).
The HKCU uninstall key `{B6C1B6C7-…}_is1` is present. The scheduled task is Ready. The trip is
staged in `C:\Users\Public\reclaim_ac3` from `33ce814` (installer hash-identical; stagehash written).

**Merged this round:** #106 (named test-suite allow-list), #107, #108 (VC++ runtime set + DLL
closure gate), #109 (`AIPackageLoadError`). **Open:** #110 (temp age guard uses subtree-newest
mtime; a real wrong-candidate class found while dogfooding).

**Findings to carry forward:**
- The 5ee5254 build shipped winrt's 14.29 `msvcp140.dll` at the dist root with no
  `msvcp140_1.dll`. Result: `import onnxruntime` failed, and `reclaim.exe` crashed with AV
  0xc0000005 in MSVCP140.dll 14.29 (Application event log, 3 crashes, all smoke runs on the
  never-shipped dist). Nuitka picked winrt's copy again in the 33ce814 build, so the pick is
  deterministic in the full build. Ruled out: #106 (it excluded 0 binaries), Nuitka 4.1.3→4.2.2
  (both ship the correct pair in isolation), and winrt import order (no repro in isolation). The
  exact trigger inside the full build is NOT isolated. #108 makes it moot, and the new closure
  gate fails on the 5ee5254 dist.
- The transient C: drops (down to 2.7 GiB) are pagefile extensions: 9.5 → up to 38.6 GB, released
  seconds to minutes later. They are driven by another session's Ollama loading 17.5 / 6.2 GiB
  models with mmap disabled (server.log timestamps line up). They are NOT reclaim: a scan's own
  transient disk is SQLite's `scan_seen` temp table, peak 1.74 GB, plus a WAL of at most 42 MB.
- WAL is bounded on the frozen build: scans 1 and 2 of `C:\Users\gaura` both leave 0 B of WAL
  after close (peaks 42 / 41.5 MB); the index is 4.89 GB for 5.86M rows.
- Safe mode puts every candidate in Tier B by design (ADR-0023 guarantee 3). A tier-A dogfood in
  safe mode finds 0 by design. Dogfood applied `crash_dump_file` (10/10, 121.7 MB, Recycle Bin).
- A dry-run `apply --tier both` processes ~2.7 s per candidate, about 21 h for 27,955 candidates
  (perf defect, not fixed). The `bytes_freed` label overstates Recycle Bin moves; the disk delta
  is ~0.
- Recycle Bin orphan `$R21C2JW` is NOT deleted: 63.5% of its files are hardlinked into live
  venvs, so the approval's precondition failed. Deleting it would free 8.13 GB (not 22.6).
  Waiting for GG.

**Next:** GG runs the trip from ReclaimSmokeTest (command in the session report). Merge #110,
then rebuild (warm, ~25 min with `-SkipCleanBuildDirs`) before any release.

## WAITING ON MERGE 2026-09-23 ~18:50 IST — superseded by the section above

**State at this checkpoint (VERIFIED unless marked):** `origin/main` = `e6d6c3f` (#105), CI green
on it (ci, eval, scale-nightly, pages all success). `verify.py` on
`e6d6c3f`: 1228 passed, 9 skipped, 94.56% coverage. Installer is still the stale 2026-08-26 build
(`packaging\dist\reclaim-setup.exe.buildsha` = `157be80`).

**Done this session:**
1. **Step 1 (measure before the build dir is wiped):** the stopped attempt-2 build's ccache log was
   parsed into `reports/build-timing/2026-09-23-attempt2-partial/` (summary, per-file CSV,
   PROVENANCE). First 279.6 min of C compile: scipy 80.1, numpy 58.2, onnxruntime 30.1, narwhals
   16.2, pydantic 14.5. numpy+scipy test suites account for 84.1 min of that. Projected full cold
   C stage: ~494 min (ESTIMATED; 862 files were never compiled). This supersedes the unsourced
   "~150 of ~197 min is scipy" figure.
2. **Step 2: PR #106 (draft)** `fix/nuitka-build-test-allowlist`. Named allow-list of 50 test
   packages, a static import gate (with an f-string review mechanism added after an adversarial
   verifier found that gap), `--report`, a post-build breakdown step, and a new frozen smoke test
   `packaging/test_packaged_serve.ps1` (serve + scan + AI). Expected saving ~199 min cold (84
   measured + 115 estimated). scipy **cannot be dropped**: imagehash.phash → `scipy.fftpack`,
   datasketch (top-level `lsh.py`) → `scipy.integrate`, lightgbm.basic → `scipy.sparse`
   (+ narwhals). Narrowing `--include-package=scipy` to those subpackages is a possible follow-up,
   not done (riskier: scipy's C extensions import each other in ways static follow may miss).
3. **Step 5 cleanup:** uv prune 1.6 GiB, pip purge 2.19 GB, npm 1.31 GB, conda 0.38 GB, HF
   detached revisions 2.14 GB (0 left; all 70 HF repos were accessed within the last 3 days, so
   the rest were only listed), %TEMP% >7d ~0.1 GB (1,941 items; guarded venvs/.git kept),
   orphaned worktree `agent-ad5d7026940ab1ee3` removed (0.39 GiB; every file's content matched git
   history or the LFS oids except a generated pytest-results.xml). Docker builder prune was
   skipped because the daemon was not running.
   **Open, needs GG:** C: free fell from 70.17 GiB (18:07) to ~34.9 GiB (~18:25) and then
   stabilized, from something that is not this session's work and could not be attributed. Ruled
   out: WSL (only active 17:56-17:57), pagefile/hiberfil, the Reclaim vault, and every >200 MB
   file written anywhere readable. Candidates that need admin to inspect: Windows Search index
   (SearchIndexer.exe wrote 29.6 GB since boot) and VSS shadow storage.

**Next, after GG merges #106:** fast-forward main, confirm HEAD == origin/main with CI green, then
rebuild with `pwsh packaging/build_installer.ps1`. It's a warm-ccache build, but the allow-list may
change compile flags and invalidate the cache (BELIEVED), so budget ~5h. Then
`test_packaged_serve.ps1` + `test_packaged_safe_mode.ps1`, and the Step 4 items (fresh install, two
scans with DB/WAL sizes, dogfood tier-1, HKCU uninstall key) and Step 6 (`Stage-AC3Trip.ps1`),
all unchanged from the section below.

## PAUSED 2026-09-23 15:12 IST — superseded by the section above where they differ

**Merged since the sections below were written**: #102 (crash-harness, BO2/BO3), #103 (WAL fix),
#104 (this file's previous revision). `main` = `7ac212c`, CI green on that SHA.

**Rebuild status: STOPPED deliberately, not finished.** Two attempts today:
1. Attempt 1 (from pre-#103 `7003525`): killed on instruction — it lacked the WAL fix.
2. Attempt 2 (from `7ac212c`, includes the WAL fix): started 10:12:34, stopped cleanly at 15:12:45
   (~5h00m) because the laptop was being closed — a sleep would have frozen it mid-compile and made
   its wall-clock meaningless. Still in Step 5 (Nuitka C compile) at stop; no `entry_point.dist`, no
   new installer. `packaging\dist\reclaim-setup.exe` is still the stale 2026-08-26 build
   (`157be80`). Process tree confirmed fully gone after stop.

**Before restarting the build, do these two things, in order:**
1. **Measure the per-package compile breakdown from the kept partial output.**
   `packaging\build\entry_point.build\` holds 2,214 `.o` files covering ~5h of serial
   (`--jobs=1`) compilation. With serial compile, sorting `.o` files by mtime and diffing gives
   per-file compile time; group by module prefix (`module.scipy.*`, `module.faiss.*`, ...). Report
   ccache hits separately (near-zero gaps). **The next build's Step 5 wipes this directory
   unconditionally — measure first or the data is lost.** This supersedes the W2/V4 "~150 of ~197
   min scipy" figure, which was a prior session's spot-sampled, never-committed chat number
   (BELIEVED, not a sourced measurement).
2. **Decide the ccache question for the timing comparison.** The ccache under
   `%LOCALAPPDATA%\Nuitka` is now warm with ~5h of compiled objects, so a plain restart will be
   faster for reasons unrelated to Defender. For a fair "what did the Defender exclusion buy"
   number (vs. the runbook's 180.4 / 349.3 min cold builds), either clear the ccache first or
   report the restart explicitly as a warm-cache build — not as a cold-build comparison.

**Timing facts to carry into the report:**
- Defender exclusions VERIFIED active by GG (Get-MpPreference lists
  `C:\Users\gaura\AppData\Local\Nuitka` and `C:\Users\gaura\ml-projects\reclaim\packaging`). The
  build script's own Step 4 still logs SKIPPED (it runs unelevated) — that's independent and
  harmless.
- Contention caveat: at attempt 2's start, another concurrent session had heavy processes running
  (`pip install -e .[tes...]` in `oss-contrib\adk-python-verify`, and `find / -iname
  merge_gate.py`) — not this session's, deliberately left untouched. Attempt 2 had already exceeded
  ~3-3.5h without finishing; that is NOT evidence the exclusion bought nothing, given the
  contention and an unfinished run.

**Then resume BQ3/BQ4 unchanged** (all pre-approved by GG): rebuild from `origin/main` → fresh
install to the gaura profile (not the stale `AppData\Local\Programs\Reclaim\` dir) → two
consecutive scans with DB+WAL sizes after each (WAL must stay bounded, not merely checkpointed
once) → dogfood scan/apply, tier-1 via recycle bin/vault only, found vs. applied, any wrong
candidate = product PR → HKCU uninstall-registration check → `Stage-AC3Trip.ps1`, Step -3 ==
`origin/main`, one-line ReclaimSmokeTest config check, trip instructions (human list: did a toast
render). One final report: build wall-clock vs prior, C: free before/after, GiB per item C1/C2/C3,
WAL sizes across scans, PRs in merge order, anything needing GG.

**Queued AFTER BQ3/BQ4 — plan only, do not implement without GG's go-ahead:**
- Build-time fix: exclude test packages from Nuitka's follow set via a **named allow-list proven
  unused at runtime, never a glob** — `RELEASE_RUNBOOK.md` records that the earlier blanket
  `--nofollow-import-to=*.tests`/`*.testing` broke `structlog.testing`, `jinja2.tests`, and
  `scipy._external.array_api_extra.testing` (runtime imports) and crashed the packaged app on every
  invocation. Plan must include: (a) a static check that fails the build if any excluded package is
  imported by non-test code; (b) a frozen smoke test that starts `reclaim.exe serve` and exercises
  one scan; (c) whether scipy is needed at runtime at all, and via which import chain; (d) expected
  build-time impact, from the fresh per-package measurement above.
- Add Nuitka's `--report=<path>.xml` to `build_installer.ps1` so the per-module breakdown is a
  first-class build output. **Unverified**: the report may record only Python-level per-module
  optimization time, not per-file gcc C-compile time (where the scipy/faiss hours are). Check the
  first report it produces; if C-compile timing is absent, keep the `.o`-mtime method as a scripted
  post-build step.

**Other session outcomes (detail in the sections below):** C1 deleted top-level `dist/` (671MB) —
nothing else qualified once measured; the orphaned `.claude\worktrees\agent-ad5d7026940ab1ee3`
(412MB, no git metadata) left in place for manual review; all 96 `[gone]` branches refused
`git branch -d` (squash-merge history; `-D` not used per instruction). Real vault's 4.85GB WAL
checkpointed to 0 manually. C2 (native cache cleanup) not yet run.

## What happened before this checkpoint

The machine restarted at 2026-09-23 06:47-06:50. **Corrected framing, VERIFIED**: this was a
**planned Windows Update restart** (`TrustedInstaller.exe`, Event 1074/109/6006/6005 in the System
log), **not an unexpected power-off** — no Event ID 41/6008 (dirty-shutdown markers) anywhere in
the log. A prior session's last git activity was a commit at 03:24:53 on
`fix/crash-harness-exit-code-diagnostics`; whatever it was doing for the ~3h25m gap before the
reboot is not recorded in git. Full integrity check (git fsck, `reclaim recover` against both the
dev fixture manifest and the real installed vault at
`C:\Users\gaura\AppData\Local\Programs\Reclaim\`, all 37 project `.venv`s, build artifacts,
scheduled task) came back clean — nothing was damaged or half-applied. See this session's chat log
for the full Step 1/Step 2 forensic detail if it's ever needed again; not reproduced here.

## Done this session (2026-09-23)

- **Track 2 (crash-harness exit code) pushed and CI-clean.** The interrupted session's BO3
  conclusion (`9c884c8`: the 0xC000070A flake is a real, reproducible OS-level phenomenon under
  synthetic concurrent-process-creation + disk-churn load, but not a product defect — see below)
  had never been pushed to origin. Force-pushed with `--force-with-lease` (branch was already
  correctly rebased onto current `origin/main` locally, no new rebase needed). **PR #102 is now
  OPEN, MERGEABLE, all 6 CI checks SUCCESS.** Still draft — needs your merge.
  - **Verification of the "not a product defect" claim, independently re-checked**: exactly 2
    production subprocess-spawn sites exist in `src/` (`scanner.py::_query_git_clean`,
    `ai/eval_harness.py::current_commit_sha` — dev-tooling only), both plain `subprocess.run` with
    broad exception handling, neither running under a contention shape that reproduces the
    combined python-spawn+disk-churn load that caused 4/50 failures. **One discrepancy worth a
    follow-up**: `packaging/reclaim.iss` (lines ~459/475) documents "a scan/AI worker spawned via
    multiprocessing" as a reason the uninstaller force-kills the process tree, but no
    `multiprocessing`/`ProcessPoolExecutor` call exists anywhere in `src/` — either stale installer
    commentary or an unfound code path. Doesn't overturn BO3's conclusion but wasn't itself checked
    by BO3's own grep (which never searched for `multiprocessing`).
- **New product defect found and fixed: unbounded WAL growth.** The real installed vault's SQLite
  index had a 4.85GB WAL file against a 3GB main DB — confirmed via `wal_checkpoint(TRUNCATE)` to
  have 0 pending frames (pure dead disk space, no durability risk, but present on every install).
  Root cause: SQLite's automatic checkpoints reclaim WAL frames logically but never shrink the file
  on disk, and `ScanIndex.close()` never ran an explicit checkpoint. **PR #103 (draft, CI
  in-flight): `journal_size_limit` + explicit TRUNCATE-on-close, with a regression test that
  reproduces the actual growth condition.** Checkpointed the real vault manually as an immediate
  mitigation: WAL/SHM reclaimed to 0 bytes (freed ~4.85GB on this machine right away, independent
  of the PR landing).
  - **Related finding, not fixed**: the 3GB index itself has presence-based pruning (rows for
    deleted files get removed on rescan) but no age/TTL retention and no `VACUUM` — deleted rows
    free space logically but the `.sqlite3` file itself never shrinks. Matches AS3's prior
    "measured not fixed" finding. Flagged in PR #103's description, not addressed there.
- **Git hygiene**: force-push above; `git fetch --prune` cleared ~140 stale remote-tracking refs.
  Attempted `git branch -d` (never `-D`, per instruction) on the resulting 96 `[gone]`-upstream
  local branches — **all 96 refused as "not fully merged."** This repo squash-merges PRs (one
  commit per PR on `main`), so `git branch -d`'s ancestry check can never recognize these as merged
  even though their content landed — a structural mismatch between the requested method and this
  repo's merge style, not a failure to act. Zero branches deleted; `-D` was not used since it was
  explicitly excluded. `git worktree prune` found nothing (the one orphaned directory has no git
  metadata at all for prune to act on — see below).
- **C1 cleanup**: measured before touching anything. Only one item qualified under the deterministic
  rules once actually measured:
  - Deleted top-level `dist/` (`cli.build` + `cli.dist`, 671MB) — confirmed gitignored, untracked,
    unreferenced anywhere in `packaging/`, `src/`, or `scripts/`; a stray Nuitka output for a
    different, no-longer-used build target, dated 2026-07-18.
  - `C:\Users\Public\reclaim_ac3\ac3_run_*.txt`/`reclaim_app_*.log`: **nothing pruned** — exactly 3
    trip runs exist (all 2026-08-26), and the rule is "keep 3 most recent," so nothing exceeds it.
  - The "~1GB AT3-reset backup index" named in the original brief: **not found anywhere** — no
    reference in `docs/`, no matching file on disk. Either already cleaned up in an untracked way,
    or a misremembering; not guessed at further.
  - `.claude/worktrees/agent-ad5d7026940ab1ee3` (412MB, dated 2026-08-20/22): **left alone,
    flagged, not deleted.** It has no `.git` file and no corresponding entry in the main repo's
    `.git/worktrees/` — not a registered worktree at all, so there is no git history to check for
    "unpushed commits" as instructed. Fail-closed per this project's own guard-verification
    principle: needs manual review or a content diff against known history, not a guess.
  - `packaging/build` (Nuitka log/telemetry cache): only 20KB, not worth a separate action, and
    was about to be overwritten by this session's rebuild anyway.
- **Rebuild kicked off** (item 4): from current `origin/main` (`7003525`) — **does not include the
  WAL fix**, since PR #103 isn't merged and this session never self-merges. `packaging/reclaim.iss`
  had 96 changed lines vs. the prior build's source commit (`157be80`) with 0 changes in `src/`, so
  a rebuild was required regardless of the WAL fix's timing. No incremental Nuitka cache existed to
  reuse (`packaging/build` had no `.build`/`.dist` subdirectories) — full from-scratch build,
  ~3-6 hours per this repo's own documented range. **Whoever resumes next: check whether this
  build finished; if PR #103 has merged by then, rebuild again on top of it before the real trip.**

## Still open from before this session (unchanged, not re-verified)

- **U6 (`ReclaimSmokeTest` deletion) — still explicitly DEFERRED.** Two reasons stand: (1) its
  profile is the only one on this machine with the 8.3-short-name condition the TEMP-cache
  detection fix depends on for regression coverage; (2) the last trip (`181912`, 2026-08-26) never
  actually exercised `#98`'s `[BI3]` AUMID diagnostic due to a staging-staleness bug (BL7) — **one
  more trip against a `Stage-AC3Trip.ps1`-staged, current-main build is still required** before
  this account's job here is done. Do not run U6 until that trip produces real `[BI3]` evidence.
- **Toast from Step 10 — still genuinely open.** Notifications are not armed on either the stale
  build's install directory or a fresh one by default (`config.toml` ships with no `[notifications]`
  section at all, confirmed both on gaura's real install and the repo's shipped default). Turn the
  Settings-tab toggle on before the next trip.
- The dozens of stale `.claude/worktrees/agent-*` directories (~850MB total in `.claude/worktrees/`)
  — real disk usage, mostly not actioned this session either (only the one orphaned, git-less
  directory was specifically investigated; the rest weren't re-swept).
- Everything under "Disclosed, not re-opened" / "Believed, not verified" / "Deliberately deferred"
  in this file's prior (2026-08-26) version stands unchanged — not reproduced here to keep this
  checkpoint legible; read the git history of this file if the detail is needed.

## Not yet done from this session's own instructions (next session picks up here)

1. **C3 dogfood scan/apply against the CURRENT build** (once the rebuild above finishes) — the only
   prior dogfood evidence (`docs/CASE_STUDY.md`, 33.73GB verified reclaim) is from **2026-07-17/18**,
   predates 7+ P0 fixes since, and does not answer whether the current build produces wrong
   candidates on a real drive. Tier-1 categories only, via recycle bin/vault. Report found vs.
   applied; any wrong candidate is a product PR, not a silent skip.
2. **Item 6 (uninstall registration)**: confirmed **zero** entries for Reclaim in
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall` (8 unrelated entries present, so the
   per-user registration mechanism itself works on this machine — Reclaim just isn't in it) despite
   a live-looking install directory with `unins000.exe` present. `reclaim.iss` has no explicit
   `[Registry]` uninstall-key section, which is normal (Inno Setup registers this automatically) —
   so the absence is unexplained by the script itself. Most consistent theory given this project's
   own documented history (a pre-`fix/uninstaller-terminate-running-process` uninstall that removed
   the registry key but left locked files behind): this directory is stale post-uninstall debris,
   not a currently-registered install. **Needs a fresh install+uninstall cycle with the NEW build to
   get a real answer** rather than more archaeology on the stale directory — do this as part of
   item 1's dogfood work, using a clean install rather than reusing this directory.
3. **Track 3 trip prep**: `Stage-AC3Trip.ps1` was never actually run this session (no `.stagehash`
   sidecar in the stage dir; everything in `C:\Users\Public\reclaim_ac3\` is still Aug 26). Run it
   against the new build once ready, confirm Step -3 == `origin/main`, arm notifications, then the
   real trip is still a human-required step (toast-render observation).

## Exact resume sequence

```powershell
# 1. Check the rebuild finished (packaging\build\build_run_2026-09-23.log, packaging\dist\reclaim-setup.exe.buildsha)
git fetch origin --quiet
gh pr view 102 --json state,mergeable   # should already be OPEN/MERGEABLE/CI-green -- merge when ready
gh pr view 103 --json state,mergeable,statusCheckRollup   # WAL fix -- check CI, merge when ready

# 2. If PR #103 merged since the rebuild started, rebuild AGAIN on top of it (product code) before
#    doing anything below -- the first rebuild deliberately did not wait for it.
git checkout main; git pull --ff-only
git diff --stat <packaging\dist\reclaim-setup.exe.buildsha> origin/main -- src/ packaging/reclaim.iss
# non-empty -> rebuild again: pwsh packaging/build_installer.ps1

# 3. Fresh install (not the stale C:\Users\gaura\AppData\Local\Programs\Reclaim\ directory --
#    see item 6 above) + dogfood scan/apply (tier-1, recycle bin/vault only) + report found vs
#    applied against the real drive.

# 4. Stage-AC3Trip.ps1, confirm Step -3 == origin/main, arm notifications (Settings tab), run the
#    real trip. Human list: did a toast render (Step 10).
```
