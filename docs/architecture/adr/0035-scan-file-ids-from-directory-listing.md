# 0035. The scan reads file IDs, sizes and times from the directory listing; per-tree aggregate rows were evaluated and not shipped

## Context

`scanner.build_record` called `os.stat()` once per entry. On Windows that is a CreateFile +
GetFileInformationByHandle + CloseHandle round-trip per file, paid only because
`os.scandir`'s `FindNextFile` data carries no file ID (`st_ino`) or volume serial (`st_dev`),
and hardlink identity (`check_hardlink_shared_active_install`, dedup's hardlink-aware reclaim
estimate, the apply-time identity re-verification) needs both. A full scan of the owner's
profile (5.2M entries) took 30 minutes on this machine.

Two levers were proposed (the originating analysis is not in the repo):

- **Lever 2**: get IDs/sizes/times from the directory listing itself.
- **Lever 1**: store one aggregate row per subtree (path, total size, file count, newest mtime)
  for trees Reclaim never deletes inside and never needs per-file identity for.

## Decision

**Lever 2 ships.** `reclaim/dirlist.py` enumerates a directory with
`GetFileInformationByHandleEx(FileIdExtdDirectoryInfo)` on one handle. For a plain file the
walk builds the `files` row straight from that data (`index.file_row`), with no per-file
syscall, `Path` or `FileRecord`. Still `os.stat()`-ed exactly as before: reparse
points/cloud placeholders (timeout-guarded), directories (their listed size is 0 versus
`os.stat`'s index-allocation size), and any entry whose 128-bit ID does not fit 64 bits.
Network/UNC roots, non-NTFS volumes and any listing error fall back to the original scandir +
`build_record` path per directory. The volume serial is read once per walk.

**Lever 1 does not ship** — no tree class could be proven safe to aggregate; see below.

## Consequences

**Equivalence, measured** (base `3deb05d` vs this change, same tree, `scan_tree` into a fresh
index, uv cache + `.venv` + HF hub, 456,044 rows): every column of every row equal except
one row's `attributes` (ARCHIVE vs NORMAL bit).

**The trade-off.** The listing is the NTFS directory entry, which NTFS refreshes lazily, so it
can differ from a live `os.stat()`. Measured for every non-reparse file of the whole
`C:\Users\gaura` profile (5,068,337 entries, 4,557,160 files):

| field | files that differ | what they are |
|---|---|---|
| `ino`, `dev` | 0 | |
| `size` | 110 (+1 hardlinked) | files open for write at scan time (browser WAL/session files, a wandb run, a uv temp file) |
| `mtime` | 168 (+1 hardlinked) | same |
| `attributes` | 47,493 | 46,984 by a `0x40000` bit `os.stat` adds and the directory entry (and `GetFileAttributesW`/scandir) lacks; 509 hardlinked ARCHIVE<->NORMAL flips |

Nothing reads those `attributes` bits (the index derives only reparse `0x400` and
recall-on-data-access `0x400000`, neither affected, and the risk classification already used
the directory-entry attributes). The real exposure is a file that is **open for write** showing
the size/mtime of its last close: an age-based detector can see it as older than it is, and a
dedup hash-cache entry keyed on `(size, mtime)` can be reused for content that changed. Both
are softened by apply-time behaviour (an open-for-write file usually cannot be moved or
deleted — sharing violation — and exact duplicates are vaulted, not permanently deleted), and
neither effect was observed beyond the 110/168 files above; it is a loss of exactness, not a
new class of deletion. If exactness matters more than the speed,
`scanner._USE_DIRECTORY_LISTING = False` restores the legacy path.

**Intended difference, as shipped: the listing for the walk, a live `stat` at the decisions.**
The owner's decision is to keep the listing for the walk (the speed) and to re-`stat` only the
*candidate set* at the points where size/mtime exactness decides something, via
`freshstat.fresh_stat_signature(path)` (a real `os.stat`, `None` when unreadable):

| decision point | where | re-`stat` applied to |
|---|---|---|
| temp-root min-age, incl. newest-mtime-of-subtree (#110) | `detectors.detect_temp_and_browser_caches` | only children the listing says are old enough; a file child itself, a directory child's files (all up to 2000, else the newest 2000 by listing mtime) |
| old installers `max_age_days` | `detectors.detect_old_installers` | only installers the listing says are old enough |
| large logs `stale_days` (and the size floor) | `detectors.detect_large_logs` | only logs the listing says are large and stale enough |
| dedup hash-cache reuse, keyed `(size, mtime)` | `dedup._hash_member` | only a would-be cache hit; a mismatch is a miss (rehash) |

Bound argument: the listing can only be stale toward an OLDER mtime / the last-close size (the
write has not reached the directory entry), so a stale listing can only turn "keep" into a wrong
"old"; entries the listing already keeps need no check. Everything below is therefore the
listing's "would be proposed / would be reused" set, never the index. A path that cannot be
`stat`ed carries no evidence of a live writer, so the listing's value stands. Residual: a
temp-root child directory with more than 2000 files re-`stat`s only its newest 2000 by listing
mtime, so an open-for-write file with an old listing mtime outside that window is not caught
there (the apply-time preflight identity check still compares live `(dev, ino, mtime)`). No
other detector compares an mtime to a threshold (`detect_crash_dumps`, `detect_archive_pairs`,
dev/package/model caches have no age rule). Separately, entries directly under the scan root are
always built by the legacy `os.scandir` + `os.stat` path (`scan_tree`'s top level), so they were
never affected.

**Speed** (interleaved base/new A/B, 3 repetitions each, on a live machine other sessions were
using; timings moved by 20-40% between repetitions, so only the interleaved ratios mean
anything). Walk only, SQLite writes stubbed out, 0.8M entries: base 67.5/67.7/78.2 s vs new
25.8/25.8/32.4 s, **2.6x**. End to end, full `C:\Users\gaura` profile (~5.0-5.2M entries): base
1813/1649/1736 s vs new 1344/1252/1270 s, **1.32-1.37x**; 0.8M-entry subtree set: base
165/128/129 s vs new 87/92/95 s, **1.36-1.90x** (median 1.39x); a small 55,563-entry tree
(`triage-iq`, one pair): 9.4 s vs 2.0 s. The index is unchanged in size (same rows and
columns). The gap between 2.6x and ~1.4x is SQLite: with the walk this cheap, writing the rows
(six secondary indexes plus the `scan_seen` temp table) is what the scan waits on. On the 0.8M
set, same-window A/B of the current writer (151.6/149.6 s) vs creating the indexes after the
scan instead of during it (64.7+15.2 and 66.1+14.3 s) shows the next lever is the write path,
not the walk; it is not part of this change.

For scale, per-class row counts of the profile (`C:\Users\gaura`, 5,020,798 entries; a row can
fall in several classes): `.git` 82,002 (1.6%), `node_modules` 622,151 (12.4%), `site-packages`
2,458,899 (49.0%), venvs 1,433,460 (28.6%), browser caches 692,425 (13.8%), uv cache 522,051
(10.4%), `__pycache__` 437,769 (8.7%), HF 4,126 (0.1%, but 236 GB). Even `.git`, the class
least entangled with deletion, is only 1.6% of rows.

## Lever 1: why no tree class is aggregated

An aggregate row replaces every per-file row under a path, so it is only safe if **no consumer
of per-file rows can tell**. The consumers, from reading the code at `3deb05d`:

| consumer | what it needs from per-file rows |
|---|---|
| `dedup.find_duplicate_clusters` via `ScanIndex.duplicate_size_candidates` / `_count` / `immaterial_duplicate_bucket_stats` | every non-directory, non-placeholder row of size > 0, **globally** — a file inside the aggregated tree is a cluster member, can be the *kept* copy (`select_keep` ranks git-repo membership first), and a `BLOCKED` member excludes its whole cluster |
| `detectors` (`files_by_name`, `files_by_ext`, `files_larger_than`, `files_matching_path_pattern`, `record_exists` manifest lookups) | rows at any depth, e.g. `detect_model_caches` matches `<root>/*<ext>` at any depth under the HF/torch roots |
| `detectors._reclaimable_bytes_for_candidate`, `executor._direct_delete_directory_mismatch` (`candidate_inventory(under=)`), `executor` entry-count guard (`subtree_entry_count`) | per-file `(dev, ino)` and path under every directory candidate: package/model/browser caches, `node_modules`, `__pycache__` are all directory candidates |
| `api.service` treemap (`direct_children`, `subtree_size_bytes`), `physical_size_bytes(full_inventory())`, `reconciliation` | hardlink-deduplicated totals across the **whole** index, and drill-down into any directory |
| `ai` analysis (`index.full_inventory(under=root)`) | every file, including images/documents inside caches |
| `scanner` incremental rescan (`load_stat_cache`, prune) | per-path `(size, mtime)` |

Classes evaluated:

- **`.git` internals**: every row has `git_repo_root` set, so `SafetyValidator` blocks them all
  (`REASON_IN_GIT_REPOSITORY`) and nothing is ever deleted there — but dedup still uses them as
  cluster members (a byte-identical copy elsewhere is currently proposed for deletion *because*
  the `.git` copy is kept), they count toward `physical_size_bytes`/treemap, and a git-adjacent
  hardlink is exactly the incident class. Not provably unaffected: left per-file.
- **`node_modules`**: a directory candidate with `_direct_delete_directory_mismatch` and
  `_reclaimable_bytes_for_candidate` reading its per-file rows, plus the in-repo
  clean/dirty logic in `SafetyValidator`. Per-file.
- **venvs / `site-packages` / uv & pip caches**: per-file by the owner's constraint (the
  hardlink-into-active-install check and dedup's cross-environment filter).
- **Browser profile caches, HF/torch/Ollama model caches, package caches**: directory
  candidates (above); model caches are matched by file extension at any depth. Per-file.
- **Protected roots** (Windows, Program Files): never deleted, but dedup still keeps a
  protected copy to justify deleting a user duplicate. Per-file.

Where aggregation would change anything observable, it is a candidate-set change (a lost
reclaim, or a different keep), and the task's rule is that an unprovable class stays per-file.
Per-file consumers are also global, so the claim "unaffected" would need a whole-index
byte-identity check at scan time, which is the cost aggregation exists to avoid.

## Alternatives

- **Aggregate `.git`/caches anyway** and accept the changed dedup/treemap/reclaim numbers:
  rejected under the stated hard constraint; would need an owner decision per class.
- **`NtQueryDirectoryFile` directly**: same data, private-ish API, no gain over
  `GetFileInformationByHandleEx`.
- **Cheaper per-file stat** (`GetFileInformationByName`, as CPython 3.13 does on newer
  Windows): exact and carries `nlink`, but still one syscall per file — it cannot approach the
  listing's amortized cost, and needs an OS-version fallback. Candidate if exactness for
  open-for-write files must be restored for plain files above some size.
- **Re-`stat` files modified in the last N hours** to bound the stale window: cheap and removes
  the common case, but not the long-open-file case; not done. The shipped variant re-`stat`s by
  *decision*, not by recency (see "Intended difference" above), which does cover a long-open file
  whenever it falls in the re-checked set.
