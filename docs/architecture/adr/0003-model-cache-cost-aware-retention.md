# 0003. Model-cache category and a cost-aware direct-delete size guard

## Context

ADR-0001 introduced `retention = none` (permanent delete on apply, no vault) for four
category groups whose recovery mechanism is "the owning tool re-downloads/rebuilds it" rather
than "the literal bytes come back": `dev_artifacts`, `package_caches`,
`temp_and_browser_caches`, `crash_dumps`. `package_caches` bundled HuggingFace hub, torch hub,
and other ML model-weight download directories in with pip/npm/uv/conda package caches,
because both are "global download caches matched by path" from a detection standpoint.

The first real-disk dry run against `C:\` (see `PLAN.md`'s 2026-07-17 checkpoints) surfaced
`.cache\huggingface\hub` at 124.9 GB, classified `package_cache` → `retention=none` →
permanent delete on apply. That classification was wrong: re-acquiring a `pip` cache costs one
`pip install` and a few seconds; re-acquiring 124.9 GB of model weights costs hours of
bandwidth, and for gated, private, fine-tuned, or manually-pushed models — never re-uploaded
anywhere else — re-acquisition may be **impossible**, not merely slow. "Rebuildable" was being
decided by path type (is this under a known cache directory?), not by actual rebuild cost
(how much does it cost, and is it even still possible?).

## Decision

1. **New category group `model_caches`**, split out of `package_caches`. Detects HuggingFace
   hub, torch hub, and Ollama model directories (whole-directory sweep, same pattern as
   `package_caches`), plus individual `*.safetensors`/`*.ckpt`/`*.bin` files scoped to those
   same configured roots (defense in depth for cache layouts the directory sweep alone might
   miss — never a disk-wide extension sweep). Two properties distinguish it from every other
   category:
   - **`retention_days` defaults to `30` (vaulted), never `None`.** Model caches are the one
     "redownloads deterministically" category that does NOT default to direct-delete, because
     the redownload isn't guaranteed to be possible or cheap.
   - **`suggested_tier` is hardcoded to `Tier.B`** in the detector itself, so a model-cache
     candidate is never Tier A (auto-quarantine-eligible) regardless of
     `config.categories.model_caches.enabled` — every one requires a human review before it can
     ever be quarantined. This falls out of the existing tier formula
     (`tier = rc.suggested_tier if _category_enabled(...) else Tier.B`) for free: no special-case
     branch needed in `generate_candidates`.

2. **A cost-aware size guard, independent of category**, added to `apply_batch`
   (`config.safety.direct_delete_size_guard_bytes`, default 1 GB): any candidate whose category
   resolves to `retention_days=None` (direct-delete) is force-downgraded to `vault` if
   `size_bytes >= direct_delete_size_guard_bytes`, using its own retention window
   (`config.safety.direct_delete_size_guard_retention_days`, default 30) rather than the
   category's `None`. This is deliberately a second, independent line of defense — not a
   replacement for (1) — because `retention_days=None` is a per-*category* setting, and a
   large-but-otherwise-ordinary member of an existing direct-delete category (a single huge
   crash dump, an unusually large `node_modules` artifact, or a future direct-delete category
   nobody has stress-tested against a full disk yet) shouldn't be able to permanently delete an
   expensive-to-redo item just because its category was configured `None`. Recovery cost, not
   category, is what should gate permanence.

3. **Report/dashboard show recovery cost, not just "rebuildable."** `RawCandidate`/`Candidate`
   gain an optional `recovery_cost_note` field — a per-category caveat surfaced alongside the
   existing `rebuild_instruction` (which only ever said *how*, never *at what cost or risk*).
   Populated for `model_caches` ("recovery cost scales with model size... gated/private/
   fine-tuned/manually-pushed models may be permanently unrecoverable"); `None` for categories
   whose rebuild is already cheap enough not to need a caveat. `reclaim apply`'s CLI report now
   prints `rebuild_instruction`/`recovery_cost_note` per top-N-largest candidate (previously it
   printed only path and size — "rebuildable" with no cost attached); the dashboard renders the
   same field next to the existing rebuild-instruction line.

## Consequences

- **`package_caches`'s default paths shrink** (HuggingFace hub, torch hub moved to
  `model_caches`; Ollama models added to `model_caches`, not previously covered at all). A
  `config.toml` that explicitly lists these paths under `[categories.package_caches]` keeps
  working (user-supplied `paths` isn't touched), but relies on built-in defaults will see those
  paths move category and tier on upgrade — this is the intended fix, not a regression, but is
  a behavior change worth calling out for anyone with an existing config relying on the old
  `package_caches` default path list.
- **The size guard is deliberately generic, not model-cache-specific** — it protects every
  current and future direct-delete category, not just this one. It fires rarely in practice
  today (model caches, the category most likely to trip it, now default to vaulted retention
  and so mostly never reach the guard at all) but exists as a backstop for the case a category's
  `retention_days=None` default turns out, on a given disk, to be wrong for one specific huge
  item even though it's right for the category in general.
- **A guard-downgraded item's manifest entry uses the guard's retention window
  (`direct_delete_size_guard_retention_days`), not the category's own `retention_days`
  (`None`).** `restore_batch` decides restorability from `entry.method`, not `entry.retention_days`
  — a guard-vaulted item has `method="vault"` and is fully restorable through the normal path,
  same as any other vaulted item; nothing new needed in `restore_batch` for this to already work
  correctly.
- **`_effective_method` was renamed `_effective_method_and_retention_days`** and now returns a
  `(method, retention_days)` pair instead of just `method`, since the guard case needs a
  retention window that isn't simply `candidate.retention_days`. Every call site (`apply_batch`)
  updated; no other module referenced the old name.

## Alternatives considered

1. **Keep model caches under `package_caches`, just flip `package_caches.retention_days` to
   `30` project-wide.** Rejected: forces every package cache (pip, npm, uv — genuinely free to
   redownload) into vaulted retention too, defeating the fast-direct-delete path ADR-0001 added
   specifically because these categories are expected to run often and automatically.
2. **Size guard scoped only to `package_caches`/`model_caches`, not every category.** Rejected:
   the actual risk (an expensive-to-redo item slipping through direct-delete) isn't unique to
   these two groups — `dev_artifacts`, `temp_and_browser_caches`, and `crash_dumps` all default
   `retention_days=None` too, and nothing about the guard's rationale is model-cache-specific.
   A general guard costs nothing extra for the common case (small files never reach it) and
   closes the gap for every category at once, present and future.
3. **Block model-cache candidates from Tier A via a config validator/runtime check instead of
   hardcoding `suggested_tier=Tier.B` in the detector.** Rejected: the existing tier formula
   already produces exactly this outcome for free once `suggested_tier=Tier.B` is set at the
   detector — a separate enforcement mechanism would be redundant complexity solving an already-
   solved problem.
