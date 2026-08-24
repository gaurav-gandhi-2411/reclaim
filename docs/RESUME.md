# Resume checkpoint — 2026-08-24 (refreshed, supersedes this file's own prior version)

Written for a session with zero prior context. Full depth/history: `docs/AUDIT-2026-08.md`.

**The version of this file committed on `main` going into this refresh was itself already
stale** — it claimed "no rebuild #6 needed" (true when written, before PR #76 existed) and "only
PR #75 still open, everything else already verified clean, just the two human steps left." Both
were wrong by the time this refresh started: PR #76 (a real `src/reclaim/detectors.py` change)
had merged after that claim was written, and PRs #77/#78 merged after that. This is the exact
failure this file's own opening line warns about — always `git fetch origin` and compare SHAs
(and re-check `C:\Users\Public\reclaim_ac3\` for what's actually run) before trusting ANY
checkpoint doc's claims, including this one (rule 118a).

## Where we are

`main` is at `289de71` (PR #78, merged). Confirmed via `git fetch origin` + `git rev-parse
origin/main`, local `main` fast-forwarded to match, working tree clean. No PRs open.

**Everything that was open going into this refresh is now resolved:**
- PR #70 (AN5 apply-scope fix), #71 (item-7 TTL fix), #72 (AR1/AR2 docs), #73 (build provenance
  sidecar), #74 (smoke probe timeouts), #75 (AR4/AR5/AS1-AS4 docs) — all merged, as the prior
  checkpoint already recorded.
- PR #76 (AU1 — `temp_and_browser_caches` returned zero candidates when the scan root IS the temp
  directory itself; a real `src/reclaim/detectors.py` change) — merged. **This is what makes the
  prior checkpoint's "no rebuild #6 needed" claim stale** — AS2's "zero `src/reclaim/` diff"
  check was true against the commits that existed when it ran, not against what merged next.
- PR #77 (AT1/AT2 — Step 6/7/8 timeouts raised to 600s, Step 6's false-pass risk closed, Step 7's
  fixture redesigned to a real dev_artifacts/`__pycache__` shape, Step 8 uses the warm-up
  endpoint) — merged.
- PR #78 (AU2 — Step 7's own measurement code had two bugs: a reference-equal, live-backed
  `PSDriveInfo` object read twice made the OS-measured delta always read `0` regardless of what
  was actually freed; the log read the batch-level nominal method instead of the per-item
  resolved one) — merged. Full mechanism in `AUDIT-2026-08.md`'s AU2/AV1/AV2.

## Rebuild artifact — STALE, rebuild #6 required before any further trip run

`packaging/dist/reclaim-setup.exe`, SHA-256 `79cae649b6321c67ce71e48f52f1d56c1ad5b491e1b5baba434
bca16fccfc2b7`, built from source commit `0d9e3e9` (`reclaim-setup.exe.buildsha`, PR #73's
sidecar). `0d9e3e9` is confirmed **after** PR #70/#71 (`git merge-base --is-ancestor` plus local-
timezone commit timestamps — the build genuinely contains the AN5 apply-scope fix and the item-7
TTL fix) but **before** #76, #77, and #78. The staged copy at `C:\Users\Public\reclaim_ac3\` is
the same stale artifact. **Rebuild #6 off current `main` (`289de71`) is required** — this
directly contradicts the just-superseded checkpoint's "no rebuild #6 needed" claim; see above for
why that claim went stale.

## What the trip runs against the stale artifact actually showed (useful signal, not wasted)

8 full trip-script runs happened today against this stale artifact (`ac3_run_20260824_*.txt` in
the staging dir, 01:28 through 05:08) before this refresh, plus 4 more the prior day against an
even earlier artifact. Not wasted — they're still real evidence for what they actually tested:

- **Step 6 (AE1 teeth-proof)**: PASS, and genuinely real evidence — `mode_log.jsonl` timestamps
  independently confirm the server was in the exact state the check exercises when it ran.
  Nothing in #76/#77/#78 touches this code path, so this result should carry forward to rebuild
  #6, though it should still be re-run there rather than assumed (AR1's discipline: a proof is
  evidence for its tested configuration, not a standing guarantee).
- **Step 7 (S2/U4 free-space delta)**: reported FAIL every run before PR #78, "100% difference...
  NOT A SCRIPT BUG." **That framing was itself wrong** — see PR #78 above. Hand-recomputing the
  last pre-fix run's own logged numbers gives an exact 0.00% difference against the app-reported
  figure. **BELIEVED, not yet VERIFIED** — this is a recomputation from a log the unfixed script
  produced, not a clean run of the fixed script. Task 4's dry run must confirm this once,
  instrumented, before it's written up as VERIFIED (AV1); report the instrumented agreement %
  as its own explicit line when that happens (AV5).
- **Step 8 (8.3 short-name / temp-root-is-scan-root)**: reported FAIL (0 candidates) every run —
  correctly consistent with, and expected from, an artifact predating PR #76's fix. Not a new
  finding; just confirms the artifact needs replacing, which rebuild #6 does.

## Exact resume sequence

```powershell
# 1. Confirm origin/main and local main agree, work happens on the real tip
git fetch origin --quiet; git branch -f main origin/main; git checkout main
git rev-parse HEAD  # must equal origin/main

# 2. Task 2 (structural floor test): for each default-enabled category, a realistic seeded
#    fixture must produce a non-zero candidate count. Not a regression test for AU1/the 8.3 bug
#    specifically -- a floor that catches the NEXT silent-zero-yield mechanism before it ships.

# 3. Full scripts/verify.py run, clean, before rebuilding.

# 4. Confirm RAM free >= 8GB, then rebuild #6:
[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB, 2)
nohup powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_installer.ps1 `
  > packaging/build/nuitka_build_console7.log 2>&1 &
# wait for "Successful compile"; record SHA-256 + .buildsha; confirm buildsha == step 1's HEAD.

# 5. Frozen suite + full trip-script dry run against the fresh artifact. Report Step 7's
#    instrumented agreement % as its own explicit line (AV5) -- it's the gate on hands-on
#    testing. Bar: every step PASS, zero ABORT, zero silent skip, Steps 7 and 8 produce real
#    (not just non-timed-out) results.

# 6. Only after a clean dry run: trip instructions to GG, human list capped at exactly two items
#    (toasts across triggers, first-run screen description) unless something else genuinely
#    can't be automated -- justify if so.
```

## The two irreducible human steps for the trip (everything else is scripted)

1. Watch for toasts across Triggers 1-4 in the trip script, note which (if any) rendered.
2. Describe the first-run screen (headline/body copy, Simple-vs-Advanced landing view,
   screenshot if convenient) — never yet directly observed this engagement (every
   `ReclaimSmokeTest` session so far already shows `{"acknowledged": true}`).

## Persisted-index growth (AS3, carried forward — real, not fixed, not new)

The account's real index (`reclaim_index.sqlite3`) reached **1.02GB / 1,084,134 rows** as of the
prior checkpoint, driven up by this engagement's own repeated verification cycles. `GET
/api/candidates` against it measured **225.8s**, already past PR #74's 180s timeout at the time.
This is why the trip script's Steps 7/8 exercise long real scans rather than something cheaper —
the trip deliberately walks the real shared production data path, by design, not a fixture.
Isolation (`--db`/`--vault-dir`, which `reclaim serve` already supports) would be cheap to add to
the smoke suite specifically but hasn't been, per instruction (sized and documented, not
implemented). Not re-measured this refresh.

## Test account state

`ReclaimSmokeTest` — not re-checked this refresh (no destructive/auth action taken). Last
confirmed (2026-08-23): exists, enabled, `LastLogon` matching that day's trip window. **U6
(delete the account) stays deferred** until a real trip against a fresh (rebuild #6) artifact
comes back clean.

## Open-items list

**Blocking the trip:** rebuild #6 (blocked on nothing now — every prerequisite PR is merged; RAM
needs a live check; task 2's floor test and a `scripts/verify.py` run should land first per the
resume sequence above).

**Not yet done:** task 2 (structural floor test), task 4's dry run (needed to promote AU2's Step 7
finding from BELIEVED to VERIFIED with an instrumented figure), the human-required list
finalization, S4 (this doc's own final state), S5 (first-60-seconds report, blocked on human step
2), U6 (account cleanup, deferred per above).

**Disclosed, not re-opened:** the original full-drive-scan incident's exact trigger was never
conclusively identified (AN1); check 1b's PASS carries a caveat (AN3); every VERIFIED tag proven
on one synthetic fixture rather than the full branch structure of the code under test should be
read as "proven for that configuration," not "proven in general" (AR1) — the named list of such
entries is in `AUDIT-2026-08.md`'s AR1 section, not repeated here; the persisted-index growth
above is real and disclosed, not fully closed.
