# Resume checkpoint — 2026-09-23 (post-reboot reconstruction: crash-harness track pushed clean, WAL leak found+fixed, rebuild in flight)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`. Always
`git fetch origin` + `gh pr list` before trusting any claim below, including this one (rule 118a).

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
