# 0023. Stage 2: safe mode as a structural safety boundary for the public installer

## Context

Every prior stage of this tool was built and measured against GG's own disk, by GG, with GG
watching the output. Stage 2 changes that: a public installer ships this tool to strangers who
"won't watch it" — won't read the dry-run report, won't recognize a misdetected category,
won't know to undo a batch. Every safety property this project has built so far (§7.5's
recommend-only AI gate, ADR-0001's retention model, ADR-0003's recovery-cost size guard,
ADR-0004's atomic vault moves, ADR-0009/0010's environment-detection hardening) assumes a
human reviewing output before it matters. A public release cannot assume that.

GG's own build instruction: safe mode must be a SAFETY BOUNDARY, not a flag — meaning it must
be provable the same way §7.5 is provable (a structural guarantee a test can assert against,
not a default value a code path could accidentally bypass), and it must be the DEFAULT for
every fresh install, with the tool's current full behavior demoted to an explicit, typed,
logged opt-in.

## Decision

### Mode is external state, not a config.toml field

`reclaim.mode` (new module) tracks the live mode via an append-only event log
(`data/mode_log.jsonl`, same shape/rigor as `executor.py`'s `manifest.jsonl`) — the live mode
is whatever the LAST entry's `to_mode` says, or `Mode.SAFE` if the log doesn't exist. Mode is
deliberately never read from `config.toml`: a hand-edited config file must never be the thing
that silently disables the safety boundary. `switch_to_power_mode(confirmation_text)` is the
ONLY way to enter power mode — it raises `ModeSwitchDeniedError` (mode unchanged, nothing
logged) unless `confirmation_text` exactly equals `"I understand this can permanently delete
files"` (case-sensitive, no fuzzy matching — a typo means not confirmed, never close enough).
`switch_to_safe_mode()` requires no confirmation: becoming more conservative is never the
dangerous direction.

### Three independent, structurally-enforced guarantees, not one flag

Deliberately layered — no single check is "the" safety boundary, matching this project's
existing defense-in-depth posture (§7.5's two-layer AI gate; `apply_batch`'s BLOCKED-refusal +
fresh-re-verify pair):

1. **Recycle-Bin-only, permanent-delete structurally unreachable.**
   `_effective_method_and_retention_days` (the ONE function `apply_batch` calls to decide a
   candidate's real method) checks `mode == Mode.SAFE` FIRST, before any other rule, and
   returns `("recycle_bin", candidate.retention_days)` unconditionally — regardless of the
   candidate's own `retention_days` (which would normally force `direct_delete` when `None`)
   and regardless of the batch's requested `method`. `apply_batch` additionally refuses the
   entire call immediately, before any I/O, if `mode=Mode.SAFE` and the caller requested
   anything other than `method="recycle_bin"` (`SafeModeViolationError`). The `vault` and
   `direct_delete` branches in `apply_batch`'s per-candidate loop are therefore unreachable by
   construction whenever `mode=Mode.SAFE`, not merely unreached in the cases tested — proven
   exhaustively in `evals/test_safe_mode_gate.py::test_safe_mode_never_produces_vault_or_
   direct_delete_method` (every `retention_days` value × every requested method, including the
   ADR-0003 size-guard-triggering case) and at the integration level by monkeypatching
   `os.unlink`/`shutil.rmtree` to raise if called and running a real `apply=True` batch. Same
   pattern extended to `purge.purge_expired`: it raises `SafeModeViolationError`
   unconditionally, before reading the manifest, whenever `mode=Mode.SAFE` — purge is always a
   permanent-delete of a vault copy, and a machine with real vault entries from an earlier
   power-mode session must not be able to purge them just because it's since switched back to
   safe.

2. **Dangerous categories forced off, independent of config.toml.**
   `SAFE_MODE_FORCED_OFF_CATEGORY_GROUPS = {duplicates, model_caches, dev_artifacts}` —
   `duplicates` (exact-duplicate detection reaches into environments, the same .venv-class risk
   ADR-0009/0010 exist to catch), `model_caches` (large, sometimes gated/unrecoverable
   checkpoints), `dev_artifacts` (node_modules/.venv/target — runtime-adjacent by definition).
   `config.load_effective_config` (new — deliberately separate from `load_config`, see below)
   forces these three `enabled=False` in the returned config's categories regardless of what
   config.toml requests, whenever the resolved mode is SAFE. Every other category
   (package_caches, temp_and_browser_caches, crash_dumps, old_installers, archive_pairs,
   large_logs) may still be enabled — safe mode restricts WHICH categories exist; guarantee 1
   above separately restricts HOW anything from an enabled category can ever be deleted.

3. **Recommend/review-only — no auto-delete, no batch-auto, for ANY category.**
   `detectors.generate_candidates` and `dedup.generate_duplicate_candidates` force every
   candidate to `Tier.B` unconditionally whenever `config.mode == Mode.SAFE` — checked ahead
   of, and independent of, the existing `Verdict`/category-enabled tier logic, so this
   guarantee does not depend on every detector's `suggested_tier` staying correct. At the API
   layer, `apply_selection` additionally refuses (`SafeModeViolationError` → HTTP 400) a
   blanket tier/category-group apply request with no explicit `paths` — the one-click "apply
   everything this tier matches" flow a dashboard button could otherwise offer. A safe-mode
   apply must always be an explicitly enumerated, human-picked list of paths.

### `load_config` vs. `load_effective_config`: why two functions, not one

The first implementation baked mode-resolution into `load_config` itself and broke ~240 of
this project's ~600 existing tests: every test that calls `load_config(path)` without an
explicit `mode=` (the overwhelming majority — this predates Stage 2 by the whole project) hit
`current_mode()`'s honest SAFE default and had `dev_artifacts`/`model_caches`/`duplicates`
silently stripped out from under it, even for tests with no interest in mode at all. Splitting
the concern into two functions — `load_config` (pure TOML parsing, behavior UNCHANGED, what
every existing test and internal call site keeps using) and `load_effective_config`
(mode-resolved, safety-policy-layered, what the CLI and dashboard call at their real entry
points) — fixed this without touching config.toml's semantics or the hundreds of tests that
were never about mode. Five genuinely mode-sensitive tests (`tests/test_cli.py`,
`tests/test_api.py`'s shared `_make_app` helper) were updated to explicitly seed a power-mode
log, since they were — now made explicit — testing this project's pre-Stage-2 "full" behavior
all along, not something mode-neutral.

### Dynamic mode switching without a server restart

`AppState.config` stores the RAW config (never mode-resolved); `AppState.effective_config` (a
property, re-derived on every access) and `AppState.live_mode` (same) re-read the mode-change
log fresh on every request. A mode switch via `POST /api/mode/power|safe` takes effect on the
very next request — no restart, no stale cached category/tier decisions from server startup.

### CLI method auto-resolution, not a hard failure

`reclaim apply`'s `--method` flag defaults to `vault`; naively, a plain `reclaim apply --apply`
under the default safe mode would immediately hit `SafeModeViolationError` on a flag the user
never touched. Both the CLI (`_run_apply`) and the API (`apply_selection`) instead auto-resolve
`method` to `"recycle_bin"` whenever the live mode is SAFE, so the common case just works.
`apply_batch`'s own structural check remains the real enforcement regardless — this is a UX
convenience layered on top, not a substitute for it.

### First-run screen

Shown once (`GET/POST /api/first-run`, a marker file — not a log, since "acknowledged" is a
one-way transition), gates the rest of the dashboard behind an overlay stating: what the tool
does, that deletes go to the Recycle Bin in safe mode, how to restore (Windows' own Recycle
Bin, no extra tooling), which categories stay off by default and why, that power mode is
opt-in/reversible, the residual-risk disclaimer (below), and a link to the LICENSE.

## Consequences

- **Residual risk, stated honestly, not closed.** Safe mode's category/tier/method guarantees
  are exhaustively provable — they are not a statement that every remaining enabled category's
  DETECTION LOGIC is perfect for every environment. A rebuildable-cache false positive
  (package_caches/temp_and_browser_caches/crash_dumps/old_installers/archive_pairs/large_logs
  all remain available in safe mode) is still possible; safe mode's job is ensuring any such
  mistake is Recycle-Bin-recoverable and always human-confirmed, never that it can't occur.
  This is the same "provably safe boundary, not provably correct detection" distinction §7.5
  already draws for the AI layer.
- **Power mode is exactly today's behavior, now demoted to opt-in.** GG's own existing
  workflow, after this branch merges, requires one `reclaim mode power --confirm "..."` (or
  the dashboard equivalent) to keep working unchanged — a deliberate, disclosed cost of making
  safe mode the genuine default rather than a parallel, easy-to-forget-to-enable path.
- **`purge_expired`'s safe-mode refusal is currently redundant with guarantee 1** (a safe-mode-
  only manifest never contains `vault` entries in the first place, so there is structurally
  nothing for it to purge) — kept anyway as an explicit, independently-testable second layer,
  closing the case of a machine with real vault entries from an earlier power-mode session.
- **Test isolation cost, paid once.** `tests/test_api.py::_make_app` now pre-seeds an isolated
  power-mode log for every test using it — a small, one-time, well-justified change (see
  above), not a pattern this ADR expects to recur for future features.

## Alternatives considered

- **Single boolean flag on `Candidate`/`apply_batch` call sites** (`safe: bool`), checked
  ad hoc wherever relevant. Rejected: exactly the "flag, not boundary" shape GG's instruction
  explicitly ruled out — easy for a future call site to forget, no single place to prove the
  guarantee holds.
- **Mode stored in `config.toml`.** Rejected: a config file is exactly the kind of state a
  well-meaning or malicious edit could silently flip, defeating the entire point of a default
  a stranger "who won't watch it" can rely on without reading anything.
- **Baking the category override into `load_config` directly.** Tried first, reverted — see
  "why two functions" above; broke the existing test suite's separation between "what does
  config.toml say" and "what should actually happen given the live mode."
