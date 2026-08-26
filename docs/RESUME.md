# Resume checkpoint — 2026-08-26 (rebuild #8 dry-run clean, one PR from the real trip)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
Always `git fetch origin` + `gh pr list` and re-check `C:\Users\Public\reclaim_ac3\` for what has
actually run before trusting any claim below, including this one (rule 118a) — this file has gone
stale mid-engagement multiple times already this week.

## Where we are — rebuild #8 dry-run clean under gaura's own account; one PR left before the real trip

**Merged this session**: BD1-BD5 (#88-#90), BE1/BE2/BE4 (#91-#93), BA1 (#87). **Still open**:
**#94** — BF1/BF2 (trip script asserts the per-account task name, real second-account collision
groundwork) + BF3-BF5's dry-run findings (a real SUMMARY tally bug found and fixed) + the manual
toast-codepath verification writeup. CI was still running on #94 as of this checkpoint — verify
green before merging.

**Rebuild #8**: SHA-256 `e670a0480e1221d740834d3196cad6529919a53119892f1e1631903f10d5e2d0`, built
from `04a49a849e25fc89fbd9e2c43b6cb3dba4271635` (= `origin/main`'s tip, CI green, confirmed via
`git log origin/main -1` immediately before AND after the build — a first attempt accidentally
built from an unmerged branch and was discarded before staging, see AUDIT's BF3 section). Staged
to `C:\Users\Public\reclaim_ac3\` and confirmed byte-identical. **The staged trip script is
sourced from PR #94's branch content, not `origin/main`** — needed for Step 1's per-account task
query to work at all post-BE1; re-stage from `origin/main` once #94 merges (should be a no-op,
content is identical).

**Full dry run, gaura's own account, rebuild #8**: 0 ABORT, 0 FAIL, 0 WARNING, 0 ERROR. Every step
real PASS or correctly-explained non-failure — see `docs/AUDIT-2026-08.md`'s BF3/BF4/BF5 section
for the complete walk. Highlights:
- Step 1 (BF1): `"Reclaim Disk Space Check (gaura)"` confirmed registered and `Ready`; old
  shared-name task confirmed migrated away.
- Step -1.5/-1 (BD3): first-run marker confirmed deleted; raw `/api/first-run` read
  `{"acknowledged":false}` both before and after the unattended run — genuinely fresh state,
  first time this engagement, though still not a human's visual observation.
- Step 10 (BD2): SKIPPED (`reason=disabled`) in the scripted run — `gaura`'s dev `config.toml`
  has no `[notifications]` section (predates that section, never touched by the installer's
  `onlyifdoesntexist` upgrade-preservation logic). **Not a defect** — verified separately by hand:
  with notifications temporarily enabled, `check-disk-space` reached
  `reason=would_notify percent_used=87.9% threshold=50.0`, updated `notification_state.json`, and
  logged no `toast_failed` — **the actual `send_disk_space_toast` call chain confirmed firing for
  real on the frozen binary for the first time this entire engagement.** Config was restored
  immediately after.
- A real apparatus bug found in the same pass: the SUMMARY's `SKIPPED` tally required an exact
  `[SKIPPED]` close-bracket, undercounting Step 10's own `[SKIPPED -- real, not a bug]` line as 0
  instead of 1. Fixed to match the same bare-prefix pattern `$aborts`/`$fails` already used.

## Exact resume sequence

```powershell
# 1. Merge #94 (verify CI green first) in the web UI, then:
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main

# 2. Re-stage from the now-merged origin/main (should be byte-identical to what's already staged,
#    since #94 only touched packaging/smoke/*.ps1 + docs/ -- no rebuild needed):
Copy-Item packaging\dist\reclaim-setup.exe,packaging\dist\reclaim-setup.exe.sha256,`
  packaging\dist\reclaim-setup.exe.buildsha,packaging\smoke\ac3_login_diagnostic.ps1 `
  C:\Users\Public\reclaim_ac3\ -Force

# 3. Then the actual trip under ReclaimSmokeTest -- see the two-item human list below.
```

## The two irreducible human steps for the trip

1. **First-run screen** — now genuinely observable for the first time this engagement (BD3's
   fix, confirmed working under gaura's own account). Note headline/body copy, Simple-vs-Advanced
   landing view, screenshot if convenient.
2. **Toast count from Step 10** specifically (not Steps 1-4, which structurally cannot fire one,
   per BD2) — Step 10 will only reach `reason=would_notify` if `ReclaimSmokeTest`'s fresh-install
   `config.toml` actually has `[notifications] enabled = true` and real disk usage crosses
   `disk_threshold_percent` (a fresh install's `config.default.toml` DOES ship this section
   enabled at 50% — unlike gaura's stale dev config — so this should fire for real on the actual
   trip without any manual config edit). Watch for a real toast and report whether it rendered.

Nothing else belongs on this list — Task Scheduler registration (BE1, live-verified for one
account, structurally guaranteed for any second), the CWD-independence fix, the Snooze protocol
handler, AE1's teeth-proof, the free-space-delta measurement, and 8.3 short-name detection are all
automated and passing for real as of this checkpoint.

## Test account state

`ReclaimSmokeTest` — last used for the real trip on 2026-08-25/26 (the run BD1-BD5 were found
against). Not re-touched this session (all verification since was under gaura's own account).
**U6 (delete the account) stays deferred** until the next real trip against the merged state
comes back clean, per the two-item list above.

## Open-items list (flat, everything outstanding — BF6)

**Blocking the next real trip:**
- Merge #94 (CI status unconfirmed as of this checkpoint — check before merging).
- Re-stage from merged `origin/main` (step 2 above, should be a no-op).
- Run the actual trip under `ReclaimSmokeTest` — this is what finally collects BE1's true
  cross-account live data point (BF1's assertions are wired and ready but have only run under a
  single-account upgrade scenario so far, not a genuine second-account install).

**Disclosed, not re-opened (won't be fixed without a specific reason to revisit):**
- The machine-wide Task Scheduler namespace collision's underlying cause (task names are global)
  is not restructured — BE1 works around it (per-account naming), doesn't eliminate the shared
  namespace itself. A UI surface for `task_registration_diagnostic.log` (BD1) is not built.
- BD4's specific scan-trigger instance from the 2026-08-25/26 trip: origin/token permanently
  unrecoverable (evidence rotated out before capture). The scan-trigger mechanism itself (AN1/AZ4/
  this instance) has never been root-caused across three occurrences — fixed pragmatically each
  time (cancel-then-proceed), by design, not chased further.
- BD5's 52.11% secondary free-space gap: BELIEVED (not VERIFIED) to correlate with a concurrent
  scan; direct evidence gone. Step 7 waiting for scan-cancellation to fully settle before baseline
  sampling — noted, not implemented.
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
- BD5's scan-during-Step-7-measurement-window correlation (above).
- BE1's structural "any second account is collision-safe by construction" claim — now backed by
  one additional real data point (this session's gaura-account re-registration/migration) but
  still not a live cross-account (two DIFFERENT accounts, same machine, same moment) confirmation.

**Deliberately deferred:**
- U6 (`ReclaimSmokeTest` account deletion) — until the next clean trip.
- Task Scheduler subfolder organization for R5 — considered and rejected in favor of per-account
  naming (BE1), not merely postponed.
