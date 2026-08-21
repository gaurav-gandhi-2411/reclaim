# 0032. Entry-count guard downgrade and synchronous purge for the M1 re-walk cost budget

## Context

PR #45 (`fix/apply-batch-identity-reverify`, branch checkpoints dated 2026-08-21 in `PLAN.md`)
added M1: a full-subtree re-walk (`executor._direct_delete_directory_mismatch`) that re-verifies
every scan-recorded entry's live identity immediately before an irreversible `direct_delete`
directory candidate is permanently removed — closing a live-reproduced TOCTOU gap where content
swapped at a candidate's path between scan and apply was destroyed or misrouted.

That re-walk has a real, measured, non-batchable cost. N2's follow-up (same `PLAN.md` session)
replaced the identity-read half with a batched `GetFileInformationByHandleEx` call (1.35x-1.42x
real speedup, confirmed via a `FileId`-vs-`st_ino` equivalence proof), but disclosed a second,
architecturally distinct bottleneck it could not remove: `_direct_delete_directory_mismatch`'s
third loop calls `check_hardlink_shared_active_install` per live FILE, which needs a real
`os.stat()` per file to read `st_nlink` — `FILE_ID_BOTH_DIR_INFO` (the struct the batched read
uses) has no `nNumberOfLinks` field, and no batched Win32 directory-query API provides one. On a
real, disposable mirror of this machine's actual `%LOCALAPPDATA%\npm-cache` (88,864 files, 11,205
dirs), the full re-walk still measured a NEW median of 12.30s with a 9.30s-17.08s range across 5
interleaved reps — under the checkpoint's own ~15s absolute ceiling on the median, but not
reliably so on a per-run basis (one of five reps exceeded it on the exact same fixture).

`npm-cache` is `package_caches`, a category ADR-0003's addendum already exempts from the
byte-size guard (`size_guard_exempt=True`) because recovery cost for a package manager cache
never scales with size — but that exemption was never a statement about RE-WALK cost, which is a
completely different, unrelated axis. A `package_caches` candidate can therefore be arbitrarily
small in bytes (or exempt outright) while still containing enough entries to make M1's re-walk
take longer than a human waiting on `reclaim apply` should have to.

