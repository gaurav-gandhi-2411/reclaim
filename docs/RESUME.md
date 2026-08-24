# Resume checkpoint — 2026-08-24

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.
This file has been rewritten several times today as state changed — trust this version, not
memory of an earlier one. Always `git fetch origin` and compare SHAs before trusting ANY
checkpoint doc's claims about `main`, including this one (rule 118a).

## Where we are

`origin/main` is at `92d633eb64a3938decf8df6e425f3677c40146e8` (PRs #69/#70/#71/#72/#63/#74/#73
merged, in that order). Confirm: `git fetch origin --quiet && git rev-parse origin/main`.

**Only one PR still open: #75** (`docs/ar5-frozen-verification`) — pure documentation
(`docs/AUDIT-2026-08.md` + this file), zero code. Records AR4 (rebuild #5) + AR5 (frozen-artifact
re-verification) + AS1-AS4 (post-merge staging refresh, rebuild-#6 assessment, index sizing,
re-verification). Not self-merged, standing policy. Nothing downstream depends on it merging —
it's the write-up of work already done and verified, not a gate.

**Rebuild #5 is done and remains valid — no rebuild #6 needed.** SHA-256
`79cae649b6321c67ce71e48f52f1d56c1ad5b491e1b5baba434bca16fccfc2b7`, built from
`0d9e3e9253e0d61280be7774833526fb5c972abd`. Verified directly (AS2): `git diff --stat
0d9e3e9253e0d61280be7774833526fb5c972abd origin/main -- src/reclaim/` is empty — every PR merged
since (#72 docs-only, #73/#63/#74 packaging-tooling-only) touches zero product source. The
installer's `.buildsha` sidecar now trails `origin/main`'s literal tip (since #63/#72/#73/#74
merged on top of the source commit it was built from), so the trip script's own freshness guard
correctly refuses without `-AllowStaleBuild` — that's the guard working as designed on an
artifact confirmed still valid, not a real staleness problem. Staged at
`C:\Users\Public\reclaim_ac3\reclaim-setup.exe` with current `.sha256`/`.buildsha` sidecars and
the current trip script (byte-identical to what's on `main`, reverified AS1).

**PR #70's fix (the scan-root-scope P0) is confirmed against the real frozen binary, four
independent ways** across this session: source-level reproduction, a manual HTTP round-trip
against the frozen server, and two separate trip-script Step 6 runs (`[PASS] File outside user
scope was NOT touched`). Closes the "not yet verified against the frozen binary" gap this
engagement's own audit doc flagged as its core open risk.

## Persisted-index growth — sized, not fixed (AS3, full detail in the audit doc)

The account's real index (`reclaim_index.sqlite3`) is now **1.02GB / 1,084,134 rows** (grew from
~985MB earlier this session, all from this session's own repeated verification cycles).
`GET /api/candidates` against it: **225.8s**, already past PR #74's newly-raised 180s timeout.
Isolation (`--db`/`--vault-dir`/etc. — `reclaim serve` already supports all of them) would be
cheap to add to the smoke suite specifically, but was **not implemented**, per instruction — sized
and documented only. **This is also why the trip script's own Steps 7/8 ABORT on this account**:
the trip deliberately exercises the REAL shared production data path (not a fixture), so it pays
the same growing cost by design — isolating the smoke suite wouldn't change this for the trip
itself. Not a regression, not new; a standing characteristic of testing repeatedly on one real,
long-used dev machine.

## Frozen smoke suite + trip-script dry run — re-verified post-merge (AS4)

Both re-run against the refreshed staging after #63/#72/#73/#74 merged. Frozen suite: 7 PASS / 1
FAIL (check 7, the index-growth timeout above — not a regression) / 5 BLOCKED (pre-existing,
documented). Trip script: ABORTs correctly without `-AllowStaleBuild` (freshness guard working as
designed against a source-SHA-trailing-but-content-valid artifact, see above); with
`-AllowStaleBuild` (justified — AS2 verified no product code changed), completes with Step 6's
AE1 teeth-proof `[PASS]` again, 3 ABORTs (Task Scheduler unregistered, the two index-growth
timeouts), 0 FAIL, 1 WARNING (the expected override notice). No new regressions anywhere in this
pass.

## Test account state

`ReclaimSmokeTest` exists, enabled, `LastLogon` 2026-08-23 13:12:10. Credential believed still
valid based on that successful logon — not independently re-tested this session to avoid side
effects. **U6 (delete the account) is still deliberately deferred** until a real trip against
ReclaimSmokeTest itself comes back clean — do not delete it.

## Everything that could be verified without spending a login has been. What's left is exactly two things.

1. **Watch for toasts across Triggers 1-4** in the trip script (`ac3_login_diagnostic.ps1`,
   already staged and current at `C:\Users\Public\reclaim_ac3\`) — note which, if any, actually
   rendered on screen. The script fires all four triggers automatically and captures the
   before/after `notification_state.json` state either way; only the human-visible half (did a
   Windows toast actually appear) needs your eyes.
2. **Describe the first-run screen** — headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient (Win+Shift+S). The script already confirms the data-level
   acknowledgment fires correctly; only the visual description has never been captured.

**To run the actual trip against `ReclaimSmokeTest`:**

```powershell
# Log in as ReclaimSmokeTest (Fast User Switching — Win+L or Start menu -> account icon ->
# "Switch User", NOT "Sign out", to keep this session alive concurrently), then from that
# session:
pwsh -NoProfile -File C:\Users\Public\reclaim_ac3\ac3_login_diagnostic.ps1 -AllowStaleBuild
# -AllowStaleBuild is required (see "Where we are" above -- the artifact is source-verified
# current, just source-SHA-trailing origin/main's literal tip). ReclaimSmokeTest cannot reach
# gaura's repo checkout to verify freshness itself either way (documented, expected -- see the
# script's own freshness-check error message if you omit the flag).
```

Everything else in the trip script's 9 steps is already automated and has been proven clean from
gaura's own account this session (AS4). Report the two human observations above and that's the
whole trip.

## Open-items list

**Blocking the actual AC3 trip:** nothing except the two human steps above.

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger was never
conclusively identified; check 1b's PASS carries a caveat — real evidence, but from a harness
that's already been wrong more than once this engagement; the persisted-index growth issue above
is real and disclosed, not fully closed (sizing only, per instruction).

**Deliberately deferred:** U6 (above); S5 (first-60-seconds report — blocked on human step 2).
