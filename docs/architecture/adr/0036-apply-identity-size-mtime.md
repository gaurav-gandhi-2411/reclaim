# 0036. Apply-time identity also compares a file's size and mtime; the regenerable tier re-stats before every unlink

## Context

Two earlier statements disagreed about `apply_batch`'s preflight: "compares only `(dev, ino)`;
size/mtime are logged" versus "compares live `(dev, ino, mtime)`" (ADR-0035 said the latter).
Measured on `origin/main` `3f42f93`, scan -> change -> `apply_batch(apply=True)` of one flagged
file (same inode), three modes:

| mode / method | unchanged | in-place append | mtime-only `os.utime` |
|---|---|---|---|
| power / direct_delete | deleted | **deleted** | **deleted** |
| power / vault | vaulted | **vaulted** | **vaulted** |
| safe / recycle_bin | trashed | **trashed** | **trashed** |

The first statement was true: `preflight.check_identity_unchanged_since_scan` computed
`identity_changed` from `(st_dev, st_ino)` only; `live_mtime`/`live_size_bytes` were diagnostics.
(Its module comment even listed same-inode in-place edits as a deliberately out-of-scope gap.)

A file whose size or mtime moved since the scan is, by definition, in active use or has been
edited. Deleting it acts on a stale picture of the disk.

A second gap, found while wiring the API test: `POST /api/apply` with explicit `paths` regenerates
rule candidates from the index, where the ADR-0035 decision-point re-stat drops a file modified
after the scan; the path then fell into `_build_user_selected_candidate`, which built its
`(dev, ino, mtime)` baseline from a FRESH stat taken moments earlier, so any check trivially
matched itself.

The regenerable tier (ADR-0034: `POST /api/clean/regenerable`, `reclaim auto-clean`) does not use
`apply_batch` or scan records at all: it walks, judges "newest content older than the age floor",
then deletes later, guarded only by a share-mode-0 open-handle probe.

## Decision

1. **FILE candidates are skipped on any change of live size or mtime versus the scan record**,
   in addition to `(dev, ino)`, with a new `PreflightSkipReason`
   `size_or_mtime_changed_since_scan`, distinct from `identity_changed_since_scan` (a swapped
   inode still reports the latter; identity is checked first). It is one `os.stat` per candidate
   (the identity check already paid it). The reason reaches `ItemApplyResult.skip_reason`,
   `ItemApplyResultOut.skip_reason` (the schema reuses the `PreflightSkipReason` literal, so the
   OpenAPI enum follows), the CLI (`SKIPPED: <path> -- <reason>` on stderr instead of
   `FAILED: <path> -- None`), the dashboard's result list, and MCP `delete`
   (`DeleteResult.skipped_by_reason`, a per-reason count; MCP never enumerates paths).
2. **Comparison is exact float equality** on `st_mtime`, the same test `index.is_unchanged`
   already uses. Both scan paths store the same float: the `os.stat` path stores `st_mtime`; the
   NTFS directory-listing path (ADR-0035) stores `dirlist.filetime_to_unix_seconds`, which mirrors
   CPython's FILETIME conversion bit for bit (`tests/test_dirlist.py` pins it against `os.stat`).
   SQLite `REAL` is float64 and round-trips it exactly.
3. **Directories keep the existing tiering unchanged** (top-level `(dev, ino)`; the M1 subtree
   re-walk for direct-delete directories). A directory's mtime moves on any child change and would
   false-skip. `Candidate.mtime == 0.0` means "no baseline" (hand-built fixture) and disables the
   size/mtime half only.
4. **`_build_user_selected_candidate` baselines from the scan index row** when the scan saw the
   path (fresh record only for a path the scan never indexed).
5. **Regenerable tier re-stats immediately before each unlink**: after the open-handle probe,
   `lstat` again and skip (counted in `files_skipped_in_use`, path recorded) when size or mtime
   differs from the walk's stat, when the entry changed type, or, for the aged TEMP / crash-dump
   paths, when the live mtime is younger than `env.min_age_seconds` relative to `env.now()`.
   Reparse entries (junction/symlink) get the same re-stat before the link is removed.

## Consequences

- **One choke point, proven.** CLI `reclaim apply` (`cli.py:867`), `POST /api/apply`
  (`routes.py:539` -> `service.run_apply` -> `service.py:1491`), MCP `delete`
  (`mcp/server.py:413` -> `service.mcp_execute_delete` -> `service.py:1614`) and the dashboard
  one-click flow (`/api/clean/one-click-summary` -> flattened paths -> `POST /api/apply`) all call
  `apply_batch` (`executor.py`), whose per-candidate `_preflight_skip_reason` carries this check.
  `tests/test_apply_size_mtime.py::test_every_entry_point_runs_the_same_preflight_and_reports_the_new_reason`
  runs all four against one fixture with a spy on `_preflight_skip_reason`.
- **False-skip rate, measured** (`scan_tree` into a fresh index, then every non-directory row
  compared with `check_identity_unchanged_since_scan` straight away): `%LOCALAPPDATA%\npm-cache`,
  98,076 files, 12.2 s scan + 8.6 s compare: 0 identity changes, 4 size/mtime changes, 0 of them a
  rounding case (size equal and |delta mtime| < 1 ms: 0). All 4 are npm debug logs still held open
  by a live process (`check_file_in_use` is true for each): the directory listing showed their
  last-close size 0 and an older mtime, the live `stat` shows 1209-1217 bytes. That is the ADR-0035
  stale-listing effect and apply skips them fail-closed; the in-use probe would have skipped them
  anyway. A 1,943,872-file scan of `C:\Users\gaura\ml-projects` (396 s scan, 249 s compare while
  other sessions were editing) saw 25 size/mtime and 20 identity changes, the examples inspected
  being real growth of live logs and notebooks; not usable as a false-skip rate because the tree
  was changing under it.
- **Behavior change to expect.** A user-selected (AI Suggestions) file edited after the last scan
  is now skipped until the next scan; previously it was applied with a self-matching baseline.
- A stale listing for a file open for write skips it (fail-closed). A file Reclaim itself touched
  between scan and apply (none known) would skip too.
- The regenerable tier's age floor is not applied to browser caches (they have no age rule, only
  the closed-browser gate; existing tests clean 0.1-day-old cache files), so their re-stat only
  compares against the walk's own stat.
- Residual: the re-stat is still a TOCTOU window of microseconds before the unlink; the
  share-mode-0 probe plus the OS's own sharing violation on delete remain the last line.
- MCP `delete` failures that are skips are now distinguishable (`skipped_by_reason`), where before
  `files_failed` carried no reason.

## Alternatives

- **Compare with a tolerance** (e.g. 2 s, FAT-style): rejected; both stores hold the identical
  float, so the extra slack would only let a quick edit through. Measured rounding suspects: 0.
- **Compare size only, or mtime only**: rejected; a size-preserving overwrite changes only mtime,
  and an `os.utime` restore after an append changes only size. Both are tested separately (and fail
  separately under mutation).
- **Fold into `identity_changed_since_scan`**: rejected; a swapped inode and an edited file are
  different situations for a user reading a skip, and ADR-0001-style recovery advice differs.
- **Add size/mtime to directory candidates**: rejected, false skips on any child change; the M1
  subtree re-walk already covers direct-delete directories' contents.
- **Pass planned per-file size/mtime from the regenerable walk's first pass**: the plan is an
  aggregate (`_TreeInfo`), not per-file; re-statting at unlink against the delete walk's stat plus
  the age floor against the live clock covers the same window without a second per-file table.