Separately, ADR-0005 already established that a guard-downgraded, `rebuildable`
(`category_group in REBUILDABLE_CATEGORY_GROUPS`) candidate gets `retention_days=0` instead of
the guard's normal 30-day window — "immediately purge-eligible." But nothing in the codebase
before this ADR actually purged it: `retention_days=0` only ever meant "the very next `reclaim
purge --apply` run, whenever a human or a scheduled task runs it, will pick this up." Until then,
the item sits vaulted with zero bytes freed — a real gap against ADR-0001's own founding
rationale (real disk-free delta, not merely recoverability) for exactly the category of item that
was never going to have a review checkpoint in the first place (direct-delete has never offered
one).

## Decision

1. **A second, independent guard axis, keyed on entry count rather than bytes.**
   `ScanIndex.subtree_entry_count(under)` — a cheap indexed `COUNT(*)` over the last scan's own
   recorded rows, never a live walk — lets `apply_batch` decide, BEFORE attempting the expensive
   re-walk, whether a `retention_days is None` DIRECTORY candidate's scan-recorded subtree entry
   count is at or above `direct_delete_entry_count_guard` (config:
   `safety.direct_delete_entry_count_guard`, default `87_882`). If so, the candidate is
   force-downgraded from `direct_delete` to `vault` the same way the byte-size guard already
   works (`_effective_method_and_retention_days`), independent of `size_guard_exempt` — that flag
   only ever meant "this category's recovery cost doesn't scale with size," never "this
   category's re-walk cost doesn't scale with entry count." A guard-downgraded, `rebuildable`
   candidate gets `retention_days=0` under the exact same ADR-0005 rule a byte-size-guard
   downgrade already gets.

   The default (`87_882`) is a real, measured crossing point, not a round number: using the
   WORST (not median) of the 5 real reps measured against the 100,069-entry (88,864 files +
   11,205 dirs) npm-cache-shaped fixture — 17.08s — as the per-entry rate basis (170.68us/entry),
   `15s ÷ 170.68us/entry ≈ 87,882.6`, floored. The worst rep, not the median, is deliberately
   used: the prior checkpoint already flagged the median alone as "not reliably under budget on a
   per-run basis," and a human waiting on `apply` experiences the run they get, not an average of
   five they didn't.

2. **Once a directory is downgraded from `direct_delete` to `vault` (by either guard), M1's
   full-subtree re-walk is structurally never reached for it** — `_direct_delete_directory_mismatch`
   is only ever invoked from `_preflight_skip_reason`'s `item_method == "direct_delete"` branch.
   This is not new code; it falls out of the existing tiered-by-recoverability design M1 already
   established (vaulted directories get the cheaper top-level `(dev, ino)` check only). The
   entry-count guard's whole purpose is choosing this cheaper path BEFORE paying the walk's cost,
   not after.

3. **The top-level `check_hardlink_shared_active_install` call is skipped for `method="vault"`
   candidates specifically** (previously ran unconditionally for every candidate).
   `check_hardlink_shared_active_install`'s own docstring already establishes that deleting a
   hardlink-shared path is NOT destructive to any sibling by construction (an unlink only
   decrements the shared inode's reference count) — the check exists purely as
   defense-in-depth against an unattended apply silently doing something a human might be
   surprised by, not because the delete itself corrupts anything. A vault move
   (`_atomic_move`: same-volume `os.rename` in the common case, cross-volume copy-then-delete in
   the fallback) shares that exact "not destructive to the sibling" property in EITHER case, and
   additionally never destroys anything at all — the item stays fully restorable via
   `reclaim undo`. Direct-delete's genuine permanence is the actual reason this check matters
   there; vault has neither of the two properties (permanence, destructiveness) that motivate it.
   Grep-confirmed no UI/reporting path depends on this specific skip_reason firing for a
   vault-method item — the only consumer outside `executor.py` is the generic
   `PreflightSkipReason` display, which renders any skip reason identically. Kept fully,
   unconditionally, for `direct_delete` and `recycle_bin` — load-bearing there, per the original
   P0-1 finding this session's work does not revisit.

4. **Synchronous purge for guard-downgraded, `retention_days=0` items, within the SAME
   `apply_batch` call.** Immediately after the main per-item loop (same manifest lock, same
   `try:` block), every succeeded item where `item_method == "vault"`, the resolved
   `item_retention_days == 0`, AND `candidate.retention_days is None` (the CATEGORY's own,
   pre-guard setting — checked explicitly, never inferred from the resolved value alone) is
   purged right back out of the vault: a `phase="intent", operation="purge"` manifest entry
   (fsynced), the real `shutil.rmtree`/`unlink_clear_readonly` call against the vault copy, then
   a `phase="done", purged=True` entry (fsynced) on success, or `phase="aborted"` on failure
   (the item then simply stays a normal, `retention_days=0` vault entry — eligible for the next
   `reclaim purge --apply` run, exactly as it would have been before this ADR). `disk_free_after`
   is measured only after this purge pass completes, so `BatchApplyReport.disk_free_delta_bytes`
   reflects the real freed bytes within the same call, not a promise about a future run.
   `ItemApplyResult.synchronously_purged`/`BatchApplyReport.synchronously_purged_count`/
   `bytes_synchronously_purged` surface this per-item and in aggregate, through both the CLI and
   `POST /api/apply`'s response.

   The `candidate.retention_days is None` scoping (checked BEFORE guard resolution) is
   deliberate and narrow: a category a human explicitly configures with `retention_days: 0` in
   `config.toml` is NEVER swept into synchronous purge by this mechanism — it keeps the ordinary
   "vault now, `reclaim purge` explicitly later" checkpoint completely unchanged, because that
   item was never routed through either guard at all.

## Why this does not reopen ADR-0001's rejected "auto-purge everything on every run" alternative

ADR-0001's alternative 3 ("auto-purge every vaulted item after its retention window, on every
run, without a separate command") was rejected specifically because it "removes the explicit,
reviewable checkpoint between '30 days have passed' and 'these bytes are gone forever.'" That
rejection is about **vaulted items whose category was assigned `retention_days=30` (or any
positive window) precisely because they were meant to have that checkpoint** — `old_installers`,
`archive_pairs`, `large_logs`, `duplicates`, and any vaulted-with-a-real-window item.

This ADR's synchronous purge never touches any of those. It is scoped, by construction, to items
whose `candidate.retention_days is None` — i.e., items whose category was ALREADY going to be
permanently, irreversibly deleted with **zero review checkpoint of any kind** the instant `apply`
ran, before this ADR (and before ADR-0003/0005) ever existed. ADR-0003 detoured those items
through a vault-then-eventually-purge round trip purely as a **recovery-cost** safety net (protect
against an unboundedly-expensive-to-redo item slipping through a `retention_days=None` category),
not to grant them a review checkpoint they never had. ADR-0005 already established that for the
`rebuildable` subset specifically, that round trip should be as short as possible
(`retention_days=0`) because "regret is impossible... their only recovery path was always
'rebuild it.'" This ADR is the direct completion of that reasoning: if regret really is
impossible and the round trip exists purely for recovery-cost safety (not review), there is no
remaining reason to leave the bytes sitting in the vault, unfreed, until some unrelated later
`reclaim purge` invocation. Synchronously closing that round trip within the same `apply_batch`
call restores ADR-0001's real-disk-free-delta guarantee for exactly the category of item that
was always going to lose the bytes anyway — it does not remove a checkpoint, because there was
never one there to remove.

The one genuinely new thing this ADR adds beyond "purge sooner" is doing so INSIDE `apply_batch`
rather than via an explicit follow-up `reclaim purge --apply` invocation. This is deliberate, not
incidental: P1's requirement is that `retention_days=0` mean what it says — bytes actually gone
by the time `apply` returns — and the only way to guarantee that without inventing a new
background-job/auto-scheduling mechanism this project does not otherwise have is to do it
synchronously, in the same call, under the same manifest lock. A caller that never wants this
behavior for guard-downgraded rebuildable items has no config knob to disable it today; this was
judged acceptable because the category is, by definition, one users have already opted out of any
review workflow for (the category's own `retention_days=None` setting).

## Consequences

- `ScanIndex` gains `subtree_entry_count` — a cheap indexed COUNT query, registered in
  `tests/test_query_plan_coverage.py`'s completeness gate like every other SQL-issuing method.
- `apply_batch`/`SafetyConfig` gain `direct_delete_entry_count_guard` (default `87_882`),
  threaded through the CLI (`reclaim apply`) and `POST /api/apply` exactly like
  `direct_delete_size_guard_bytes` already is.
- `ItemApplyResult` gains `synchronously_purged: bool = False`; `BatchApplyReport` gains
  `synchronously_purged_count: int = 0` / `bytes_synchronously_purged: int = 0`. Both default to
  the pre-existing behavior for every caller that never triggers a guard downgrade — no existing
  test needed to change to accommodate the new fields, only the one test (`evals/
  test_apply_safety_preflight.py`) whose assertion literally contradicted the new, intentional
  vault-method hardlink-check-skip behavior (split into two tests: the direct-delete regression,
  unchanged, and a new vault-method non-skip proof).
- `check_hardlink_shared_active_install`'s top-level call site in `_preflight_skip_reason` now
  branches on `item_method`, matching M1's own established "tiered by recoverability" pattern
  rather than introducing a new one.
- A vault entry created via synchronous purge and then immediately purged is indistinguishable,
  after the fact, from an ordinary `retention_days=0` vault entry a human simply hasn't purged
  yet — same manifest shape, same `reclaim.recovery` crash-safety machinery (which already
  handles `operation="purge"` generically, regardless of which code path wrote the intent), zero
  new code needed in `reclaim.recovery` for this to work correctly under a crash between the
  vault "done" write and the purge intent, or between the purge intent and its own "done" write
  (both proven directly in `evals/test_apply_identity_reverify.py`, mirroring the exact
  `KeyboardInterrupt`-injection pattern `tests/test_recovery.py` already established for
  ADR-0026).

## Alternatives considered

1. **Gate M1's re-walk behind a size/entry-count threshold that skips the CHECK, not the
   METHOD** (i.e., a large direct-delete candidate still permanently deletes, just without the
   re-walk's protection above the threshold). Rejected: this reopens the exact TOCTOU gap M1
   exists to close, for precisely the largest, highest-consequence candidates — the wrong
   direction to trade off cost against safety. Downgrading the METHOD instead means large
   candidates get a real, different, cheaper-to-verify safety mechanism (the top-level identity
   check + reversibility), not a silently weaker version of the same one.
2. **Accept the measured cost as-is (do nothing).** Rejected per the prior checkpoint's own
   framing: a single real rep already exceeded the ~15s budget on the exact worst-case fixture
   this project has measured, and `package_caches`' `size_guard_exempt=True` means the existing
   byte guard structurally cannot ever catch this shape of candidate.
3. **Fix the third loop's nlink-stat cost directly** (find or build a batched way to read
   `st_nlink`). Rejected for this fix: no batched Win32 directory-query API exposes
   `nNumberOfLinks` (confirmed in the prior session's own investigation) — a raw `DeviceIoControl`
   approach would be new, unproven surface for a safety-critical path, out of proportion to a
   guard-and-downgrade fix that solves the same problem by avoiding the walk entirely for the
   cases that would trip the budget.
4. **Report `retention_days=0` items as "pending purge" with a zero real delta instead of
   purging synchronously** (P1's option (b)). Rejected: breaks the "delete immediately" promise
   `README.md` already makes for rebuildable caches, and defers ADR-0001's real-disk-free-delta
   guarantee to an indefinite future `reclaim purge` invocation for items that, before this ADR,
   were never going to sit in a vault waiting for review in the first place.
5. **A config toggle to disable synchronous purge for guard-downgraded items.** Rejected for v1:
   no user-facing need identified yet, and the category this touches is, by construction, one
   whose category-level `retention_days=None` setting already means the user opted out of any
   vault-review workflow for it. Revisit if a real use case for "downgrade but don't purge"
   surfaces.
