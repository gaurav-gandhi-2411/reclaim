# Resume checkpoint — 2026-08-26 (BL1-BL7 -- audit-log display bug, tally bug, stale-config root cause, tight process polling, AND a real trip-apparatus staleness incident)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
Always `git fetch origin` + `gh pr list` and re-check `C:\Users\Public\reclaim_ac3\` for what has
actually run before trusting any claim below, including this one (rule 118a) — this file has gone
stale mid-engagement multiple times already this week, **and this round is the first time that
staleness actually corrupted a real trip's own evidence, not just a doc — see BL7, read it before
trusting any AUMID/toast conclusion below.**

## Where we are — BL1-BL6 fixed/closed from a real clean trip; BL7 found the trip script itself was stale

**Merged this session**: BD1-BD5 (#88-#90), BE1/BE2/BE4 (#91-#93), BA1 (#87), BF1/BF2/BF3-BF5
(#94), a RESUME checkpoint (#95), BH1-BH7 (#96, #97), BI1-BI3 (#98). **One more PR about to be
drafted this session**: BL1/BL2/BL4/BL6/BL7 + S5 (see `docs/AUDIT-2026-08.md`'s BL sections) — found
while investigating a real, clean `ReclaimSmokeTest` trip (`ac3_run_20260826_181912.txt`, 0
ABORT/FAIL/ERROR).

**Real findings from that trip, closed**:
- **BL1**: `reclaim_audit.log` showing "0 MB" was a Step 9 display-rounding bug, not a broken audit
  sink — the sink was proven correctly wired via 3 independent live tests. Fixed the display; the
  file actually held 3 real `api.scan_initiated` events.
- **BL2**: SUMMARY undercounted WARNING (1 reported, 2 real) — the same tally-bug class BF4 already
  fixed for SKIPPED, now generalized to all 5 severities via one shared function, with a real Pester
  regression test (`SeverityTally.Tests.ps1`, 6/6 passing).
- **BL3**: Step 10's `threshold=50.0` (vs BH5's shipped `80.0` default) was `ReclaimSmokeTest`'s own
  surviving hand-edited config, not a fresh-install defect — confirmed via a real scratch install
  that BH5's actual defaults write correctly.
- **BL4**: Step 1's 3s/5-sample process polling replaced with a 100ms poll (measured real
  `check-disk-space` runtime: ~0.51s; validated 5/5 live). The originally-requested Task Scheduler
  Operational event log is confirmed infeasible without elevation (`wevtutil sl .../e:true` →
  Access is denied unelevated) — reported as a substitution, not silently swapped.
- **BL6**: residual, not fixed — uninstalling ANY Reclaim install on an account deletes that
  account's per-account Task Scheduler task regardless of which install directory registered it.
  Hit and repaired twice this session (gaura's own task). Flagged for a future design pass.
- **S5**: the first-run overlay was directly observed by a human for the first time this entire
  engagement — closes the presence/absence question BD3+BH2 existed to enable.

**BL7 — a real process defect in this session's own work, corrected before it shipped**: the
`ac3_run_20260826_181912.txt` trip that BL1-BL3/S5 above are based on ran against a **pre-#98**
trip script — this session's own local `main` was one commit behind `origin/main` (rule 118a) when
the prior turn re-staged the script, so the stale local file got copied instead of `origin/main`'s
actual content. Consequence: `#98`'s AUMID diagnostic (`Get-AumidNotificationState`,
`[BI3]`-tagged `PeriodicNotificationCount` evidence) never ran on this trip — zero `[BI3]` lines
exist anywhere in that log. **The toast question is therefore still open, not closed**: Step 10 DID
reach `reason=would_notify` (percent_used 89.46% > threshold 50.0, `last_notified_at` updated) and
the human observer still saw no toast — the exact AH1 shape recurring, now with `#98`'s diagnostic
still unexercised on `ReclaimSmokeTest`. Fixed this session: local `main` fast-forwarded to
`origin/main`, script re-staged fresh (confirmed byte-identical, 27 case-insensitive "aumid" matches
present), BL1/BL2/BL4 correctly layered back on top. **The next trip is the first one that will
actually produce real `[BI3]` evidence for `ReclaimSmokeTest`.**

