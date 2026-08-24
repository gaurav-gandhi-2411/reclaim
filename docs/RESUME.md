# Resume checkpoint — 2026-08-25 (trip-ready)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
Always `git fetch origin` + `gh pr list` and re-check `C:\Users\Public\reclaim_ac3\` for what has
actually run before trusting any claim below, including this one (rule 118a) — this file has
gone stale mid-engagement more than once already.

## Where we are — the dry run is genuinely clean for the first time this engagement

Rebuild #7 (SHA-256 `d34b55f340a57e3df1ab48eaf0f3a0efdc0878f2769943508d728e6c67d48505`, built from
`e63ca72` = `origin/main`'s tip at build time) plus a series of trip-script/smoke-suite fixes
(PRs #82–#86, three still open as drafts pending your merge — see below) produced a full dry run
with **0 ABORT, 0 FAIL, 0 SKIPPED, 0 ERROR** — the frozen smoke suite separately: **10 PASS, 0
FAIL, 3 BLOCKED** (all three genuinely structural: toast visual confirmation needs a human,
hardlink probes need a dedicated fixture, 8.3 short-name needs a long-username account this
machine doesn't have).

**Open draft PRs, not yet merged — merge these before the actual trip:**
- **#85** — AY1 promoted to a headline audit-doc finding, plus the AZ4 addendum (second instance
  of AN1's unexplained-scan-trigger shape) and this file's own refresh.
- **#86** — every fix found while getting to a clean dry run (see below). CI green on both.

## What was fixed to get here (this session, in order found)

- **AY1**: the disk-space-check scheduled task had never actually registered, on any real
  install, since the feature shipped (PR #38, 2026-08-21) — an XML-encoding mismatch in
  `packaging/reclaim.iss` (`SaveStringToFile`'s `AnsiString` parameter silently narrowed the
  declared-`UTF-16` content to single-byte bytes). Fixed in PR #83 (merged). This session also
  found and cleared a *separate*, machine-local corruption of the exact task name on this dev box
  (11+ create/delete cycles had left it in an `Access is denied` state for both query and
  create) — confirmed gone before starting this refresh.
- **AZ2**: Step 7's measurement design was wrong, not just noisy — comparing app-reported bytes
  against whole-drive free space over a multi-second window on a machine under real concurrent
  use cannot resolve to 2%. Redesigned: primary assertion is now exact equality against the
  fixture's own known byte size; OS free-space delta is a secondary, drift-relative sanity check
  only. **Now PASSes cleanly**, real number: app-reported `10485760` bytes exactly matches the
  known fixture size.
- **AZ3**: Step 8's `POST /api/candidates/warm` 409 was PR #62's own single-flight guard working
  correctly (a warm-up genuinely was already in flight, most likely the dashboard browser tab
  Step -1 opens) — the bug was letting that 409 abort the whole step instead of polling the
  existing warm-up. Fixed.
- **AZ4 (four more, found chasing the dry run to genuinely clean)**:
  1. The frozen suite's own check 7 hit AR5's predicted timeout recurrence again (the shared,
     ever-growing index this machine's whole testing history writes to). Implemented the durable
     fix AR5/AY2 both disclosed as out of scope: `run_frozen_smoke_suite.ps1`'s server now gets
     an isolated `--db`/`--vault-dir`/`--manifest`/`--mode-log`/`--first-run-state`/`--log-path`,
     never touching the shared index again. Check 7 now PASSes deterministically.
  2. Step 7's fixture scan hit a 409 from a REAL, unrelated whole-home-directory scan already
     running (`origin: POST /api/scan/my-files`, confirmed a real button click via `app.js`, not
     a frontend auto-scan) — a second instance of AN1's "unexplained scan trigger" shape, fixed
     pragmatically (cancel-then-scan) per AN1's own precedent, not re-investigated to a root
     cause.
  3. Step -2's install hit Inno Setup exit code 5 on a second trip run in the same session — a
     leftover `reclaim.exe` process from the prior run held a file lock. `reclaim.iss`'s own
     `InitializeUninstall` already guards this for uninstall; the trip script's install step had
     no equivalent — added.
  4. Step 8's final `GET /api/candidates` call was the one remaining 30s timeout in the whole
     script (the warm-up cache being ready doesn't make serializing the real candidate list
     free) — raised to 600s, matching this script's own established convention everywhere else.

## Exact resume sequence

```powershell
# 1. Merge PRs #85 and #86 (both CI-green drafts) in the web UI, then:
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main

# 2. Re-stage the merged trip script + current artifact (SHA above still matches origin/main
#    unless something merges into src/ after this checkpoint -- if so, rebuild #8 first):
Copy-Item packaging\dist\reclaim-setup.exe,packaging\dist\reclaim-setup.exe.sha256,`
  packaging\dist\reclaim-setup.exe.buildsha,packaging\smoke\ac3_login_diagnostic.ps1 `
  C:\Users\Public\reclaim_ac3\ -Force

# 3. One more dry run to confirm the merged state is still clean (frozen suite + trip script),
#    same bar: 0 ABORT / 0 FAIL / 0 SKIPPED / 0 ERROR.

# 4. Then the actual trip -- see the two-item human list below. Nothing else on it.
```

## The two irreducible human steps for the trip (everything else is now scripted AND verified clean)

1. Watch for toasts across Triggers 1-4 in the trip script, note which (if any) rendered.
2. Describe the first-run screen (headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient) — never yet directly observed this engagement (every account
   session so far already shows `{"acknowledged": true}` by the time this step runs).

Nothing else belongs on this list — every other check (Task Scheduler registration and
delivery, the CWD-independence fix, the Snooze protocol handler, AE1's teeth-proof, the
free-space-delta measurement, the 8.3 short-name detection) is fully automated and, as of this
checkpoint, passing for real, not just avoiding a timeout.

## Test account state

`ReclaimSmokeTest` — not re-checked this session (no destructive/auth action taken). Last
confirmed (2026-08-23): exists, enabled. **U6 (delete the account) stays deferred** until the
actual trip (post-merge, post one more confirming dry run) comes back clean.

## Open-items list

**Blocking the actual trip:** merging PRs #85/#86, then one more confirming dry run against the
merged state (steps 1-3 above).

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger (AN1) and
this session's second instance of the same shape (AZ4 addendum) were both fixed without
identifying the exact trigger, by design — a third instance is the point to actually chase it
down, not another patch-around; every VERIFIED tag proven on one synthetic fixture rather than
the full branch structure of the code under test should be read as "proven for that
configuration" (AR1); the persisted-index growth this whole engagement caused is real and now
structurally addressed for the frozen suite specifically (isolated data root), not for the trip
script's own Steps 7/8 (which deliberately still walk the real shared account data, by design —
see AS3).

**Deliberately deferred:** U6 (above); S5 (first-60-seconds report — blocked on human step 2).
