# Reclaim: what it costs to let an agent delete files

Reclaim is a Windows disk-cleanup tool: scan a drive, propose what's safe to remove, delete it.
That one-line description hides the actual engineering problem. An agent that deletes files for
a living has exactly one job that matters more than finding space to reclaim: never destroying
something the user needed, and never lying about what it did. Every mechanism in this codebase —
the safety validator, the dry-run default, the retention tiers, the reclaimable-size accounting —
exists because a wrong number is embarrassing and a wrong delete is unrecoverable. This is the
record of what it actually took to earn that guarantee against a real, 3.1-million-file disk in
active daily use, not a fixture tree.

## Architecture

- **Rules-first.** No ML in the deletion path. Every candidate comes from a deterministic
  detector (path pattern, extension, byte-identical hash) or an explicit heuristic — nothing is
  ever proposed because a model scored it "probably junk."
- **SafetyValidator-first.** Every candidate, from every detector, passes through one
  deny-first validator before it can be tagged for any tier — protected roots, git repositories,
  protected extensions, database/VM files, Docker/WSL roots, cloud-sync placeholders. `BLOCKED`
  means excluded entirely, not degraded to a lower tier.
- **Dry-run by default.** `apply=False` makes zero filesystem calls — no moves, no deletes, no
  manifest writes. `--apply` is the only way anything on disk changes, ever.
- **Atomic, recoverable moves.** Vault and Recycle Bin quarantine are both real filesystem
  operations with parity checks before the source is ever removed — a partial move never leaves
  an orphaned half-copy and an intact source, or vice versa.
- **Tiered retention.** Permanence is a property of the *category*, not the run: rebuildable
  caches (`dev_artifacts`, `package_caches`, `temp_and_browser_caches`, `crash_dumps`) delete
  permanently because their real recovery path was always the rebuild command, never the vault;
  everything else — including every exact-duplicate candidate — is recoverable by construction.
- **Hardlink- and structure-aware reclaimable accounting.** Byte-identical content is not the
  same as reclaimable space. A "duplicate" can be a hardlink to its own keep copy (0 bytes
  freed), a hardlink to a shared blob (0 bytes freed), or a stdlib file inside a shared Python
  interpreter build that a dozen unrelated projects depend on (deleting it breaks all of them,
  regardless of what bytes it shares with something else).

## The honesty arc — the centerpiece

The single throughline of this project is how many times the "how much can we reclaim" number
turned out to be wrong, and specifically wrong in the direction of overclaiming — never once
did a correctness fix increase the estimate.

| stage | exact_duplicate reclaimable estimate | what changed it |
|---|---|---|
| first real-disk dry run | ~48GB (logical size, unexamined) | baseline: sum of every non-kept cluster member's size |
| ADR-0006 | 23.09GB | hardlink-aware accounting — a "duplicate" sharing blocks with its own keep copy reclaims 0, not its full size |
| ADR-0008 | 4.26GB | model-cache and conda/venv-environment exclusion — byte-identical stdlib/model files inside a structurally-managed cache or a live environment are never safe to treat as arbitrary duplicates, regardless of what they share on disk |
| real apply (same day, natural disk drift) | 4.24GB selected | not a correction — the live disk had changed slightly between estimate and apply time |
| ADR-0009, after the real apply | 3.92GB, net of 186 restored files | a live incident found the ADR-0008 exclusion didn't cover *standalone* Python installations (no `conda-meta/`, no `pyvenv.cfg`) — closed, verified against the entire applied batch |

Each drop is a correctness fix, not a change of heart about what counts as "reclaimable." The
last one happened *after* the apply had already run for real — see below.

## The 11-bug trail

Four themes, each with real evidence a fixture-only test suite could not have produced.

**Observability — a silent hang looks exactly like a hang.**
1. The first real-disk dry run stalled with zero output for over 10 minutes: no heartbeat, no
   incremental hash-cache writes, no per-file read timeout. A single locked file, or simply a
   long-running hash pass, was indistinguishable from a crash. Fixed with a 5-second heartbeat,
   500-file write flushes, and a 30-second per-file timeout that converts a stuck read into a
   recorded skip instead of a wedge. Every unit test was green — none of them ran against enough
   files, or a long enough pass, to notice silence itself was the bug.

**Scalability — four separate ways "works on 500 fixture files" doesn't mean "works on 3.1M
real ones."**
2. Candidate generation materialized the *entire* scan index into `FileRecord` objects before
   any detector ran — 21+ minutes and ~4.9GB RSS with zero rows hashed. Fixed by pushing every
   detector query down into indexed SQL (ADR-0002); verified via `EXPLAIN QUERY PLAN`, not
   assumption.
3. The dedup pipeline did the same thing one level down: it collected an entire duplicate-size
   candidate bucket into memory before hashing a single file. Fixed by processing one size
   bucket at a time (bounded by the largest single bucket, not the total candidate count).
4. `direct_children()`'s `LIKE ? ESCAPE '\'` query silently defeated SQLite's index — a bare
   3.1M-row table scan on every call, 1,309 seconds for one detector alone. An `ESCAPE` clause
   unconditionally kills the LIKE-to-index-range optimization; rewritten as a prefix-range
   comparison (`path >= 'x/' AND path < 'x0'`), needing no escaping at all. 346x speedup,
   confirmed by query plan, not benchmark vibes.
5. `_drop_nested_candidates` re-scanned an entire `list` of kept directories for every candidate
   — O(candidates × kept_dirs × depth) — invisible until a real disk produced tens of thousands
   of non-nested sibling directories. A `set` and an inverted containment check made it
   O(candidates × depth): 3.5+ minutes (never finished) to 1.55 seconds.

