# Resume checkpoint — 2026-08-23

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`
(1225 lines, committed but PR #68 still draft — merge or read off that branch).

## Where we are

`main` is at `34b1b9bed89b3ba93c03e178ab167b5bb14be77d`. Merged today: PR #65 (full-drive scan
requires a session-minted confirmation token, not just CSRF — a real P0 found live), PR #66
(closed the same gap in `POST /api/scan` + MCP's `scan` tool), PR #67 (adversarial tests proving
MCP `delete`'s scan_id/selection_hash are integrity checks, not capability tokens — no bypass
found there). Full `scripts/verify.py` clean on all three.

**Rebuild #5 (off this exact `main` SHA) never started.** Blocked on RAM: the build script
refuses below an 8GB-free floor (a real, working guard, not a bug — see its own header comments
for why). Last known blocker: `llama-server` process still running (~5GB), user's stop attempt
didn't take, session closed before resolving it.

## Exact resume sequence

```powershell
# 1. Confirm RAM is actually free (not just believed free — verify)
[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB, 2)  # need >= 8

# 2. Confirm HEAD matches origin/main before building
git fetch origin --quiet; git checkout main; git pull --ff-only origin main
git rev-parse HEAD  # must equal origin/main

# 3. Rebuild #5
nohup powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_installer.ps1 `
  > packaging/build/nuitka_build_console5.log 2>&1 &
# wait ~35-40 min; confirm "Successful compile" in the log; record SHA-256 from
# packaging/dist/reclaim-setup.exe.sha256; re-confirm it was built from the SHA in step 2.

# 4. AO4 — from your own account, before spending a login:
#    - Frozen suite (needs pwsh, at C:\Program Files\PowerShell\7-preview\pwsh.exe, not on PATH):
&"C:\Program Files\PowerShell\7-preview\pwsh.exe" -NoProfile -File packaging/smoke/run_frozen_smoke_suite.ps1 `
  -InstallPath <fresh dist path>
#    - Full dry run of the trip script end-to-end (real install, real dashboard, all 9 steps),
#      report every step PASS/FAIL/ABORT/SKIPPED, confirm zero silent skips.

# 5. AO5 — only after AO4 is clean: the actual trip against ReclaimSmokeTest.
```

## Staged artifacts — STALE, must refresh before the trip

`C:\Users\Public\reclaim_ac3\reclaim-setup.exe` and `ac3_login_diagnostic.ps1` currently
correspond to **rebuild #4** (SHA-256 `3452ca017e339e92456955dd0db4501f630649bb3c41640e575ef980e34a378f`,
built from `4c3521974865c444a4cdf23a01f0703b56f1f027`) — **predates #65/#66/#67**. Do not run the
trip against these as-is. After rebuild #5 completes, copy the fresh installer and the current
`packaging/smoke/ac3_login_diagnostic.ps1` over both files in that directory.

## Item 7 — undecided, real gap, not blocking

Full-drive-scan confirmation tokens are single-use but have no expiry. Closes replay-of-an-
already-consumed-token; does NOT close a delayed first use of a token minted but never
immediately consumed. Fix (short TTL, e.g. 60s) not implemented. Decide before or after the trip.

## Test account state

`ReclaimSmokeTest` exists, enabled, credential re-stored and confirmed working this session.
**U6 (delete the account) is deliberately deferred** until a trip comes back clean — do not
delete it.

## The two irreducible human steps for the trip (everything else is scripted)

1. Watch for toasts across Triggers 1-4 in the trip script, note which (if any) rendered.
2. Describe the first-run screen (headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient) — the data-level acknowledgment is already confirmed real from an
   earlier trip; the visual description was never given.

## AP5 open-items list, verbatim (as of last session)

**Blocking the trip:** rebuild #5 (above).

**Automated in the trip script, never yet run against the real ReclaimSmokeTest account:**
AE1 teeth-proof against ReclaimSmokeTest's real persisted index; S2/U4 app-reported-vs-measured
free-space delta; check 1e's fix-effect (real `temp_and_browser_caches` candidate count under
the 8.3-aliased path — the precondition itself is already confirmed real).

**Requires you:** the two items above.

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger was never
conclusively identified (item 7, above, is the closest thing to a mechanism); check 1b's PASS
carries a caveat — real evidence, but from a harness that's already been wrong once this session.

**Deliberately deferred:** U6 (above); S5 (first-60-seconds report — blocked on human step 2).
