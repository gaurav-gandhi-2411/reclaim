# 0001. Category-tiered retention

## Context

Phase 1 shipped with a single, uniform recovery model: every quarantined file is moved
(`vault` method, default) or `send2trash`'d into the Recycle Bin, and nothing is ever
permanently deleted — the spec states this twice ("Every action recoverable... No permanent
delete in v1" and "No Tier for silent permanent deletion. It does not exist in v1").

Manual end-to-end verification during Stage 6 (see `PLAN.md`'s 2026-07-13 checkpoint)
surfaced a real problem with this model: moving a file into `data/quarantine/` keeps it on
the same NTFS volume as the source, so `shutil.disk_usage()` shows a **0-byte delta**
immediately after a real apply. The same physics applies to the Recycle Bin — nothing
frees disk space until a human empties it. Combined with the "no permanent delete" rule,
Phase 1 as built cannot literally satisfy the project's top-level success criterion:
"Reclaims ≥30 GB on GG's machine in first run... verified via before/after disk-free
measurement." Vaulting is real recoverability, but it is not real reclamation on a
94%-full single-volume machine.

Separately, several rule categories (dev artifacts with manifest-adjacency already
enforced, package/model caches, temp + browser caches, crash dumps) have a recovery
mechanism that is *not* "the original bytes come back" — it's a deterministic, already-
displayed rebuild command (`npm ci`, re-run the pip/uv/HuggingFace download, let Windows
re-create its own temp/crash-dump files). For these categories, keeping the literal bytes
around in a vault adds real disk cost and real apply-time I/O without adding real
recoverability beyond what the rebuild instruction already provides.

## Decision

Retention is now a per-category-group property (`config.categories.<group>.retention_days:
int | None`), not a single project-wide policy:

- **`retention = none`** (permanent delete on apply, no vault): `dev_artifacts`,
  `package_caches`, `temp_and_browser_caches`, `crash_dumps`. These are exactly the
  categories whose rationale already carries (or structurally could carry) a rebuild
  instruction, and whose manifest-adjacency / global-cache-location checks already make
  false positives unlikely. On apply, the file/directory is permanently removed
  (`Path.unlink()` / `shutil.rmtree()`) — not moved, not trashed. A manifest entry is still
  written for every direct-deleted item (`retention="none"`, `vault_path=None`) recording
  path, size, category, rationale, and rebuild instruction — for audit and reporting, never
  for restore. Restoring a `retention="none"` entry is impossible by construction and
  `restore_batch` must refuse it with the same honest, no-fabricated-capability posture
  already established for `recycle_bin` entries (see `RecycleBinRestoreUnsupportedError`).
- **`retention = 30` (days), the default for everything else**: `old_installers`,
  `archive_pairs`, `large_logs`, `duplicates`. Unchanged from Phase 1's existing behavior —
  vault (or `recycle_bin`) + manifest + restore, plus a new **explicit** `purge` command
  that permanently deletes only vaulted items whose `retention_until` has passed. `purge`
  is never automatic, is dry-run by default, and only ever acts on the `vault` method's own
  copies in `data/quarantine/` — it does not touch anything at the item's original path
  (which, by the time retention expires, is long gone; the vaulted copy is the only thing
  left to purge).

Both new permanent-delete code paths (`retention=none` direct-delete, and `purge`)
re-run `SafetyValidator.evaluate()` on every path immediately before deleting anything —
defense in depth on top of the Stage 3/4 candidate-generation gate, following the same
posture `apply_batch`'s existing `SafetyInvariantError` check already established. This is
non-negotiable: it is the only thing standing between a misconfigured or tampered
`config.toml` and an unrecoverable mistake.

## Consequences

- **`send2trash` is bypassed entirely for `retention=none` categories.** These categories
  tend to contain many small files (`node_modules`, cache directories) where Recycle-Bin
  move overhead and Explorer's practical Recycle-Bin size/count limits are a real concern;
  direct deletion is also simply faster, and speed matters more here because these
  categories are exactly the ones expected to run automatically and often.
- **This introduces the project's first genuine, code-driven permanent-delete capability.**
  Highest blast radius of any change so far. The CI hard gate (Stage 1: "zero
  protected-category files may ever appear in Tier A") must be extended to independently
  prove zero protected files can ever reach *either* new delete path — including under an
  adversarial/malicious `config.toml` that tries to force a protected path through
  `retention=none` or into a purge-eligible vault entry. This is a new, separate assertion
  in the eval suite, not a reuse of the existing Tier-A-candidate-generation gate, because
  the attack surface is different (config-driven retention assignment vs. detector output).
- **Manifest schema gains `retention: Literal["none"] | int` per entry** (days remaining
  is derived from `retention_until` at read time, not stored redundantly). A `retention=
  "none"` entry always has `vault_path=None` and can never transition to `restored=True`.
- **The original spec's blanket "No permanent delete in v1" is explicitly superseded** for
  the four `retention=none` category groups. This is a deliberate, reviewed scope change —
  recorded here rather than silently drifting — not a violation of the original rule; the
  rule's intent (never destroy something the user might still need without a real recovery
  path) is preserved because the recovery path for these categories was always "the rebuild
  command," never "the vault."
- **`purge` needs its own dry-run-by-default posture and its own measured bytes-freed
  report**, mirroring `apply_batch`'s existing report shape (real per-item results, real
  `shutil.disk_usage()` before/after — and this time the delta *should* be non-zero, since
  purge actually removes bytes from the volume, unlike vaulting).
- Restoring a `retention=none` batch, or a batch containing already-purged vault entries,
  must fail with a clear, specific, actionable error — never silently no-op and never
  claim success.

## Alternatives considered

1. **Keep vault-only uniformly (status quo).** Rejected: doesn't free real disk space on a
   single, nearly-full volume without the user manually emptying the vault/Recycle Bin —
   directly conflicts with the ≥30 GB success criterion this project exists to hit.
2. **Move the vault to a second physical volume.** Rejected for v1: assumes a second drive
   exists on the user's machine (not guaranteed — GG's is a single-volume 94%-full disk,
   the exact scenario this tool targets), adds a new required config surface, and does
   nothing for the `retention=none` categories' actual disk math on the source drive
   either way.
3. **Auto-purge every vaulted item after its retention window, on every run, without a
   separate command.** Rejected: removes the explicit, reviewable checkpoint between "30
   days have passed" and "these bytes are gone forever." A dedicated `purge` command that
   the user (or a scheduled task the user sets up themselves) runs deliberately keeps that
   checkpoint, consistent with rule 55's "pause before destructive operations" posture.
4. **Make `retention=none` vs vaulted a per-run flag instead of a per-category-group
   config.** Rejected: the decision of "is this bytes-identical-recovery or
   rebuild-command-recovery" is a property of the *category*, not the run — flagging it at
   category-group granularity in `config.toml` keeps the policy visible, auditable, and
   consistent across runs, and matches how every other category-specific setting
   (`old_installers.max_age_days`, `large_logs.min_size_bytes`, ...) is already modeled.