**Rebuild #9**: SHA-256 `d9ff12552187ff8751457a4f623a0db5f33c3822950ca2d2cf140dfb065a1975`, built
from `157be80e0bfc6270a75be7df3fc199f26d7e12bf` (= `origin/main`'s tip after #96/#97 merged, CI
green, confirmed via `git log origin/main -1` before the build). **A full dry run against this
rebuild, under gaura's own account, with BH2's fresh-server guarantee active, came back clean: 0
ABORT, 0 FAIL, 0 WARNING, 0 ERROR (1 SKIPPED — Step 10's `reason=disabled`, correct and expected:
gaura's install now ships BH5's real default, `[notifications] enabled = false`).** Steps 6/7/8
all fired `[OK -- BH2] Confirmed: port 8420 is owned by PID <N>, the process this step just
launched` — genuinely confirmed-fresh results this time, not assumed. **S2/U4 (Step 7) is
re-closed** for this run specifically — see BI1 for why it was reopened in the first place.

## Exact resume sequence

```powershell
# 1. Merge the BL1/BL2/BL4/BL6/BL7 PR (draft, about to be created) in the web UI, then:
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main -- DO NOT skip this. BL7 (this session) is the
                     # documented cost of skipping it: a real trip's own script silently ran stale.

# 2. Re-stage using packaging\smoke\Stage-AC3Trip.ps1 -- BM1 (2026-08-26 audit): NOT a manual
#    Copy-Item anymore. That manual step is what let BL7 happen (and, before it, the original
#    STALE-BASE VERIFICATION GAP) -- a script that structurally cannot stage without first,
#    freshly, re-verifying local main == origin/main (no override; no cached/remembered state):
.\packaging\smoke\Stage-AC3Trip.ps1
# Aborts loudly and refuses to stage if local main isn't confirmed current -- if it does, that IS
# the correct behavior: fetch/fast-forward (step 1 above) and re-run this, don't work around it.

# 3. U6 (delete ReclaimSmokeTest + its profile): DEFERRED per this session's explicit instruction
#    -- do NOT run it. The account is the only profile on this machine with the 8.3 short-name
#    condition, and BL7 means one more trip is still needed before the account's job here is done.
#    Re-evaluate only after a trip against a Stage-AC3Trip.ps1-staged script produces real [BI3]
#    evidence for the toast question -- see "Test account state" below.
```

## The two irreducible human steps for the trip

1. **First-run overlay** — confirmed appearing for real on the last trip (S5, closed this session):
   a human directly observed it on a genuinely fresh `ReclaimSmokeTest` profile, BD3 + BH2's
   guarantees both active. No longer an open question for a NEW test account unless it recurs, in
   which case that is a new, real finding.
