# 0004. Long-path-safe, atomic-or-nothing vault/restore moves

## Context

The first scoped real-disk apply (`windows_temp`, `package_cache`, `crash_dump_file`,
`crash_dump_wer_report`, `browser_cache`, `dev_artifact_pycache` — see `PLAN.md`'s 2026-07-17
checkpoint) hit a genuine data-integrity gap in the vault mechanism. One guard-routed
(ADR-0003) directory — a chat-session scratch tree with 5,064 directories and 27,403 files —
failed to vault with `WinError 3`. Root cause, confirmed by inspecting both sides afterward:

1. `shutil.move`'s destination path (`<vault_dir>/<batch_id>/<uuid32hex>_<name>/...`) is always
   *longer* than the source, because it adds a batch directory and a UUID-prefixed name on top
   of the original path. For a tree already close to Windows' legacy 260-character `MAX_PATH`
   limit, this pushed some deeply-nested files over the limit.
2. `shutil.move` falls back to `copytree`+`rmtree` whenever `os.rename` can't be used (which,
   without long-path support, is exactly when a resulting path is too long). `copytree` failed
   partway through, and because `rmtree(src)` only ever runs *after* `copytree` succeeds, the
   source was left fully intact — no data was lost — but the destination held an **incomplete,
   orphaned partial copy** (1,705 of the original 5,064 directories) with no manifest entry
   pointing at it, silently consuming vault disk space with no automated way to notice or clean
   it up.
