# 0007. Keep-heuristic safety and cluster review

## Context

ADR-0006 made `exact_duplicate`'s reclaimable-size estimate honest (23.09GB hardlink-aware
reclaimable vs. 27.19GB logical, across 18,708 candidates on the real-disk run). Content
survival was never the risk there — every cluster member is byte-identical by construction
(BLAKE3 full-hash match), so deleting any non-kept member can never lose *data*. The risk is
narrower and sharper: **keeping the wrong copy**. A cluster's "keep" pick before this ADR used
`select_keep`'s ranking of `(not-Downloads/Temp bool, ctime, path-depth, lexicographic)`. That
first key is a *negative* signal (absence of Downloads/Temp) with no way to distinguish a
genuine in-project/git-repo copy from an arbitrary other location — a copy sitting in, say,
`Documents/backup` ties with a copy inside an active git repository on that check alone and
falls through to ctime/depth, which could pick the `Documents/backup` copy as keep and propose
the git-repo copy for deletion. That is exactly the failure this ADR closes: deleting the
in-project/under-git copy while stranding the sole survivor in an incidental location.

Two related gaps: (1) the kept copy itself was never re-validated — a cluster could pick a
Downloads/Temp/cloud-placeholder copy as keep while discarding a more durable copy elsewhere,
stranding the only survivor somewhere worse than what was thrown away; (2) a cluster where a
*non-kept* member sits under a SafetyValidator-protected path (a git repo, project source) was
handled member-by-member — that one member alone would be BLOCKED, but the rest of the cluster
would still be proposed, which is misleading: the "duplicate total" for that cluster no longer
reflects what's actually deletable, and a human skimming the review queue could miss that a
protected copy is quietly sitting in the same cluster.

## Decision

1. **`_location_rank(record) -> int`** — a three-way, positive-first ranking used as
   `select_keep`'s primary sort key, ahead of path depth and ctime: `0` = inside a git repository
   / active project (`record.git_repo_root is not None`), `1` = neither in a git repo nor in
   Downloads/Temp (a generic, undetermined location), `2` = inside Downloads/Temp. Full ordering
   for `select_keep`: **(1) location rank, (2) shortest path depth, (3) oldest ctime, (4)
   lexicographic path** as a final deterministic tiebreak. Git-repo membership is now a positive,
   first-class preference — a git-repo member is never outranked by a non-git, non-Downloads/Temp
   member, closing the exact gap described above.
2. **`cluster_needs_manual_review(cluster) -> bool`** — re-validates the *kept* path, not just
   the deleted ones. True when the kept copy sits in a risky location (Downloads/Temp or a
   cloud-sync placeholder — `_is_risky_sole_survivor_location`) while at least one about-to-be-
   deleted member sits somewhere more durable. A cluster like this has every surviving candidate
   forced to Tier B (manual review), overriding `config.categories.duplicates.enabled` — auto-
   apply is never allowed to strand the sole survivor somewhere less durable than what it threw
   away.
3. **Whole-cluster exclusion on a protected member.** `generate_duplicate_candidates` now
   evaluates every non-kept member's `SafetyValidator` verdict up front; if *any* member is
   `BLOCKED` (e.g. `REASON_IN_GIT_REPOSITORY`, `REASON_PROTECTED_SYSTEM_ROOT`), the **entire**
   cluster is excluded from candidates — not just that one member — and logged via
   `dedup.cluster_excluded_protected_member` with the blocked paths. This keeps the review queue
   honest: if a cluster shows up at all, every member in it is something the pipeline would
   actually propose, not a partial view with a silently-skipped protected file still sitting
   alongside it.
4. **Dashboard review endpoint** — `GET /api/duplicate-clusters/review` (`service.
   list_duplicate_cluster_review`) returns the 15 largest exact-duplicate clusters (configurable
   via `?limit=`) ranked by hardlink-aware `reclaimable_bytes`, each with the full keep-vs-delete
   member list, the `needs_review` flag, and the same rationale text the candidate itself
   carries. It reuses `generate_duplicate_candidates`'s own filtering rather than recomputing
   safety logic, so this view can never show a cluster the apply pipeline would refuse to touch.
   The dashboard's Review Queue tab renders this above the existing tier/category-filtered
   candidate list, reusing the pre-existing `renderClusterTable` component (already used per-
   candidate) so keep/delete rows render identically in both places.

## Consequences

