# Resume checkpoint — 2026-08-26 (post-AC3-trip, BD1-BD5 fixes drafted)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
Always `git fetch origin` + `gh pr list` and re-check `C:\Users\Public\reclaim_ac3\` for what has
actually run before trusting any claim below, including this one (rule 118a) — this file has
gone stale mid-engagement more than once already, including between the last checkpoint and this
one (it claimed PRs #85/#86 were still open drafts; both had already merged by the time the real
trip ran).

## Where we are — the real AC3 trip ran, artifact was product-current, four real findings

The interactive AC3 trip ran for real against rebuild #7
(`d34b55f340a57e3df1ab48eaf0f3a0efdc0878f2769943508d728e6c67d48505`, buildsha `e63ca72`). That
build's buildsha is 2 commits behind `origin/main`'s tip at trip time (`97675ad`, PRs #85/#86) but
those 2 commits touch only `docs/` and `packaging/smoke/*.ps1` — zero `src/` changes, confirmed via
`git diff --stat` and ancestry checks. **The trip tested product-current code**, including PRs
#76/#81/#83 (confirmed ancestors of the build basis).

**Real trip results** (`ac3_run_20260826_012613.txt` + user's own direct observations): 1 ABORT
(Step 1, Task Scheduler), 0 FAIL, 2 WARNING, Steps 2/3/4/6/7/8 PASS, first-run screen never
observed (contaminated again), toast codepath never actually exercised (structural gap, not weak
evidence). Four real findings opened and fixed this session (BD1-BD4), one investigated with a
plausible-but-unproven explanation (BD5) — see `docs/AUDIT-2026-08.md`'s BD1-BD5 sections for full
detail. Summary:

- **BD1**: Task Scheduler ABORT root-caused — NOT AY1 recurring. Task names are a machine-wide
  namespace; this dev machine's own prior gaura-account install already owns
  `"Reclaim Disk Space Check"` in `C:\Windows\System32\Tasks\`, and `ReclaimSmokeTest` (a
  different, non-admin account) has zero ACL rights to overwrite it — confirmed via `icacls`,
  deterministic, not a race. Real product-facing gap on any real multi-account PC, not just this
  test rig. Fixed: `reclaim.iss` now captures the real `schtasks` exit code + stderr into
  `{app}\data\task_registration_diagnostic.log`, always, with a specific "access denied by another
  account's task" message when that's the cause. Verified: `packaging\reclaim.iss` recompiles
  clean via `ISCC.exe` (exit 0) against the existing dist tree. Namespace collision itself and a
  UI surface for the diagnostic are explicitly out of scope for this pass.
- **BD2**: the toast evidence chain was never valid, not just incomplete — `--apply-snooze`
  (which every one of Steps 2/3/4 pass) returns from `cli.py`'s `_run_check_disk_space` before
  `send_disk_space_toast` is ever reached; only Step 1 (which keeps aborting, see BD1) reaches it.
  Every prior "no toast_failed line" conclusion in this document was trivially true because the
  call was never made. Fixed: new Step 10 clears snooze state and invokes plain `check-disk-space`
  directly, checking `reason=`, `last_notified_at` mutation, and a fresh log re-copy as three
  independent signals — not yet run against a real account.
- **BD3**: first-run ack (`{app}\data\first_run_state.json`) survives reinstall because Inno's
  uninstaller never removes runtime-created files under `{app}\data\` — confirmed via source +
  the user's own direct observation this trip (no overlay, straight to Simple-mode dashboard).
  Fixed: a new step deletes the marker before every real install.
- **BD4**: AN1/AZ4's "unexplained scan trigger" recurred a third time, and this session found
  *why* it's been unroot-causable all three times: `logging_config.py`'s 5MB rotating log gets
  filled and rotated by a single heavy dedup computation within ~20 seconds, evicting the
  `api.scan_initiated` evidence before Step 9 (which only ever copied the active log file) runs.
  This instance's specific origin/token is genuinely unrecoverable now. Fixed: Steps 9/10 now copy
  every `reclaim.log*` file, active and rotated, so a fourth instance has a real chance.
  **Not fixed**: the scan trigger itself, by design (same posture as AN1/AZ4).
- **BD5**: the same in-flight full-drive scan BD4 covers was running *during* Step 7's free-space
  measurement window — a plausible (BELIEVED, not VERIFIED — direct evidence is gone, same
  rotation gap) explanation for the 52.11% secondary-check gap. No code fix; a future
  wait-for-cancellation-to-settle improvement to Step 7 noted, not implemented.

**Open draft PRs from this session, not yet merged:**
- **#88** — BD1 (`packaging/reclaim.iss` diagnostic capture).
- **#89** — BD2/BD3/BD4/BD5 (trip-script apparatus fixes).
- This PR — RESUME.md refresh only.

## Exact resume sequence

```powershell
# 1. Merge #88, #89, and this PR (draft, CI-pending/green -- check before merging; #88 and #89
#    both touch docs/AUDIT-2026-08.md at the same append point, so the second one merged will
#    likely need a trivial conflict resolution in the web UI) in the web UI, then:
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main

# 2. A REAL rebuild is now needed before the next trip -- BD1's fix changed packaging/reclaim.iss,
#    which IS compiled into the installer (unlike PRs #85/#86's docs/trip-script-only changes).
#    Run packaging/build_installer.ps1 in full (~50 min), producing rebuild #8 with correctly
#    regenerated .sha256/.buildsha sidecars. Do NOT reuse rebuild #7's artifact -- this is the
#    first rebuild this session where source actually changed since the last one.

# 3. Re-stage the merged trip script + rebuild #8 artifact:
Copy-Item packaging\dist\reclaim-setup.exe,packaging\dist\reclaim-setup.exe.sha256,`
  packaging\dist\reclaim-setup.exe.buildsha,packaging\smoke\ac3_login_diagnostic.ps1 `
  C:\Users\Public\reclaim_ac3\ -Force

# 4. One more dry run (frozen suite + trip script) to confirm rebuild #8 is clean before the next
#    real interactive trip.

# 5. The next real trip should specifically verify: BD1's diagnostic file appears and reads
#    correctly on a real ReclaimSmokeTest install (the access-denied message, specifically);
#    BD2's new Step 10 actually reaches reason=would_notify and updates last_notified_at; BD3's
#    reset actually produces a genuine first-run observation this time.
```

## The two irreducible human steps for the trip (unchanged)

1. Watch for toasts across Triggers 1-4 (now also Step 10) in the trip script, note which (if any)
   rendered. **Last trip: 0 toasts** (consistent with BD2's finding that the codepath was never
   reached).
2. Describe the first-run screen. **Last trip: no overlay appeared, went straight to Simple-mode
   dashboard** — now known to be BD3's contamination, not a real "no first-run screen" product
   fact. Re-attempt after BD3's fix ships in a real rebuild.

## Test account state

`ReclaimSmokeTest` — used for a real interactive trip this session (2026-08-26). Exists, enabled,
was reachable this time (unlike the AC2 unreachable episode). **U6 (delete the account) stays
deferred** until a clean trip against rebuild #8 comes back clean on BD1/BD2/BD3's fixes.

## Open-items list

**Blocking the next real trip:** merge this session's PR batch, run a real `build_installer.ps1`
rebuild (rebuild #8 — first source-affecting rebuild since #7), re-stage, one confirming dry run.

**Disclosed, not re-opened:** BD4's specific scan-trigger origin for this trip (unrecoverable, see
above); the machine-wide Task Scheduler namespace collision itself (BD1, scoped out of this pass);
a UI surface for the task-registration diagnostic (BD1, scoped out); Step 7 waiting for scan
cancellation to fully settle before baseline sampling (BD5, noted not implemented).

**Deliberately deferred:** U6 (above).