3. Empirically confirmed on the target machine (no Windows `LongPathsEnabled` opt-in): even a
   bare `os.makedirs`/`open()` on a >260-character path fails without an explicit `\\?\`
   extended-length-path prefix, and succeeds with one.
4. ADR-0003's size guard makes this **more** likely to recur, not less: it specifically routes
   the *largest, deepest* candidates to `vault` (protecting exactly the items most expensive to
   lose), and those are the ones most likely to already be near the path-length limit before the
   vault prefix even gets added. The next planned apply (exact-duplicate + model-cache
   candidates, including a 124.9GB HuggingFace hub tree) has a much larger blast radius for the
   same failure mode.

## Decision

1. **`\\?\`-prefixed paths everywhere in the vault/quarantine/restore path.** A new
   `_long_path(path) -> str` helper returns an absolute, all-backslash, `\\?\`-prefixed string
   (`\\?\UNC\...` for UNC paths). Every raw filesystem call in `executor.py` — `os.rename`,
   `os.walk`, `os.path.getsize`, `shutil.copytree`/`copy2`, `shutil.rmtree`, `os.unlink`,
   `os.makedirs`, `os.path.exists`/`isdir`/`listdir` — now operates on this prefixed string, not
   a bare `Path`/string. `pathlib.Path` does not reliably round-trip a `\\?\`-prefixed string
   (it tries to parse it as a UNC-style root and mishandles the literal `?` segment), so this
   section of the module deliberately uses `os.*`/string paths throughout rather than `Path`
   methods — every resulting `ruff` PTH-rule finding in that section is an intentional,
   documented exception, not an oversight.
2. **`_atomic_move(src, dst, *, is_dir)` replaces the raw `shutil.move` calls** in both
   `apply_batch`'s vault branch and `restore_batch`'s move-back. Guarantee: either the move
   fully succeeds, or `src` is left completely untouched with **zero** orphaned bytes *and* zero
   leftover empty directory shells at `dst`. Tries an atomic `os.rename` first (now almost always
   usable, since both paths are long-path-safe) — a single filesystem metadata operation that
   either fully succeeds or raises with nothing changed. Only falls back to a manual
   copy-verify-delete sequence if `os.rename` raises (e.g. a genuinely cross-volume `vault_dir`).
3. **Post-copy integrity check, file-count and total-bytes parity.** In the copy-fallback path,
   `src` is walked and stat'd for `(file_count, total_bytes)` *before* the copy, and `dst` is
   walked and stat'd the same way *after* — a mismatch (whether from an exception mid-copy, or a
   copy that returns normally but is silently incomplete) raises `VaultIntegrityError`, cleans up
   the partial `dst`, and leaves `src` untouched. `src` is only ever removed once parity is
   confirmed. `VaultIntegrityError` is caught by the same broad per-item exception handler as any
   other filesystem error — one item's integrity failure is recorded as a failed item, never
   aborts the rest of the batch.
4. **`_atomic_move` owns creating (and, on failure, cleaning up) `dst`'s parent directory**,
   rather than the caller `mkdir`-ing it first. If this call is the one that speculatively
   created the batch subdirectory and the move then fails, the empty parent is removed too — a
   directory made just for one item shouldn't outlive that item's failure as debris. A parent
   already shared with other, already-succeeded siblings in the same batch (`vault_dir/batch_id/`
   with real content from other items) is left alone; only genuinely empty parents are removed.
5. **Restore gets the identical treatment.** `restore_batch`'s destination-exists check and
   move-back both operate on `\\?\`-prefixed paths, and use the same `_atomic_move` (with
   src/dst reversed) — restoring a long original path has the exact same MAX_PATH exposure in
   reverse, and deserves the same atomicity/integrity guarantee. Added a dedicated test building
   a directory tree whose full path exceeds 260 characters and proving a full vault → restore
   round trip is byte-identical — the pre-ADR-0004 restore test only ever used short paths.
6. **Same-path duplicate candidate suppression.** Unrelated in mechanism but discovered by the
   same real apply: `detect_crash_dumps` proposes a `.dmp` file both by extension (anywhere in
   the inventory) and, when it sits directly under a configured CrashDumps/WER root, again as
   that root's direct child — two independent `RawCandidate`s for the same real file under two
   different `category` labels. Neither data loss nor a vault-move concern (the file gets deleted
   either way), but whichever candidate is applied second finds the file already gone and is
   recorded as a spurious failure. `generate_candidates` now runs `_dedupe_by_path` (first-seen
   wins, by resolved path) right after combining every detector's output and before
   `_drop_nested_candidates` — a general, detector-agnostic guard, not a `detect_crash_dumps`-
   specific patch, so any other detector introducing the same kind of overlap in the future is
   covered automatically.

## Consequences

- **`_effective_method` was already renamed `_effective_method_and_retention_days` under
  ADR-0003; no further rename needed here** — `_atomic_move` is a new, separate primitive that
  `apply_batch`/`restore_batch` call instead of `shutil.move` directly.
- **The risky `copytree`+`rmtree` fallback is now rarely exercised in practice** — most moves are
  same-volume and now succeed via the atomic `os.rename` fast path regardless of depth, since
  both paths are long-path-safe. The fallback still exists (and is directly tested via
  monkeypatching, since a real cross-volume setup isn't reproducible in CI) for genuinely
  cross-volume `vault_dir` configurations, where an atomic rename is fundamentally impossible
  regardless of path length.
- **A `VaultIntegrityError` is a new failure mode a batch report can surface** — distinct from a
  plain `OSError`, so a future report/dashboard enhancement could label parity failures
  distinctly from permission/lock errors if that turns out to be useful; today it's caught and
  reported identically to any other per-item failure.
- **No change to the manifest schema, `restore_batch`'s restorability rules, or `apply_batch`'s
  public signature** — this is entirely an internal-mechanism hardening; every existing
  behavioral contract (dry-run touches nothing, `retention_days=None` still direct-deletes
  below the size guard, a `VaultIntegrityError`/`OSError` item failure doesn't abort the batch)
  is unchanged and covered by the pre-existing test suite, which passed unmodified throughout
  this change.

## Alternatives considered

1. **Just catch the `WinError 3` and treat it as an ordinary item failure, without the atomicity
   rework.** Rejected: this was already what happened (the failure WAS recorded and didn't crash
   the batch) — the actual problem is the orphaned partial vault copy left behind, which a plain
   catch-and-record does nothing to clean up.
2. **Shorten the vault path (e.g. a shorter `vault_dir`, no UUID prefix) instead of adding
   long-path support.** Rejected: reduces the odds of hitting the limit but doesn't eliminate it
   — any sufficiently deep source tree (a large `node_modules`, a deeply-nested project
   structure) can already be close to 260 characters at its *original* location, independent of
   whatever the vault adds on top. Long-path support fixes the actual limit, not just this one
   symptom of it.
3. **Enable `LongPathsEnabled` via the Windows registry as a setup prerequisite instead of code
   changes.** Rejected: requires an admin-elevated one-time machine change this tool has no
   business making on its own, and doesn't help on a machine where a user hasn't (or can't) set
   it — the `\\?\` prefix works unconditionally, with no environment prerequisite.
