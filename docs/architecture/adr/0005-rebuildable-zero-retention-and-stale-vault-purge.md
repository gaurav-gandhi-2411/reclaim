# 0005. Rebuildable zero-retention and stale-vault purge eligibility

## Context

The 2026-07-17 package-cache re-apply (see ADR-0003's addendum and `PLAN.md`) left the prior
run's 14.3GB `uv/cache` vault copy stranded: `uv` had already regenerated its cache at the
original path (exactly the "rebuilds automatically on next use" behavior ADR-0001's
`rebuild_instruction` already promises for this category), so the vaulted copy became a stale
duplicate the moment that happened — restoring it would refuse ("destination already exists")
regardless of anything else. But its manifest entry still carried the guard's normal 30-day
`retention_days` (ADR-0003), so `purge_expired`'s hard `retention_until` boundary held it
hostage for the rest of that window even though it could never again be usefully restored.

The same shape recurs for any rebuildable category a guard-downgrade (ADR-0003) or a future
code path routes to `vault` instead of `direct_delete`: `windows_temp`/`browser_cache`
(`temp_and_browser_caches`), `dev_artifacts` (pycache et al.), `crash_dumps`, and
`package_caches` all default `retention_days=None` specifically *because* their owning tool
redownloads/regenerates them deterministically (ADR-0001) — the 30-day vault window these
categories get when guard-downgraded was never really protecting anything worth protecting.

## Decision

1. **`Candidate.rebuildable: bool`**, resolved in `generate_candidates` from a new shared
   constant `models.REBUILDABLE_CATEGORY_GROUPS` (`dev_artifacts`, `package_caches`,
   `temp_and_browser_caches`, `crash_dumps` — exactly the `retention_days=None`-by-default
   groups). `_effective_method_and_retention_days` (executor.py) now gives a guard-downgraded
   *rebuildable* candidate `retention_days=0` instead of the guard's normal
   `size_guard_retention_days` (30) — immediately purge-eligible from the moment it's
   quarantined. A guard-downgraded candidate that is NOT rebuildable (a hypothetical
   misconfigured category with `retention_days=None` outside the four known groups) keeps the
   safer 30-day default; this is a targeted exception for known-safe categories, not a general
   change to the guard's behavior.
2. **Stale-vault detection, independent of category and of `retention_days`.**
   `purge._is_stale_original_reoccupied(entry)` checks whether a vault entry's `original_path`
   exists again — almost always a regenerated cache, never a human restore (a real restore
   updates the SAME entry's `restored=True`, already excluded upstream). `_purge_eligible_
   entries` now selects an entry via either of two independent paths: genuine `retention_until`
   expiry (unchanged, ADR-0001's hard boundary), OR the original path being re-occupied
   (ADR-0005, bypasses `retention_until` entirely) — since a stale entry can never be usefully
   restored regardless of category or how much of its retention window remains. Each selected
   entry is tagged with which path selected it (`stale: bool` on `PurgeItemResult`, plus
   aggregate `stale_count`/`stale_bytes` on `PurgeReport`) so a report never conflates "this
   expired normally" with "this was dead weight from the moment something else took its place."
3. **`purge_expired(..., only_rebuildable=True)`** (CLI: `reclaim purge --rebuildable-only`)
   further restricts eligibility to `category_group in REBUILDABLE_CATEGORY_GROUPS` — a purge
   run that can never touch a `model_caches`/`duplicates`/other vault entry even if one happened
   to also be eligible (e.g. via the stale-detection path, which is deliberately
   category-agnostic). Opt-in, default `False`: existing callers of `purge_expired`/
   `reclaim purge --apply` see no behavior change unless they ask for the scoped variant.

## Consequences

- **The stale-detection check is deliberately unconditional across all categories**, not scoped
  to rebuildable ones — a model-cache or duplicate vault entry whose original path somehow gets
  reoccupied is *equally* un-restorable (clobber-refusal doesn't care about category), so it's
  equally pure dead weight to keep vaulted. `only_rebuildable` exists as a separate, explicit
  safety scope for the *apply* step, not baked into eligibility itself — a caller who wants
  "stale-only, but only for categories I'm comfortable auto-purging" gets that by combining both
  flags, without the eligibility logic itself needing to know about that combination.
- **Retention-days=0 is set at quarantine time, not retroactively.** An item already vaulted
  under a prior version of this code (30-day retention baked into its manifest entry) is
  unaffected by this change — manifest entries are an append-only event log by design (ADR-0001)
  and are never rewritten. Only the stale-detection path (independent of `retention_days`)
  reaches an already-vaulted item like this; a category's zero-retention override only applies
  to items vaulted *after* this fix lands.
- **`purge_expired`'s public signature grows one parameter** (`only_rebuildable`); `PurgeReport`/
  `PurgeItemResult` each grow fields (`stale_count`/`stale_bytes`, `stale`). All new fields
  default `False`/`0` so no existing caller needs to change.
- **The CLI's stale-vs-failed distinction matters for readability at scale**: a large purge dry
  run mixing genuinely-expired and stale-reoccupied entries would otherwise look like an
  undifferentiated pile of "eligible" items — `reclaim purge`'s dry-run output now prints a
  `STALE: <path> (original path re-occupied)` line distinctly from a category breakdown, so the
  two very different reasons an item is purge-eligible stay visibly distinct.

## Alternatives considered

1. **Retroactively rewrite already-vaulted entries' `retention_days` to 0 for rebuildable
   categories.** Rejected: violates the manifest's append-only-event-log design (ADR-0001) —
   entries are never rewritten in place, only superseded by a later `restored`/`purged` update
   line for the same key. The stale-detection path already achieves the practical goal (freeing
   the specific stranded uv/cache vault copy) without needing to touch history.
2. **Scope stale-detection to rebuildable categories only, matching the retention-days=0
   scoping.** Rejected: the *reason* a stale entry is dead weight (restore would clobber-refuse
   regardless of anything else) has nothing to do with whether its category is deterministically
   rebuildable — it's a property of "something now occupies the only place this could ever be
   restored to," true for any category. Scoping it would silently leave a stale model-cache/
   duplicate entry stranded forever with no path to ever surface or resolve it.
3. **A single combined "purge everything eligible, filtered by category" CLI flag instead of a
   separate `only_rebuildable` boolean.** Rejected for v1: no other filter dimension exists yet
   to justify a more general `--category-groups` mechanism (unlike `apply`'s
   `--include-categories`, which came from a genuine need to split one category-group's Tier-A
   candidates across multiple staged applies) — a single boolean covers today's actual use case
   without speculative generality.
