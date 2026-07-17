# 0008. Model-cache and environment exclusion from exact_duplicate

## Context

ADR-0007's side-by-side review of the real disk's 15 largest exact-duplicate clusters (17.46GB
of the 23.09GB hardlink-aware total) surfaced two classes of cluster that are byte-identical by
BLAKE3 hash but structurally unsafe for `exact_duplicate` to touch at all:

1. **HuggingFace-hub blob/snapshot pairs** (clusters 1, 6, 9, 10, 13 and similar): the same model
   file appearing once under `.../hub/models--org--name/blobs/<sha>` and once under
   `.../hub/models--org--name/snapshots/<rev>/.../<name>`. HF Hub's cache design stores one
   logical object as a content-addressed blob plus a human-readable revision tree that normally
   *symlinks* back to the blob — deleting the snapshot side would normally be harmless (the blob
   still holds the content, and HF's own tooling can re-link). But if the two are separate
   physical copies instead of a symlink pair, deleting the snapshot orphans the object: the
   snapshot path is what application code actually reads (`from_pretrained`, `snapshot_download`
   return snapshot paths, not blob paths), so removing it breaks model loading even though the
   blob file is still sitting there.
2. **Conda/venv cross-environment duplication** (clusters 5, 11, 12, 15): the same CUDA DLL
   appearing in the conda base environment's `Lib/site-packages`, in a named env's own
   `envs/aetherart/Lib/site-packages`, and/or in conda's `pkgs/` extraction cache. Conda and venv
   environments intentionally carry their own copy of shared dependency binaries for isolation —
   deleting one environment's own copy because a *different* environment happens to carry a
   byte-identical copy can break that environment's DLL resolution.

Both classes share the same shape: `exact_duplicate`'s selection criterion (byte-identical
content) is agnostic to *why* two files are byte-identical, but here the "why" is structural and
load-bearing — the duplication is intentional infrastructure, not waste.

### Investigation: is this a `linkinfo.py` hardlink-detection blind spot?

The hypothesis this halt raised: since `estimate_reclaimable_bytes` (ADR-0006) did not flag the
HF blob/snapshot pairs as already-deduplicated (`reclaimable_bytes == 0`), maybe `st_nlink`
doesn't see a real link between them — a reparse point Windows' hardlink accounting is blind to.
Checked directly against the real disk, for all of clusters 1, 6, 9, 13 (`os.stat()` on both
sides of each pair):

| pair | blob inode | snapshot inode | nlink (each) | reparse point (either side) |
|---|---|---|---|---|
| sdxl-turbo unet | `7318349394998913` | `844424931457734` | 1 / 1 | No |
| controlnet-sd21-canny | `1970324837474804` | `1688849860763625` | 1 / 1 | No |
| BAAI/bge-base-en-v1.5 | `37999121856038986` | `844424931374035` | 1 / 1 | No |

**Result: the hypothesis is disproven.** These are genuinely separate inodes with `nlink == 1`
and no `FILE_ATTRIBUTE_REPARSE_POINT` on either side — not a hardlink, not a symlink, not a
junction. `linkinfo.py`'s `st_nlink`-based accounting is *correct* here: it reports the full
logical size as reclaimable because that's genuinely true — deleting the snapshot file really
would free real, non-shared disk blocks. The real cause: `huggingface_hub` normally creates the
snapshot entry as a symlink to the blob, but symlink creation requires either Administrator
privilege or Developer Mode's `SeCreateSymbolicLinkPrivilege` — neither is available on this
machine, so the library's fallback path performs a real file copy instead. The safety problem
isn't a broken hardlink estimate; it's that `exact_duplicate` shouldn't be evaluating these two
paths as independent, arbitrary duplicates at all, regardless of whether they share disk blocks.

## Decision

1. **Blanket model-cache-root exclusion.** Any path under a configured
   `config.categories.model_caches.paths` root (HF hub, torch hub, Ollama — ADR-0003) is never
   eligible to be an `exact_duplicate` deletion candidate, full stop. Model-weight caches are
   reviewed as one unit under the `model_caches` category's own cost-aware, review-only handling
   (ADR-0003); `exact_duplicate` must never reach into that space from the side.
2. **Structural HF blob/snapshot detection, independent of configured roots.**
   `_hf_cache_object_root(path)` recognizes the HF cache's own naming convention
   (`models--*`/`datasets--*`/`spaces--*` directories containing `blobs/`, `snapshots/`, or
   `refs/`) and excludes any matching path even if the HF cache happens to live somewhere other
   than the configured default (a relocated `HF_HOME`, for instance). This is a **blanket
   per-path exclusion**, not narrow blob-paired-with-snapshot detection: any single path matching
   the structure is excluded on its own, which is a strict superset of "exclude the pair" and
   simpler to reason about and verify.
3. **Conda/venv cross-environment exclusion.** `_environment_root(path)` identifies the Python
   environment a path lives inside (conda base, a named `envs/<name>`, or a `venv`/`.venv`) via
   the `Lib/site-packages` layout both conda and Windows venvs use. A duplicate is excluded if
   its environment root differs from the cluster's kept member's environment root — i.e. never
   propose deleting one environment's own copy to keep a different environment's copy. Conda's
   own `pkgs/` extraction cache is deliberately **not** treated as a live environment (despite
   having the same internal `Lib/site-packages` layout) — it's a package-manager cache, exactly
   as safe to reclaim as any other package cache (`package_caches`), and protecting it here would
   be over-broad. This exclusion is per-member relative to the kept copy, not whole-cluster: a
   `pkgs/`-cache duplicate in the same cluster as an excluded cross-environment duplicate is
   still proposed normally.
