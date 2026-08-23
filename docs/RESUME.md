# Resume checkpoint — 2026-08-23 (refreshed)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`
(committed on `main` as of PR #68 — no longer draft, already merged).

**This file supersedes its own earlier version from earlier today.** That version (still visible
in this same PR's history) claimed `main` was at `34b1b9b` and that rebuild #5 had never been
attempted — both were already stale by the time they were written down: PR #68 had merged minutes
earlier, and (per filesystem evidence found this session, not git history) rebuild #5 *was*
attempted once, and failed at the RAM preflight. Don't trust a checkpoint doc's claims about `main`
without an actual `git fetch` + SHA compare — see rule 118a.

## Where we are

`main` is at `182365ba46aba1879b467f659853ed65b63537c7` (PR #68, the audit-doc checkpoint, merged
2026-08-23T12:12:21Z). Confirmed via `git fetch origin` + `git rev-parse origin/main`, not assumed.

**Two new draft PRs opened this session, both blocking everything below until merged:**

- **PR #70** — `fix/apply-scan-root-scope-ttl-and-membership`. A real, live-reproduced P0 found
  during this session's own ground-truth verification (not part of the original instructions):
  `resolve_allowed_apply_roots()` treated a confirmed outside-home scan's root as blanket,
  indefinite apply authorization for the ENTIRE subtree — including content created *after* the
  scan, never seen by it. Reproduced from source in isolation (real confirm-intent token, no
  test-only bypass): a never-scanned file under a confirmed root was deleted. **This directly
  contradicts this doc's own earlier "AE1 correctly skips outside-home paths" claim** — that
  claim only exercised `scan_status.root is None`, which is not the case a long-lived, multiply-
  reused server process is actually in. Fixed with two independent, teeth-proofed checks: (a) an
  outside-home path must already be present in the confirming scan's persisted index, not merely
  nested under its root; (b) the root's authorization expires 30 minutes after that scan
  completes. `scripts/verify.py` clean: 1202 passed (+2), 94.49% coverage.
- **PR #71** — `fix/scan-confirmation-token-ttl`. Item 7 (below), now fixed: confirm-intent
  tokens are `dict[str, float]` (token → mint time), single-use either way, rejected past 60s,
  swept on every mint so an unconsumed token doesn't accumulate forever. `scripts/verify.py`
  clean: 1203 passed (+3), all safety-critical floors met.

Neither self-merged, per this repo's standing policy for auth/security-adjacent changes. **Human
merge required before rebuild #5 or the AC3 trip proceed** — everything downstream depends on
`origin/main` actually containing both fixes.

## What actually happened to the "blocked on RAM" state

Not blocked anymore. Free RAM checked live this session: 21.69GB (floor is 8GB). One rebuild #5
attempt already exists on disk (`packaging/build/nuitka_build_console5.log`) and failed at the
preflight with 5.2GB free — consistent with the earlier session's `llama-server` blocker. That
attempt predates PRs #70/#71 anyway, so it wouldn't have been a valid build regardless of RAM.

## AC3 trip: already run four times today, against the STALE rebuild #4 artifact — before either fix above existed

`C:\Users\Public\reclaim_ac3\` holds 4 trip run logs from today (13:14–14:18), all against
`reclaim-setup.exe` SHA-256 `3452ca01...` (rebuild #4, from `4c352197...`, predates #65/#66/#67
*and* predates #70/#71). **This is exactly the "do not run the trip against these as-is" case the
prior version of this doc warned about** — it happened anyway (not by this session; found as
pre-existing filesystem state at session start). 3 of the 4 runs are inconclusive (CSRF omission →
403, a timeout, an unreachable server — apparatus bugs, matching AN3's established pattern). The
4th (`ac3_run_20260823_141414.txt`) is the one that surfaced the PR #70 finding above.

**Do not treat any of these 4 runs as a completed AC3 trip.** Rebuild #5 (with #70 and #71 both
merged) is still required before a real trip.

## Exact resume sequence

```powershell
# 1. Confirm both PRs are merged
gh pr view 70 --json state   # must be MERGED
gh pr view 71 --json state   # must be MERGED

# 2. Confirm RAM is actually free (not just believed free — verify)
[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB, 2)  # need >= 8

# 3. Confirm HEAD matches origin/main before building -- fetch first, always (rule 118a)
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main, must contain both #70 and #71

# 4. Rebuild #5
nohup powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_installer.ps1 `
  > packaging/build/nuitka_build_console6.log 2>&1 &
# wait ~35-40 min; confirm "Successful compile" in the log; record SHA-256 from
# packaging/dist/reclaim-setup.exe.sha256; re-confirm it was built from the SHA in step 3.

# 5. AO4 — from your own account, before spending a login:
#    - Frozen suite (needs pwsh, at C:\Program Files\PowerShell\7-preview\pwsh.exe, not on PATH):
&"C:\Program Files\PowerShell\7-preview\pwsh.exe" -NoProfile -File packaging/smoke/run_frozen_smoke_suite.ps1 `
  -InstallPath <fresh dist path>
#    - Full dry run of the trip script end-to-end (real install, real dashboard, all 9 steps),
#      report every step PASS/FAIL/ABORT/SKIPPED, confirm zero silent skips.
#    - Re-verify PR #70's fix specifically against the frozen binary: the AE1 teeth-proof (Step 6)
#      must now show either a clean skip (never-scanned path) or, if testing the legitimate case,
#      a real vault/recycle_bin success for an ACTUALLY-scanned outside-home path. A repeat of the
#      141414 log's result (silent success, skip_reason: null, on a never-scanned path) means the
#      source fix didn't survive freezing -- treat that as a new P0, not a retry-blind situation.

# 6. AO5 — only after AO4 is clean: the actual trip against ReclaimSmokeTest. Copy the fresh
#    installer + current ac3_login_diagnostic.ps1 into C:\Users\Public\reclaim_ac3\ first.
```

## Item 7 — FIXED (PR #71, pending merge)

Was: full-drive-scan confirmation tokens single-use but no expiry. Now: 60s TTL, pruned on mint.
Tests prove expiry at both consumption sites and that pruning actually removes (not just rejects)
a stale token.

## Test account state

`ReclaimSmokeTest` exists, enabled, `LastLogon` 2026-08-23 13:12:10 (matches today's trip window).
Credential believed still valid based on that successful logon — not independently re-tested this
session to avoid side effects. **U6 (delete the account) is still deliberately deferred** until a
real trip (post rebuild #5, on a clean artifact) comes back clean — do not delete it.

## The two irreducible human steps for the trip (everything else is scripted)

1. Watch for toasts across Triggers 1-4 in the trip script, note which (if any) rendered.
2. Describe the first-run screen (headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient) — the data-level acknowledgment is already confirmed real from an
   earlier trip; the visual description was never given.

## Open-items list

**Blocking the trip:** PR #70 + PR #71 merge, then rebuild #5 (above).

**New this session, not yet in a trip:** PR #70's fix needs to be re-proven against the frozen
binary specifically (see step 5's note above) — the source-level fix is proven, but this whole
engagement's headline lesson is that source-level proof isn't sufficient on its own.

**Automated in the trip script, never yet run against the real ReclaimSmokeTest account (on a
non-stale artifact):** AE1 teeth-proof against ReclaimSmokeTest's real persisted index; S2/U4
app-reported-vs-measured free-space delta; check 1e's fix-effect (real `temp_and_browser_caches`
candidate count under the 8.3-aliased path — the precondition itself is already confirmed real).

**Requires you:** the two items above, plus merging PR #70 and PR #71.

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger was never
conclusively identified; check 1b's PASS carries a caveat — real evidence, but from a harness
that's already been wrong once this session.

**Deliberately deferred:** U6 (above); S5 (first-60-seconds report — blocked on human step 2).
