# 0026. Crash-safe two-phase manifest (intent/done) for apply/purge/restore

## Context

A production-readiness audit (2026-07-24, see `PLAN.md`) found a critical data-integrity gap
shared by `executor.apply_batch`, `executor.restore_batch`, and `purge.purge_expired`: each
function's per-item loop performs its filesystem action (vault move, `send2trash`, permanent
delete, restore-move) immediately, but only appends the corresponding `QuarantineManifestEntry`
records to `manifest.jsonl` once, in a single batched write **after the entire loop finishes**
(`executor.py`, pre-fix: `append_manifest_entries` called once after the `for candidate in
candidates` loop; `purge.py` and `restore_batch` had the identical shape).

Killing the process (crash, power loss, forced termination) at any point during that loop — after
some items have already been moved/trashed/deleted/restored on disk but before the loop finishes —
leaves every already-processed item with **zero manifest trace**. There is no way to know, after
the fact, which items were touched, where a vaulted file actually ended up, or whether a restore/
purge that appeared to run actually completed. No resume mechanism existed; the only options were
manual filesystem archaeology or treating the vault directory as opaque, unaccounted-for space.

This is exactly the failure mode ADR-0004 already fixed for a *single* `_atomic_move` call (an
interrupted copy leaving orphaned, unreferenced vault debris) — but one level up: ADR-0004
guarantees each individual move is atomic-or-nothing; this ADR guarantees the **manifest's record
of that move** survives a crash at any point in the batch, not just within one move.

## Decision

**Two-phase intent log, not naive write-then-act.** A write-then-act ordering (write the manifest
entry, then perform the action) would create the opposite problem: a crash between the write and
the action leaves a manifest entry describing something that never happened (a phantom quarantine
record, a "restored" file that's still in the vault). Instead, per item:

1. Append a `phase="intent"` `QuarantineManifestEntry` (full payload — everything the eventual
   `"done"` entry would carry, including the pre-computed `vault_path` for a vault-method item),
   then `fh.flush()` + `os.fsync(fh.fileno())` — durable on disk before anything is touched.
2. Perform the actual filesystem action (unchanged from before this ADR).
3. Append a `phase="done"` entry (same `intent_id`, same content) on success, or `phase="aborted"`
   on a caught per-item exception — again `flush()` + `fsync()`. A caught, handled failure closes
   its own intent immediately; there is nothing for recovery to reconcile about it.

`phase` defaults to `"done"` and `intent_id`/`operation` are optional on
`QuarantineManifestEntry`, so every manifest line written before this ADR — which had no
intent/done split at all, since the action had already fully completed by the time anything was
written — parses and folds exactly as before. This is not an approximation: an old-format line has
no way to be an orphaned intent, so defaulting it to `"done"` is the literal truth for that line.

**`fold_latest_manifest_entries` only trusts `phase="done"` entries.** Building the "current
state" view (used by `purge_expired`'s eligibility scan, `restore_batch`'s batch lookup, and every
API read) now skips `intent`/`aborted`/`needs_review` entries entirely before folding to
latest-per-key. A crash that leaves an orphaned intent makes that item **invisible** to normal
reads until `reclaim recover` resolves it — silently absent is the safe failure mode; an
unconfirmed intent must never be mistaken for a completed operation just because it's the last
line written for its key.

**Reconciliation (`reclaim.recovery`), replayed at will, not automatically at every startup.** An
orphaned intent is one whose `intent_id` has no later `done`/`aborted`/`needs_review` entry.
`reclaim.recovery.compute_reconciliation` (read-only, side-effect-free — safe for the dashboard to
call on every page load) and `reclaim.recovery.reconcile_manifest` (same classification, plus
writes the resolving entry) both classify each orphaned intent by `os.stat`-ing the two real
filesystem locations its `operation`/`method` implies:

| operation | source | target |
|---|---|---|
| apply, method=vault | `original_path` | `vault_path` |
| apply, method=recycle_bin/direct_delete | `original_path` | *(none — no real target to check)* |
| restore | `vault_path` | `original_path` |
| purge | `vault_path` | *(none)* |

- **Source only** → `aborted` (the action never executed).
- **Target only** → `completed` (it ran; only the `done` record was lost to the crash) —
  synthesizes a proper `phase="done"` entry (with `restored=True`/`purged=True` as appropriate)
  so the item folds back into normal reads exactly as if the crash had never happened.
- **Both, or neither** → `needs_review` — never guessed, surfaced for a human (e.g. the
  cross-volume copy-fallback window in `_atomic_move` where a copy can succeed before the source
  is removed).
