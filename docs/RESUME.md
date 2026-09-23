# Resume checkpoint — 2026-09-23 (post-reboot reconstruction: crash-harness track pushed clean, WAL leak found+fixed, rebuild in flight)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`. Always
`git fetch origin` + `gh pr list` before trusting any claim below, including this one (rule 118a).

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