2. **Toast from Step 10 — still genuinely open, read BL7 above before doing this.** The next trip
   is the first one where `#98`'s `[BI3]` AUMID diagnostic will actually run (BL7 found and fixed
   the staging gap that silently skipped it last time). Turn the Settings-tab notifications toggle
   on before running the script (BH5's fresh-install default is `enabled = false`), so Step 10 has
   a real chance to reach `reason=would_notify` rather than `SKIPPED`. If it does, watch for a real
   toast AND read the trip log afterward for `[BI3]`-tagged lines — those narrow the cause (OS
   pipeline queued it vs. never reached the pipeline vs. `Enabled=0` for this AUMID) independent of
   what you personally observed on screen.

Nothing else belongs on this list — Task Scheduler registration (BE1, live-verified for two real
accounts), the CWD-independence fix, the Snooze protocol handler, AE1's teeth-proof, the
free-space-delta measurement, and 8.3 short-name detection are all automated, confirmed against a
positively-verified-fresh server, and passing for real.

## Test account state

`ReclaimSmokeTest` — last used for the real, clean trip on 2026-08-26 (`181912`). **U6 (account
deletion) is explicitly DEFERRED — read this before deleting it, this session or a future one.**
Two independent reasons, both from the user directly, both current as of this checkpoint:
1. `ReclaimSmokeTest`'s profile is the only one on this machine confirmed to exhibit the 8.3
   short-name condition (`$env:TEMP` resolving as `RECLAI~1`) that the TEMP-cache detection fix
   (rebuild #8.3-short-name work) depends on for its own regression coverage — deleting it loses
   that fixture, not just a disposable account.
2. BL7 means the `181912` trip's own toast/AUMID evidence doesn't actually count — `#98`'s `[BI3]`
   diagnostic never ran on it. **One more trip, against a `Stage-AC3Trip.ps1`-staged script, is
   still required** before this account's job here is done. A session that sees "the last trip was
   clean" and reasons U6's gate is therefore satisfied would be repeating exactly the mistake this
   note exists to prevent — the gate is "a trip whose toast evidence is trustworthy," which
   `181912` was NOT, not merely "a trip with 0 ABORT/FAIL/ERROR."
Do not run U6 until a session confirms, freshly, that a trip run against a properly
`Stage-AC3Trip.ps1`-staged script produced real `[BI3]` lines in its log.

## Open-items list (flat, everything outstanding)

**Blocking the next real trip:**
- Draft and merge this session's PR (BL1/BL2/BL4/BL6/BL7 — trip-script fixes + audit-doc
  corrections, including BL7's own staging-staleness fix).
- Run U6 (`ReclaimSmokeTest` deletion, exact steps in the session report) as admin, then create a
  fresh disposable test account.
- Re-stage from merged `origin/main` (step 2 above, no rebuild needed) — **verify byte-identical
  AND verify `git rev-parse HEAD` equals `origin/main` immediately before copying**, per BL7.
- Run the actual trip under the new test account — this is what determines BI3's real root cause
  (does the account's own AUMID key show `Enabled=0`? does `PeriodicNotificationCount` increment
  there?) for the first time, since BL7 found the prior trip never actually exercised this
  diagnostic despite appearing to.

**Disclosed, not re-opened (won't be fixed without a specific reason to revisit):**
- The machine-wide Task Scheduler namespace collision's underlying cause (task names are global)
  is not restructured — BE1 works around it (per-account naming), doesn't eliminate the shared
  namespace itself. A UI surface for `task_registration_diagnostic.log` (BD1) is not built.
- The scan-trigger mechanism (AN1/AZ4/BH4) has never been root-caused across four occurrences now
  — fixed pragmatically each time (cancel-then-proceed) or found unrecoverable (BH4's specific
  instance), by design, not chased further as its own investigation.
- BD5's original 52.11% secondary free-space gap: BELIEVED (not VERIFIED) to correlate with a
  concurrent scan; direct evidence gone. This session's own re-run (BI4/rebuild #9) showed a real,
  clean, in-threshold secondary check (0.08% gap) — consistent with BD5's noise theory, not new
  evidence either way. Step 7 waiting for scan-cancellation to fully settle before baseline
  sampling — still noted, not implemented.
- BE2's audit-log treatment (dedicated `reclaim_audit.log`) covers only `api.scan_initiated`'s
  three call sites, matching exactly what was asked. Other rare security-relevant events (e.g.
  scan-confirmation-token consumption/denial) were not swept for the same treatment.
- Check 1d (preflight hardlink/lock probes, frozen smoke suite) — last confirmed BLOCKED earlier
  in this engagement, needs a dedicated two-hardlinked-file fixture; not re-verified this session.
- Check 3b (toast visual confirmation) — permanently human-only by nature, no headless harness can
  close this regardless of how many other toast-related checks pass.
- Dozens of stale `.claude/worktrees/agent-*` directories from earlier in this engagement — real
  disk usage, flagged historically, never actioned since cleanup wasn't requested.

**Believed, not verified (explicitly tagged as such in the record):**
- Whether the SAME literal OS process persisted from trip `012613` through to `130328` (BI2) —
  plausible reconstruction from evidence, not proven; `auditpol` (the one thing that could settle
  it) requires elevation this project's own invariant forbids requesting, confirmed blocked.
- BI3's root cause on `ReclaimSmokeTest` (or its successor account) — genuinely unknown, now
  pending a trip that actually runs the diagnostic (BL7 found the last one didn't), not guessed at.
- BL6's per-account Task Scheduler task deletion on any uninstall — real, reproduced twice, exposure
  believed narrow (concurrent same-account installs), not independently verified as narrow.

**Deliberately deferred:**
- Task Scheduler subfolder organization for R5 — considered and rejected in favor of per-account
  naming (BE1), not merely postponed.
- `disk_threshold_percent` Settings-tab editing (BH5) — stays config.toml-only by design, same
  scope as every other category's non-`enabled` field.
- BL6's fix (task-deletion-on-uninstall keyed to install-directory identity) — needs its own design
  pass, not a reactive patch under an unrelated fix.