- **No real target for this operation/method** (recycle_bin, direct_delete, purge) — the
  degenerate one-location case of the same rule: source present → `aborted`, source absent →
  `completed`. This is not a weaker guess: for these methods there genuinely is no second
  location this tool has ever had a way to check (see `RecycleBinRestoreUnsupportedError`'s
  existing "no programmatic handle on the Recycle Bin" reasoning).
- A `vault_path` that doesn't resolve inside the configured vault directory is **never** trusted
  enough to synthesize a `completed`/`aborted` verdict from — forced to `needs_review`
  unconditionally, the same zip-slip-equivalent posture `RestoreIntegrityError` already applies
  to restore.

`reclaim recover` (dry-run by default, `--apply` writes the resolving entries — same convention as
`apply`/`purge`) and a read-only `GET /api/recovery/status` dashboard endpoint both surface this.

## Measured fsync cost, and why per-item fsync ships anyway

Benchmarked against a synthetic 23,000-item vault-apply (matching the real `dev_artifacts`
pycache batch shape referenced in `executor.py`'s own ADR-0004 comment: "23,565 direct_delete
entries alongside 7 vault ones"), on this development machine (Windows 11, local NVMe SSD):

| | elapsed | per-item | fsync calls |
|---|---|---|---|
| With per-item fsync (this ADR, as shipped) | 186.02s | 8.088 ms | 46,000 (2/item) |
| Without fsync (flush-only baseline) | 21.14s | 0.919 ms | 0 |
| **Delta attributable to fsync** | **164.88s** | **7.168 ms/item** | — |

(`scripts/bench_fsync.py`, run 2026-07-24 against this exact implementation — not extrapolated.)

Per-item fsync is roughly **9x slower** for a many-small-files batch of this shape. This is real
and not hidden: it is *not* prohibitive for this tool's actual workload — a user-initiated,
occasional bulk-cleanup operation, not a hot loop — 3 minutes for a 23,000-file batch is within
normal tolerance for a one-time disk cleanup, and the honest alternative (skip fsync, silently
accept a crash-loss window) is exactly what this ADR exists to eliminate.

**Considered, not implemented: fsync only the intent write, defer the done write.** Because
`reclaim.recovery` can independently reconstruct "completed" from real on-disk state even without
a durable `done` record (source gone, target present), the `done`-phase fsync is not load-bearing
for correctness the way the `intent`-phase fsync is — the intent fsync is what guarantees a
durable record exists that an action was *attempted* at all; without it, a crash immediately after
a real move but before the intent write reaches disk would leave literally nothing in the
manifest for `reclaim.recovery` to find and reconcile. Skipping only the `done` fsync would
roughly halve the measured cost (to an estimated ~3.6 ms/item, ~104s for the same 23k-item batch)
at the cost of a **bounded exposure**: any crash between a `done` write and its fsync reaching
disk requires an explicit `reclaim recover --apply` pass before that item is visible in normal
reads again, rather than it already being visible immediately. Not implemented in this pass —
the measured 9x cost is judged acceptable for the current workload; revisit if real-world usage
(much larger single batches, or spinning-disk hardware where fsync costs more) shows otherwise.

## Consequences

- No mid-batch kill can any longer leave a processed item with zero manifest trace: it is either
  fully invisible-but-recoverable (an orphaned intent, resolved by `reclaim recover`) or fully
  recorded (`done`), never silently lost.
- `reclaim recover` and its dashboard surface are new user-facing affordances; SIGKILL-based tests
  (killing the process at each of: before-intent-fsync, after-intent-fsync-before-action,
  after-action-before-done-fsync) for apply, purge, and restore prove reconciliation is correct at
  every one of these windows — see `tests/test_recovery.py`.
- Every batch now pays the measured fsync cost above; this is disclosed, not hidden, and judged
  acceptable per the measurement.
- `needs_review` items are a real, if rare (only the copy-fallback window observed so far), new
  terminal state that requires a human — never auto-resolved, by design.

## Alternatives considered

1. **Naive write-then-act** (write the manifest entry, then act). Rejected: creates phantom
   manifest entries for actions that never happened on a crash between the two steps — worse than
   the problem being solved, not better.
2. **Batch fsync every N items.** Rejected for now (see measured-cost section above) — would cut
   cost roughly proportionally to N but reintroduces exactly the up-to-N-item loss window this
   ADR exists to close, without recovery being able to fully close the gap for the `intent`-phase
   half of it (see "considered, not implemented" above for why intent-phase fsync specifically is
   load-bearing).
3. **WAL-style single durable log entry per action instead of two.** Rejected: would require
   either a stateful lock file per in-flight item (extra failure mode of its own) or losing the
   "source untouched" provability the intent record gives for the `aborted` classification.
