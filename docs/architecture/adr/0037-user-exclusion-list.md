# 0037. User exclusion list: projects that no cleanup may ever touch

## Context

The owner declared three projects permanently off-limits to every Reclaim cleanup (their trees,
worktrees, venvs, envs, data, models, caches, and TEMP scratch directories whose NAME embeds the
project, e.g. `%TEMP%\claude\C--Users-<u>-ml-projects-<project>`). The product must honour such a
list on every delete path and be able to prove it. Those names are the owner's configuration, never
a product default.

Reading the code first (probe results and tests: `tests/test_exclusions.py`): `[safety] deny` already reached everything that
goes through `SafetyValidator.evaluate` (detectors, duplicate members, `_build_user_selected_candidate`,
the executor's direct-delete re-check, the purge re-check). It did NOT reach (1) the regenerable tier
(ADR-0034 -- no `SafetyValidator`, its own closed-world code path), (2) duplicate keeper selection
and hashing (an excluded file could be the surviving "keeper" that justified deleting another copy,
and was read and hashed), (3) a directory candidate that merely CONTAINS an excluded path (deleting
the directory deletes the excluded content), (4) `apply_batch` for a vaulted candidate that bypassed
generation (only direct-delete candidates were re-checked). Purge already aborted the whole run
for an excluded-origin entry (ADR-0001's re-check) and still does.

## Decision

- New `[exclusions] project_names = [...]` (default empty). `config.exclusion_patterns(config)` =
  `[safety] deny` + one `*<name>*` glob per name (case-insensitive substring on the whole path, so
  worktrees `<name>-wt-x`, `envs/<name>`, and scratch dirs embedding the name are all covered). One
  list, one matcher (`safety.first_matching_pattern`), used everywhere.
- `SafetyValidator` blocks (`USER_EXCLUSION`) a path matching, and for a directory also one that
  has any entry beneath it matching (name-only `os.scandir` walk, only when patterns exist).
- `apply_batch` (real runs) re-checks every candidate, vaulted or direct-delete, against the
  exclusions (whole-batch `SafetyInvariantError`) -- last line of defence independent of generation.
- Dedup: excluded files are dropped from size buckets before any hashing, so they are never read,
  never cluster members, never a keeper. They are treated as if they did not exist: no other copy is
  proposed because "a copy survives in the excluded tree". Chosen over "keep them as keepers"
  because the owner's directive is that nothing may be decided relative to them.
- Regenerable tier: `RegenerableEnv.excluded_patterns`. A top-level temp/crash entry is skipped whole
  if it, or anything beneath it, matches (so `%TEMP%\claude` holding an excluded scratch directory is
  not deleted wholesale); reported as `excluded` entries `"<path> :: <pattern>"`; a root/cache dir
  that itself matches is `skipped_excluded`. Native tool cache roots and browser cache dirs are fixed
  but checked too. Cost of the whole-entry rule: the sibling content inside such a parent is also not
  cleaned while the excluded child exists.
- `reclaim auto-clean` (the weekly task reads the installed `config.toml`) and the dashboard
  one-click both pass the config's patterns -- the parameter is required, no default.
  `auto-clean --json` carries `excluded` and `excluded_applied`; `excluded_applied` is
  `|applied_paths ∩ excluded|` (`regenerable.count_excluded_applied`) and a non-zero value is a hard
  failure (exit 1).
- Purge: unchanged. An eligible vault entry whose original path is excluded fails the fresh
  re-check and aborts the whole purge run, deleting nothing (ADR-0001). The weekly auto-clean never
  touches the vault (ADR-0034), so it cannot purge such an entry.
- Restore is unchanged: it writes the user's own file back and is not a cleanup.

## Consequences

- A purge run stays blocked while any eligible vault entry came from an excluded path; restore that
  entry or remove the exclusion to unblock it.
- A name is a substring token: `intent-router` also excludes `~/Downloads/intent-router-notes.pdf`.
  Deliberately over-inclusive for a hard exclusion; use a precise `[safety] deny` glob to be narrower.
- Config is read at startup by the dashboard (`AppState.safety`): editing exclusions needs a restart.
  The CLI and the weekly task read it on every run.
- Directory candidates pay one name-only subtree walk when any exclusion is configured (zero cost by
  default). Not measured on a large real tree.
- Not covered: a path reaching Reclaim only through a junction whose target is excluded is judged on
  the resolved path for `SafetyValidator`, but the regenerable tier deliberately does not follow links.

## Alternatives

- Hardcode the three names as defaults: rejected, they are the owner's choice.
- Prune excluded directories in the scanner: rejected, the index is persistent and incremental (stale
  rows from before the exclusion would survive) and the treemap would silently under-report disk use.
- Treat excluded files as valid keepers: rejected, see Decision.