- **Honest reachability gap in `cluster_needs_manual_review`.** Given `select_keep`'s new
  location-rank ordering, a Downloads/Temp copy can only win as keep if *every other* cluster
  member is also Downloads/Temp — in which case there's no more-durable member being discarded,
  and the function correctly returns `False` regardless. Given `ScanIndex.
  duplicate_size_candidates`'s pre-existing `is_cloud_placeholder = 0` SQL filter (unrelated to
  this ADR — already there before it), a cloud-placeholder file never becomes a cluster member at
  all. So this function's `True` branch is **not reachable end-to-end** via
  `generate_duplicate_candidates` on the codebase as it stands today. It's verified correct at
  the unit level directly (`test_cluster_needs_manual_review_when_keep_is_risky_and_a_deleted_
  copy_is_stable`, built against a hand-constructed `DuplicateCluster`) and kept as defense-in-
  depth: if either upstream guarantee (the location-rank ordering, or the cloud-placeholder SQL
  filter) is ever loosened, the review-flag mechanism is already in place to catch the case it's
  designed for. This is stated plainly rather than glossed over — no fabricated confidence about
  a scenario that can't currently be constructed end-to-end.
- **Real-disk validation performed before any apply.** Confirmed the exact scenario the fix
  targets: a cluster with one copy inside a git repository and one under Downloads keeps the
  repo copy and proposes the Downloads copy for deletion (`test_select_keep_prefers_git_repo_
  over_downloads`, plus the full-pipeline `test_generate_duplicate_candidates_keeps_git_repo_
  copy_over_downloads`); a cluster with a protected non-kept member is excluded in its entirety
  (`test_generate_duplicate_candidates_excludes_whole_cluster_when_non_kept_member_is_blocked`).
- **No apply of `exact_duplicate` is authorized by this ADR.** This ADR only changes what gets
  *proposed* and how it's surfaced for review — the actual real-disk apply still requires the
  side-by-side review output (produced separately, against the current `data/real-disk-run`
  index) and explicit go-ahead.
- **Scope.** This ADR only changes `dedup.py`'s keep-heuristic/cluster-safety logic and adds one
  read-only API endpoint plus its dashboard rendering — no changes to `executor.py`, retention
  tiers, or the hardlink reclaim-estimate math from ADR-0006.

## Alternatives considered

1. **Keep the plain "not Downloads/Temp" boolean and add a separate git-repo tiebreak after
   ctime/depth.** Rejected: this would still let a non-git, non-Downloads/Temp location (e.g.
   `Documents/backup`) win over ctime/depth before the git-repo signal is ever consulted — the
   whole point is making git-repo membership a *positive*, high-priority signal, not a
   tiebreaker several steps down the chain.
2. **Silently drop the one protected member from a cluster instead of excluding the whole
   cluster.** Rejected: this would report a smaller, technically-"safe" duplicate total for that
   cluster while hiding that a protected copy is sitting right next to the ones proposed for
   deletion — worse for review transparency than simply not showing the cluster at all until a
   human understands why a member is blocked.
3. **Build the dashboard review feature as a client-side re-sort of the existing `/api/
   candidates` response instead of a dedicated endpoint.** Rejected: `/api/candidates` returns one
   row per non-kept *file*, not one row per *cluster* — reconstructing per-cluster
   `reclaimable_bytes` totals and dedup'ing rows client-side would duplicate the exclusion/
   ranking logic the backend already owns. A dedicated endpoint keeps that logic in one place.

## Test coverage

- Unit: `_location_rank` orders git-repo (0) < neither (1) < Downloads/Temp (2);
  `select_keep` prefers git-repo over Downloads and over a plain non-git/non-Downloads location
  (checked both member orderings); shorter path depth beats older ctime; lexicographic order as
  final tiebreak.
- `cluster_needs_manual_review`: True when keep is risky and a deleted member is stable; False
  when keep is already stable; False when every member (keep and deleted) is risky.
- Full pipeline: git-repo copy kept over a Downloads copy, with the rationale text confirming
  why; a cloud-placeholder file never reaches a cluster at all (documents the pre-existing SQL
  filter rather than silently omitting the scenario); a cluster with a protected non-kept member
  is excluded in its entirety, not just that one member.
- API: `/api/duplicate-clusters/review` returns the keep/delete side-by-side shape for a real
  duplicate pair, returns an empty list before any scan, and 400s on `limit < 1`.