4. **Both exclusions are applied per-member, before safety evaluation or tiering**
   (`_dedup_ineligibility_reason`, in `generate_duplicate_candidates`) — an excluded path is
   filtered out of `cluster.duplicates` entirely and never reaches `SafetyValidator.evaluate()`
   or gets a tier; it simply isn't an `exact_duplicate` candidate. A cluster left with zero
   remaining duplicates after filtering contributes nothing. Each exclusion is logged
   (`dedup.member_excluded`, with a `reason` field) for observability.
5. **`linkinfo.py` defensive addition (not the fix for this specific bug, but a related gap
   worth closing while here).** `LinkIdentity` gains `is_reparse_point`, read from the same
   `os.stat()` call already made (`st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`, no
   extra syscall). `estimate_reclaimable_bytes` now treats a path reporting `nlink == 1` AND
   `is_reparse_point == True` as unresolved (`resolved=False`, `reclaimable_bytes=0`) rather than
   confidently claiming its full logical size — a real Windows hardlink is never a reparse point,
   so this never fires for the mechanism this module was built around; it guards against a
   different, adjacent risk (e.g. Windows Server Data Deduplication's chunk store, which uses a
   reparse point and would report `nlink == 1` despite sharing storage through a mechanism
   `st_nlink` cannot see at all).

## Consequences

- **Real, measured drop in the exact_duplicate reclaimable total.** Re-running the estimate with
  both exclusions applied: **23.09GB → see the re-run report** (recorded in
  `data/real-disk-run/` alongside this ADR) — the lower number is the honest one; the difference
  is exactly the HF blob/snapshot and cross-environment bytes that were never safely reclaimable
  through this category.
- **`model_caches` and `package_caches` are unaffected** — HF/torch/Ollama paths and conda
  `pkgs/` cache paths remain fully reclaimable through their own categories with their own
  review/retention policies; nothing is silently lost from consideration, only redirected to the
  category actually responsible for it.
- **Scope boundary, stated honestly:** `_environment_root`'s `Lib/site-packages` detection
  targets the Windows conda/venv layout this real disk exhibits; a fundamentally different
  environment layout (e.g. a non-Windows-style venv) would not be recognized and would fall
  through to ordinary duplicate handling. This is a known, documented boundary, not a silent gap.
- **The reparse-point addition to `linkinfo.py` is confirmed NOT the fix for the reported bug**
  (see the investigation table above) — it's shipped anyway as cheap, harmless defense-in-depth
  for a distinct risk class, and is explicitly not conflated with the real root cause in the
  commit history or this ADR.
- **No apply of `exact_duplicate` is authorized by this ADR.** The revised top-15 review (with
  both exclusions applied) still requires a clean pass and explicit go-ahead before any real
  apply runs.

## Alternatives considered

1. **Narrow blob-paired-with-snapshot pairing detection instead of a blanket per-path
   exclusion.** Rejected: a blanket exclusion is a strict superset (catches every case pairing
   detection would, plus any HF-cache path that happens to cluster with something outside the
   cache), is simpler to implement and verify, and requires no bookkeeping to associate two
   specific paths as "the same pair."
2. **Treat `pkgs/` as environment-private too, alongside `envs/` and the base install.**
   Rejected: `pkgs/` is conda's own package-manager cache, not a live environment — flagging it
   as protected would strand real, safely-reclaimable duplicate bytes behind an overly broad
   rule, contradicting the whole reason `package_caches`/`model_caches` exist as separate,
   rebuildable-cache categories in the first place.
3. **Skip the `linkinfo.py` reparse-point change since the investigation showed it isn't the
   cause here.** Rejected: it's a one-`if`-branch, zero-extra-syscall defensive addition against
   a real (if currently inactive on this machine) Windows storage-sharing mechanism `st_nlink`
   genuinely cannot see — cheap enough to ship as belt-and-suspenders while documenting plainly
   that it didn't explain the reported symptom.

## Test coverage

- Unit: `_hf_cache_object_root` matches blob and snapshot paths under the same repo dir to the
  same root, returns `None` for unrelated paths; `_is_model_cache_path` matches a configured
  root OR the HF structure independent of configured roots; `_environment_root` identifies conda
  base, a named env, and excludes `pkgs/`; `_is_cross_environment_duplicate` is true only across
  different live environments, false for same-environment or `pkgs/`-cache pairs;
  `_dedup_ineligibility_reason` returns the right reason (or `None`) in priority order.
- Full pipeline: an HF blob/snapshot pair produces zero candidates regardless of the
  keep-heuristic's pick; a base-env/pkgs-cache/named-env trio produces exactly one candidate
  (the `pkgs/` copy) — the named-env copy is excluded, the `pkgs/` copy is not.
- `linkinfo.py`: a path reporting `nlink == 1` and `is_reparse_point == True` (via monkeypatch —
  creating a real reparse point needs privilege this dev machine's own investigation found
  unavailable) is reported `resolved=False`, `reclaimable_bytes=0`, never a confident full-size
  claim.
