# Resume checkpoint — 2026-08-26 (rebuild #9 dry-run clean, stale-server defect fixed, one investigation open)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
Always `git fetch origin` + `gh pr list` and re-check `C:\Users\Public\reclaim_ac3\` for what has
actually run before trusting any claim below, including this one (rule 118a) — this file has gone
stale mid-engagement multiple times already this week.

## Where we are — the real 130328 trip's Steps 6/7/8 were invalid (stale server), now fixed and re-confirmed clean

**Merged this session**: BD1-BD5 (#88-#90), BE1/BE2/BE4 (#91-#93), BA1 (#87), BF1/BF2/BF3-BF5
(#94), a RESUME checkpoint (#95), BH1-BH7 (#96, #97 — BH5's notifications toggle). **One more PR
about to be drafted**: BI1-BI3 (see below) — trip-script fixes + audit-doc corrections found while
investigating the real `ReclaimSmokeTest` trip's results.

**The real trip (`ac3_run_20260826_130328.txt`) had a serious, now-understood problem.** A stale
dashboard server (never actually killed by an earlier trip's own install step) silently served
Steps 6, 7, and 8's HTTP-based results — not the freshly-installed rebuild #8 binary. This was
caught via a single piece of direct evidence: the "BEFORE launch" `GET /api/first-run` probe got a
real response *before* the script had launched anything. Full root-cause, retroactive sweep of
every historical trip (only 2 other instances found, both already-disclaimed gaura-account trips
from before this session — no other trusted result is retroactively invalidated), and the fix
(poll-verified kill + pre-launch remediation + post-launch PID confirmation) are in
`docs/AUDIT-2026-08.md`'s BH2/BI1/BI2 sections.

**Also found this pass**: the toast call succeeding internally (Step 10, confirmed via
`notification_state.json` updating) but never rendering on screen — the user directly observed
zero toasts during the trip. Investigated (BI3): a real apparatus gap (the per-app-notification-
permission check only verified a registry key's mere existence, never its actual `Enabled`/
`PeriodicNotificationCount` values) is now fixed, and live-tested on gaura's own account (a real
toast increment 17→18 confirmed the OS pipeline itself works on this machine in general). **Root
cause on `ReclaimSmokeTest` specifically is NOT yet determined** — the next trip's Step 10 will
capture the real evidence needed, IF it reaches `reason=would_notify` (see the human list below).

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
# 1. Merge the BI1-BI3 PR (draft, about to be created) in the web UI, then:
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main

# 2. Re-stage from the merged origin/main (no rebuild needed -- that PR is trip-script + docs
#    only, zero src/ changes; rebuild #9 already reflects everything merged so far):
Copy-Item packaging\dist\reclaim-setup.exe,packaging\dist\reclaim-setup.exe.sha256,`
  packaging\dist\reclaim-setup.exe.buildsha,packaging\smoke\ac3_login_diagnostic.ps1 `
  C:\Users\Public\reclaim_ac3\ -Force

# 3. Then the actual trip under ReclaimSmokeTest -- see the two-item human list below.
```

## The two irreducible human steps for the trip

1. **First-run overlay** — should finally appear for real this time. Last trip: none appeared,
   went straight to Simple-mode dashboard — now understood as BH2's stale server answering
   `{"acknowledged":true}` from its own already-consumed state, not a BD3 failure (BD3's own
   marker-deletion fix is confirmed working correctly under gaura's account this session,
   `{"acknowledged":false}` both before and after). With BH2's fresh-server guarantee now active,
   there is no known second storage location left to explain a recurrence — if the overlay still
   doesn't appear on the next trip, that would be a new, real finding, not the same one recurring.
2. **Toast from Step 10** — a fresh `ReclaimSmokeTest` install's `config.default.toml` ships
   `[notifications] enabled = false` (BH5's real, deliberately-kept default) — Step 10 will
   correctly SKIP (`reason=disabled`) unless notifications are explicitly turned on first, either
   via the new Settings-tab toggle (BH5) or by hand-editing `config.toml`. **If you want Step 10
   to actually attempt firing a toast this trip, turn the toggle on before running the script** (or
   accept a SKIP and treat this as a separate, later verification). If it does reach
   `reason=would_notify`, watch for a real toast and report whether it rendered — the script's own
   new AUMID diagnostic (BI3) will independently report whether the OS notification pipeline
   queued it, which narrows the cause either way regardless of what you personally observe.

Nothing else belongs on this list — Task Scheduler registration (BE1, now live-verified for two
real accounts, not just structurally argued — see BF1/BH1), the CWD-independence fix, the Snooze
protocol handler, AE1's teeth-proof, the free-space-delta measurement, and 8.3 short-name detection
are all automated, confirmed against a positively-verified-fresh server this session, and passing
for real.

## Test account state

`ReclaimSmokeTest` — last used for the real trip on 2026-08-26 (`130328`, the run BH1-BH7 were
found against). Not re-touched since (all verification this pass was under gaura's own account).
**U6 (delete the account) stays deferred** until the next real trip against the merged state comes
back clean, per the two-item list above.

## Open-items list (flat, everything outstanding)

**Blocking the next real trip:**
- Draft and merge the BI1-BI3 PR (trip-script + audit-doc fixes from this investigation).
- Re-stage from merged `origin/main` (step 2 above, no rebuild needed).
- Run the actual trip under `ReclaimSmokeTest` — this is what determines BI3's real root cause
  (does `ReclaimSmokeTest`'s own AUMID key show `Enabled=0`? does `PeriodicNotificationCount`
  increment there?) and gives BD3/BH2's first-run fix its first real cross-account test.

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
- BI3's root cause on `ReclaimSmokeTest` specifically — genuinely unknown pending the next trip's
  real data, not guessed at.

**Deliberately deferred:**
- U6 (`ReclaimSmokeTest` account deletion) — until the next clean trip.
- Task Scheduler subfolder organization for R5 — considered and rejected in favor of per-account
  naming (BE1), not merely postponed.
- `disk_threshold_percent` Settings-tab editing (BH5) — stays config.toml-only by design, same
  scope as every other category's non-`enabled` field.
