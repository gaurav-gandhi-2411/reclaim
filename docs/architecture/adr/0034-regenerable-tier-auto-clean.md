# 0034. Regenerable tier: a closed allow-list that frees real space in safe mode

## Context

On 2026-10-01 the owner's default-mode "Clean My Computer" yielded **0 auto-cleanable
candidates** — the original "a fresh install cleans nothing" P0 in a new form. The cause is the
interaction of two correct decisions in ADR-0023 (safe mode):

1. every scan-driven candidate is forced to Tier B (review-only, guarantee 3), and
2. every apply in safe mode is Recycle-Bin-only (guarantee 1).

A Recycle Bin move frees **no disk space** until the bin is emptied (the #48 house rule), so even
a fully-reviewed safe-mode apply of the categories that matter most — package caches, aged TEMP,
crash dumps, browser caches — reclaims nothing. Measured on the owner's drive on 2026-10-01:
`C:` was 96% used, with `uv`'s cache alone at 26.7 GB and npm's at 2.2 GB; the product could
neither show those as "safe" nor free them.

ADR-0023's guarantees exist because the generic pipeline can reach **any** file under a scan
root, driven by a path list that crosses an API boundary. That risk does not exist for a
closed-world operation whose targets are fixed in code. Weakening the generic pipeline (letting
safe mode permanent-delete some candidates) would have been the wrong repair: it would have
turned a structural boundary into a conditional.

## Decision

Add a **separate** code path, `reclaim.regenerable`, that does not call `apply_batch`, does not
read the scan index, and does not accept a path from any caller:

- **What it may touch (the allow-list, in code, not config):**
  - *Package-manager caches via the owning tool's own command*: `uv cache prune` (unreferenced
    entries only — never `uv cache clean`), `pip cache purge`, `npm cache clean --force`,
    `conda clean --tarballs --index-cache -y` (never `--packages`/`--all`, which can remove
    package directories hardlinked into live environments), `yarn cache clean`.
  - *Direct children of `%TEMP%` / `C:\Windows\Temp`* whose **newest** content is older than the
    7-day floor (the rule from #110: a directory's own mtime is meaningless).
  - *Crash dumps / WER report queues* under the fixed CrashDumps and WER roots, same age rule.
  - *Disk caches (`Cache`, `Code Cache`, `GPUCache`, Firefox `cache2`) of Chrome, Edge, Brave and
    Firefox*, only while that browser's process is not running. Never cookies, history, local
    storage, or extensions.
- **What it never does:** empty the Recycle Bin; purge or touch the vault; follow a junction or
  symlink (the link entry is removed, the target never); delete a tree containing `.git`,
  `.venv`/`venv`/`pyvenv.cfg`, `site-packages` or `node_modules` (reported for review instead);
  run a tool with `--force` past its lock; run anything outside the table in the module.
- **Per-delete re-checks at the moment of deletion:** a share-mode-0 `CreateFileW` probe
  (any other open handle ⇒ skip), reparse-point check, top-level realpath containment. Failure of
  any probe — including failure to enumerate running processes — is a **skip, never a pass**
  (rule 98a, fail closed).
- **Audit:** every item, plus every skipped/failed path (capped), is appended to
  `data/regenerable_audit.jsonl` with a run id; the summary line records logical bytes removed and
  the measured disk-free delta.
- **Reporting honesty:** `bytes_removed` is *logical* bytes unlinked or the cache directory's
  shrinkage; for hardlinked trees (uv's cache) it can exceed what the volume gained, so the
  measured `disk_free_delta_bytes` is reported beside it, never instead of it.
- **Surfaces:** `POST /api/clean/regenerable` (`apply=false` previews; body has no path field and
  `extra="forbid"`), the one-click button in Simple mode, `reclaim auto-clean`, and the weekly
  scheduled task (B5).

### What this does **not** change

ADR-0023 is untouched and still true of the generic pipeline: `apply_batch` in safe mode is
Recycle-Bin-only, candidates are Tier B, blanket applies are refused, and
`evals/test_safe_mode_gate.py` still proves it. This ADR *adds* a second, narrower, separately
tested mechanism; it does not amend any of those guarantees. The generic review flow remains the
only way anything outside the allow-list is ever offered for deletion.

## Consequences

- One click on a fresh install frees real space for the categories with no human-judgement
  component, in safe mode, with no scan.
- A second delete path exists, so it must stay closed: `tests/test_regenerable.py` has teeth tests
  for "active cache skipped" (running browser, open handle, running tool), "aged cache cleaned",
  and "non-allow-listed path untouched" (documents, downloads, HF cache, programs, a similarly
  named `CrashDumpsNot`), plus junction-not-followed and fail-closed process enumeration. Adding
  an entry to the allow-list is a code change with a test, never a config edit.
- It runs unelevated (`reclaim.elevation.assert_not_elevated` is respected); roots that need
  administrator rights (`C:\Windows\Temp`, `ProgramData` WER) are reported `skipped_no_access`,
  not failed.
- **What did not work / known limits:** (1) *Superseded behaviour, kept for the record:* the first version skipped `uv cache prune` whenever any `uv` process was running, and on a workstation running several agent sessions that is most of the time, so the biggest cache (26.7 GB) was almost never cleaned. uv's cache lock exists precisely so a prune WAITS for in-flight installs instead of racing them, so the pre-skip was removed: `uv cache prune` now runs with `UV_LOCK_TIMEOUT` = `UV_LOCK_WAIT_SECONDS` (30 min, overridable via `RegenerableEnv.uv_lock_wait_seconds`) and a subprocess timeout 60 s longer, never `--force`. Only an expired wait is a skip: `skipped_in_use`, detail "waited 1800 s for uv's cache lock". uv runs last so every other item is finished and reported before the wait, and the one-click `POST /api/clean/regenerable` (`apply=true`) is a single-flight background job (202 + run id, 409 if one is running) polled through `GET /api/clean/regenerable/status`, so no HTTP request is held for up to 30 minutes; `apply=false` previews stay synchronous and never wait. pip/yarn/conda keep the busy-process skip because no lock they hold is relied on here. A scheduled caller must allow for the wait (its task time limit must exceed 30 min plus the rest of the run). (2) The share-mode-0 probe protects
  against deleting a file another process has open, but not against a process that opens a file
  immediately after the probe; the age floor and the closed-browser gate carry that residual risk.
  (3) Logical-vs-real byte accounting for hardlinked caches is approximate by construction.

## Alternatives considered

- **Let safe mode permanent-delete `package_caches`/`temp`/`crash_dumps` candidates.** Rejected:
  turns ADR-0023's structural boundary into a conditional and keeps a path-list-driven delete in
  the default mode.
- **Add a Tier A back for these categories in safe mode.** Same objection, and still scan-driven
  (a full-profile walk before the first byte is freed).
- **Empty the Recycle Bin after moving.** Rejected outright: it would destroy items the user put
  there for unrelated reasons.
- **Do nothing and keep the review flow.** Rejected: the review flow cannot free space in safe
  mode at all, which is the defect.
