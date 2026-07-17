# 0006. Hardlink-aware reclaimable-size estimate

## Context

Purging the stranded 14.3GB `uv/cache` vault copy (ADR-0005) measured a real, unignorable gap:
the manifest's logical size said 14,276,488,979 bytes, but the actual `shutil.disk_usage`
before/after delta was only 5,596,594,176 bytes — 5.21GB, not 14.3GB. The most plausible
explanation: `uv` uses hardlinks (or copy-on-write, but this machine's filesystem/tooling makes
hardlinks the likely mechanism) to share cache blobs with the live virtual environments it
installed into, so deleting the cache's own name for a blob just drops that blob's link count —
the underlying disk blocks stay allocated as long as any other name (an installed venv's own
copy) still points to them.

This isn't a one-off curiosity specific to `uv`. `exact_duplicate` is the same mechanism in
reverse, and structurally guaranteed to hit it: its entire selection criterion is byte-identical
content (BLAKE3 full-hash match), and byte-identical content sharing the same disk blocks is
*exactly* what a hardlink produces. A "duplicate" that's actually a hardlink to the kept copy —
created by a tool that deliberately hardlinks instead of copying (uv/pip's wheel cache
extraction, some backup/sync tools, `git checkout` in some configurations) — was being reported
as fully reclaimable when deleting it frees nothing at all. The already-reported 48GB logical
duplicate total (the first successful real-disk dry run) cannot be trusted at face value until
this is corrected. The same exposure applies to the still-on-hold `model_cache`/HuggingFace-hub
apply even more severely: HF hub's `blobs/`/`snapshots/` layout is *entirely* link-based by
design (snapshot entries point into the shared blob store), so a naive size sum there would
overstate reclaimable space by construction, not just by coincidence.

## Decision

1. **New `linkinfo.py` module**: `read_link_identity(path) -> LinkIdentity | None` reads a
   file's real Windows hardlink identity — `(st_dev, st_ino, st_nlink)` — via a direct
   `os.stat()` call (never a cached `DirEntry.stat()`, which does not populate `st_ino`/`st_dev`
   on Windows; the same gotcha this project's scanner already hit once, see `PLAN.md`).
2. **`estimate_reclaimable_bytes(candidates: Sequence[tuple[Path, int]]) -> dict[Path,
   ReclaimEstimate]`**: given a set of candidate paths being considered for deletion *together*,
   groups them by `(dev, ino)` identity. An inode reachable via a single name (`nlink == 1`)
   counts its full logical size as reclaimable. An inode shared by multiple candidates in the
   set: if every name pointing to it is present in the input (candidates sharing it == that
   inode's own `nlink`), its size counts *once* (credited to one member, `0` for the rest of the
   group) — summing every returned estimate gives the correct total without the caller needing
   to know about grouping. If some name *outside* the set still holds a link (fewer sharing
   candidates than `nlink`), the whole group is `0` — this is the general mechanism behind "a
   duplicate that's actually a hardlink to the kept copy": the kept file's own name is exactly
   that external, uncounted link. An unresolvable path (vanished, permission error) is
   conservatively `0` (`resolved=False`) — never a guess in either direction, since the entire
   point of this fix is to stop overclaiming.
3. **`Candidate.reclaimable_bytes: int | None`**: `None` means "not computed for this category"
   — every category defaults to this and a caller must treat `None` as "assume equal to
   `size_bytes`," never as a claim of zero. Populated only for `exact_duplicate`
   (`dedup.generate_duplicate_candidates`) today: each cluster's non-keep members run through
   `estimate_reclaimable_bytes` (bounded by cluster size — a handful of `os.stat()` calls per
   cluster, never a whole-inventory operation), and a member whose estimate comes back `0` gets
   an explicit rationale addendum ("already deduplicated at the filesystem level... 0 bytes
   reclaimable"), not a silently-smaller number with no explanation.
4. **Reporting shows both numbers, never blended.** `reclaim apply`'s CLI report gains a
   dedicated `exact_duplicate reclaim estimate: logical=X, hardlink-aware estimated
   reclaimable=Y` line (only printed when duplicates are in the selection), plus a per-candidate
   "estimated reclaimable" annotation in the top-N-largest listing whenever it differs from the
   logical size. The dashboard's `CandidateOut` gains the same `reclaimable_bytes` field. The
   existing `apply_batch`/`purge_expired` real, measured `disk_free_delta_bytes` (before/after
   `shutil.disk_usage`) is untouched by this ADR — it was already the authoritative ground truth
   for what actually happened; this ADR is about making the *pre-apply estimate* honest before
   the real thing runs, not replacing a measurement that was already accurate.

## Consequences

- **Scope: this ADR wires hardlink-awareness into `exact_duplicate` only**, not every category.
  `model_caches` (HuggingFace hub) needs the same capability — explicitly noted as required
  before that apply ever runs (see the existing hold in `PLAN.md`/ADR-0005's follow-up), reusing
  this same `linkinfo` module — but that work stays on hold pending the separate link-*structure*
  verification (preserving symlink/hardlink topology through a vault move, not just estimating
  size) already recorded there; this ADR does not start it. Directory-based categories
  (`windows_temp`, `package_caches`, `dev_artifacts`, `crash_dumps`) are NOT retrofitted with a
  recursive per-file hardlink scan here — computing this for a whole candidate *directory*
  would mean walking and stat'ing every file inside it at report time, a cost this project has
  already been burned by underestimating once (the whole-inventory-materialization stalls this
  session root-caused earlier). `exact_duplicate` is exempt from that concern because it already
  stats/hashes every candidate file individually as part of its normal operation — the hardlink
  check adds one more `os.stat()` per file already being touched, not a new whole-tree walk.
- **`bytes_freed` (executor.py) and `disk_free_delta_bytes` are deliberately NOT changed to use
  `reclaimable_bytes`.** `bytes_freed` answers "how many bytes did this specific file/move
  operation account for," which is genuinely `size_bytes` regardless of hardlinks — the
  hardlink question is about what happens to *disk-wide free space* afterward, which
  `disk_free_delta_bytes`'s real, measured before/after already answers honestly. Conflating the
  two would have replaced one honest number with a different, no-more-honest estimate.
- **The 48GB logical duplicate figure from the first real-disk dry run is now explicitly
  superseded** — the corrected run (this same day) is the first trustworthy number for that
  category; see `PLAN.md`'s checkpoint for the actual measured logical-vs-reclaimable split.

## Alternatives considered

1. **Trust `apply_batch`'s real `disk_free_delta_bytes` measurement alone and skip a pre-apply
   estimate entirely.** Rejected: that measurement only exists *after* the destructive operation
   already ran — the whole point of a dry-run report is to inform the decision *before* running
   anything for real, and a logical-size-only estimate that's off by 3x (as the uv/cache case
   demonstrated) actively misleads that decision.
2. **Use `ctypes`/`GetFileInformationByHandle` directly instead of `os.stat()`.** Rejected:
   Python's `os.stat()` on Windows has populated `st_ino`/`st_dev`/`st_nlink` correctly via
   `GetFileInformationByHandle` since Python 3.5 — reaching for raw `ctypes` would add
   complexity and a maintenance surface for a capability the standard library already provides.
3. **Scan the whole inventory at index-build time and cache `nlink` as a new index column.**
   Rejected: hardlink counts can change between scan and apply/report time (another process
   creates or removes a link), so a cached value would just be a different kind of stale
   estimate; a live `os.stat()` at report time on the bounded candidate set is both more accurate
   and — for the categories this ADR actually wires in — cheap enough not to need caching.