**Selectivity — the 80% collision number was mostly noise.**
6. 2.49 million of 3.12M files on the real disk shared a size with at least one other file — a
   number that looked like it demanded hashing most of the disk. Querying the actual
   distribution showed it was dominated by 333K zero-byte files and a long tail of tiny sizes
   that could never reclaim anything material even in the best case. A materiality gate
   (`(member_count - 1) × size` must clear a 1MB floor before a bucket is even queried) turned a
   disk-I/O-bound multi-hour pass into one that skips the noise entirely.

**Honesty — logical size is not reclaimable size, and "byte-identical" is not "safe to
delete."**
7. The first vault-quarantine measurement showed a genuine 0-byte disk-free delta for a real
   200KB move — expected, not a bug (same-volume moves don't free space), but it meant the
   project's own top-level success criterion ("reclaims ≥30GB, verified via before/after
   disk-free measurement") was structurally unreachable under "no permanent delete in v1."
   Resolved by making retention a per-category property (ADR-0001): categories whose only real
   recovery path was always "rebuild it" get permanent deletion; everything else stays
   recoverable.
8. `.cache/huggingface/hub` (124.9GB) classified as a generic `package_cache` → permanent
   delete — wrong, because re-acquiring 100+GB is not `npm ci`, and a gated or fine-tuned model
   may not be re-downloadable at all. Split into its own `model_caches` category: vaulted,
   never Tier A, cost-aware regardless of size (ADR-0003).
9. The uv/cache purge measured a 3x gap between logical size and real freed space: 13.30GB
   logical, 5.21GB real disk-free delta. uv hardlinks cache blobs into live virtual
   environments — deleting the cache's own name for a blob just drops a link count, not the
   shared blocks. `exact_duplicate` was exposed to the identical mechanism in reverse:
   byte-identical content is exactly what a hardlink produces. Fixed with hardlink-aware
   accounting (ADR-0006) — a candidate credits its full size only if every name pointing to its
   inode is in the same delete set.
10. A side-by-side review of the largest duplicate clusters (ADR-0007) found the keep-heuristic
    could pick a Downloads-folder copy over a git-repository copy of the same file, and a
    structural review (ADR-0008) found `exact_duplicate` proposing deletion of HuggingFace
    blob/snapshot pairs and one conda environment's own binaries in favor of another's —
    byte-identical for structural reasons that have nothing to do with waste. Fixed with a
    location-ranked keep-heuristic, whole-cluster exclusion on a protected member, and
    model-cache/cross-environment exclusion, dropping the estimate from 23.09GB to 4.26GB.
11. **The exact_duplicate apply ran for real — and broke this project's own development
    environment within minutes.** `socket.py` and 185 other files were recycle-binned from three
    shared Python interpreter installations (a uv-managed build, `gcloud`'s bundled Python, the
    Android NDK's toolchain Python) because none of them are a conda environment or a venv — no
    `conda-meta/`, no `pyvenv.cfg` — so the ADR-0008 protection never recognized them as
    environments at all. A first, keyword-driven recovery pass found and restored 71 files and
    looked complete; it wasn't. A systematic re-audit — re-running the fixed detector against
    every one of the 10,134 applied files — found 186 true violations, not 71. All 186 were
    recovered from the Windows Recycle Bin by parsing its `$I`/`$R` index format directly (the
    project's own restore command doesn't support Recycle Bin batches). Fixed by adding a
    third, tool-agnostic marker to the environment detector: `python.exe` + a sibling `Lib/`
    directory, the structural signature of *any* complete Python installation, independent of
    which tool produced it (ADR-0009).

Bug 11 is the whole thesis in one incident: the estimate had already been corrected twice
(bugs 9 and 10) by the time this ran, the pre-apply review had explicitly checked for exactly
this class of risk and found zero violations, and it still happened — because the check that
existed was precise about the environments it knew to look for, and this was one it didn't.
Recycle Bin, chosen specifically for its recoverability and not because anything was expected to
go wrong, is why the incident is a paragraph in a case study instead of a rebuilt machine.

## Honest metrics

| metric | value | source |
|---|---|---|
| Real disk-free reclaimed (measured, before/after `shutil.disk_usage`) | **36,216,430,592 bytes — 33.73GB** | sum of three real applies' own before/after deltas: `data/real-disk-run/real_apply_report.txt` (10,270,556,160), `redo_real_apply.txt` (20,349,280,256), `purge_real_apply.txt` (5,596,594,176) |
| exact_duplicate, pending (Recycle Bin, not yet real free space) | 4,205,571,147 bytes — 3.92GB | `data/real-disk-run/final_reconciliation.txt` — net of 186 files restored per ADR-0009; real only once the Recycle Bin is emptied, disclosed separately, never summed into the figure above |
| exact_duplicate candidates applied | 10,134 succeeded / 10,247 selected / 113 failed (all explained: access-denied, file-in-use, long-path, vanished-in-race) | `data/real-disk-run/exact_duplicate_real_apply.txt` |
| Estimate corrections, same category | 48GB → 23.09GB → 4.26GB → 3.92GB | ADR-0006, ADR-0008, ADR-0009 |

Every number above traces to a specific file in `data/real-disk-run/` produced by the actual
run it describes — none of them is a recomputation or a rounded restatement. Nothing here is a
final total: `model_caches` and several other reviewed categories remain unapplied by design,
and the 3.92GB sitting in the Recycle Bin stays a pending number, not a freed one, until someone
empties it.
