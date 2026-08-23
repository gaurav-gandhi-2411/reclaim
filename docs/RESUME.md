# Resume checkpoint — 2026-08-24

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
This file has been rewritten twice today as state changed — trust this version, not memory of an
earlier one. Always `git fetch origin` and compare SHAs before trusting ANY checkpoint doc's
claims about `main`, including this one (rule 118a) — this exact file was already caught stale
twice today.

## Where we are

`origin/main` is at `0d9e3e9253e0d61280be7774833526fb5c972abd` (PRs #69/#70/#71 merged). Confirm:
`git fetch origin --quiet && git rev-parse origin/main`.

**Rebuild #5 is done.** SHA-256 `79cae649b6321c67ce71e48f52f1d56c1ad5b491e1b5baba434bca16fccfc2b7`,
built from `0d9e3e9253e0d61280be7774833526fb5c972abd` (confirmed matching `origin/main`). Staged at
`C:\Users\Public\reclaim_ac3\reclaim-setup.exe` with its `.sha256`/`.buildsha` sidecars and the
current trip script. Installed for real under gaura's own account.

**PR #70's fix (the scan-root-scope P0) is now confirmed against the real frozen binary, three
independent ways**: (1) source-level reproduction, (2) a manual HTTP round-trip against the frozen
server, (3) the trip script's own Step 6 AE1 teeth-proof, run twice, second time clean
(`[PASS] File outside user scope was NOT touched`). This closes the "not yet verified against the
frozen binary" gap the audit doc flagged earlier today.

**Five more draft PRs opened and pushed today, none merged yet, none self-merged (standing
policy):**

- **PR #72** — `docs/ar1-ar2-scan-scope-findings`. AR1 (what the AE1 proof actually established
  vs. how it was read) + AR2 (sibling sweep for the same defect class — two real instances, both
  already fixed in #70/#71; everything else checked and ruled out with file:line + reasoning).
- **PR #73** — `fix/build-provenance-sidecar`. `build_installer.ps1` now writes a `.buildsha`
  sidecar recording the exact source commit — the producing half of the fix below.
- **PR #63** — `docs/ac3-login-diagnostic` (existing, updated twice today). The trip script's Step
  -2 now refuses to install a build whose `.buildsha` doesn't match current `origin/main` exactly
  (the consuming half of #73's fix) — closes the actual gap that let 4 stale trip runs proceed
  yesterday. **Found and fixed a real bug in this same guard while dry-running it**: the default
  `-RepoPath` resolved wrong when run from the script's own real deployment location
  (`C:\Users\Public\reclaim_ac3\`, not the repo checkout) — fixed, re-verified live, freshness
  check now shows `[OK]`.
- **PR #74** — `fix/smoke-probe-timeout-too-short`. The frozen smoke suite's `http_probe.py` had
  15s/30s timeouts, an order of magnitude too short for a real dev machine's persisted index (same
  defect class already fixed once in the trip script — never checked for a sibling here). Raised
  to 180s. **Disclosed as a partial fix**: the real issue is a shared, ever-growing persisted index
  across repeated suite runs on the same machine (frozen builds' `data_root()` ignores the suite's
  own scratch dir by design), for which no fixed timeout is fully durable — a real fix needs an
  isolated `--db` override, out of scope for this pass.
- **PR #75** — `docs/ar5-frozen-verification`. Records AR4 (rebuild #5, the scale-nightly flake
  investigation, a `Get-FileHash`-in-nested-process build-script quirk worked around by hand) and
  AR5 (the frozen-suite/trip-script re-verification above) in the audit doc.

**Merge order doesn't matter functionally** (no PR depends on another merging first), but #63/#73
are a matched producer/consumer pair — merge together or #63 alone is inert until #73 lands too.

## Frozen smoke suite results (this session, against rebuild #5)

8 PASS / 1 FAIL / 4 BLOCKED (first run), 7 PASS / 1 FAIL / 5 BLOCKED (second run, back-to-back —
one PASS flipped to BLOCKED on notification debounce state from the first run, a known test-
harness-contamination pattern, not a regression). The 4-5 BLOCKED are all pre-existing, documented
gaps (3b visual toast, 5 task-scheduler elevation, 1d hardlink-probe seed data, 1e 8.3 short-name
username length). The 1 FAIL (`7-scan-apply-undo`) is the shared-growing-index timeout above —
**the underlying product behavior was directly proven correct** by manually re-running the same
cycle with a generous timeout: a real file was vaulted then restored, byte-identical, zero data
loss. See `docs/AUDIT-2026-08.md`'s AR5 section for full detail.

## Trip-script dry run (this session, from gaura's own account)

Completed, twice. Second run (after the `-RepoPath` fix): freshness check `[OK]`, Step 6 AE1
`[PASS]`. 3 ABORTs, all pre-existing/environmental (Task Scheduler not registered — non-elevated
install; two apply/scan calls that didn't finish within their poll windows — the same shared-index
cost as check 7 above). 0 FAIL, 0 SKIPPED, every non-PASS line had a stated reason.

## Exact resume sequence

```powershell
# 1. Merge the 5 open PRs (63, 72, 73, 74, 75) in whatever order/batching you prefer -- none are
#    self-merged, none block each other functionally.

# 2. If you want a rebuild #6 reflecting any NEW commits merged after 0d9e3e9 (not required --
#    rebuild #5 already reflects everything through PR #71, and PRs #63/#72/#73/#74/#75 are
#    docs/packaging-only, no product source changed):
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # compare to origin/main
nohup powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_installer.ps1 `
  > packaging/build/nuitka_build_console7.log 2>&1 &
# Launch from a PLAIN terminal, not nested inside another PowerShell process -- AR4 found
# Get-FileHash unresolvable in that one specific invocation shape this session.

# 3. AO5 -- the actual trip against ReclaimSmokeTest. Everything AO4 (frozen suite + dry run)
#    could verify without spending a login has been done -- see above. Only the two irreducible
#    human steps remain.
```

## The two irreducible human steps for the trip (everything else is scripted)

1. Watch for toasts across Triggers 1-4 in the trip script, note which (if any) rendered.
2. Describe the first-run screen (headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient) — the data-level acknowledgment is already confirmed real from an
   earlier trip; the visual description was never given.

## Test account state

`ReclaimSmokeTest` exists, enabled, `LastLogon` 2026-08-23 13:12:10. Credential believed still
valid based on that successful logon — not independently re-tested this session to avoid side
effects. **U6 (delete the account) is still deliberately deferred** until a real trip against
ReclaimSmokeTest itself comes back clean — do not delete it.

## Open-items list

**Blocking the actual AC3 trip:** nothing left except the two human steps above and merging the 5
open PRs (which don't block the trip functionally, only the audit trail / packaging-tooling being
in its final state — the artifact and trip script already work against `origin/main` as-is).

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger was never
conclusively identified; check 1b's PASS carries a caveat — real evidence, but from a harness
that's already been wrong more than once this engagement; check 7's shared-growing-index timeout
issue (above) is a real, disclosed apparatus gap, not fully closed.

**Deliberately deferred:** U6 (above); S5 (first-60-seconds report — blocked on human step 2).
