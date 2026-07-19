# 0014. Gold-set labeling protocol: stratified sampling, diagnosable labels, tracked balance

## Context

Before running real labeling, GG asked for the protocol to be confirmed against four
statistical-soundness requirements, having not yet run the tool. Auditing the ADR-0013 version
against those questions found four real gaps, not just documentation gaps:

1. **Sampling.** `discover_label_candidates` called only `image_similarity.
   build_near_identical_clusters`, which — via `cluster_by_hamming_distance` — transitively
   groups pairs within one threshold and explicitly drops every singleton with no near-dup
   partner. The tool therefore only ever proposed already-clustered, positive-leaning
   candidates. It never surfaced a pair just outside the threshold, nor a pair from clearly
   distant images. A gold set built this way is a set of near-duplicates with a handful of
   in-pool rejections — exactly the shape that cannot locate a decision boundary, because
   there is no labeled data on the far side of it.
2. **Label schema.** `LabelDecision` captured the binary duplicate/not-duplicate decision and
   `keep_path`, but had no field for *why* a given member was chosen as the keeper. Without a
   reason code, a future eval can measure top-1 disagreement between the classical scorer and
   GG's choice, but not diagnose which sub-signal (sharpness/resolution/exposure) was wrong.
3. **Volume + balance.** No target existed anywhere — no total, no per-class minimum, no
   in-tool visibility into the running balance. Nothing would have stopped a session from
   producing 400 confirms and 3 rejections.
4. **Persistence/versioning.** `LabelDecision` had no `commit_sha` and no `schema_version`.
   Candidate generation logic (thresholds, bin boundaries, keep-best weights) can change
   between labeling sittings; without a recorded commit, a later eval can't tell which code
   version proposed a given labeled candidate.

## Decision

**Three sampling strata, not one.** `discover_label_candidates` now proposes from:
- `near_duplicate` — the existing Feature 1a cluster-discovery pipeline's own output
  (unchanged; still needed for keep-best labeling, which requires 2+ genuinely similar images
  to compare).
- `boundary` — independent pairwise sampling for pairs with Hamming distance in
  `[_BOUNDARY_MIN_DISTANCE, _BOUNDARY_MAX_DISTANCE]` = `[11, 25]`, deliberately straddling
  both sides of the cluster-discovery threshold (15). These are the examples where the
  current threshold's correctness is most uncertain, and therefore most informative to label.
- `negative_control` — independent pairwise sampling for pairs with distance
  `>= _NEGATIVE_CONTROL_MIN_DISTANCE` = `26`, expected to almost always be labeled
  non-duplicates. Confirming that expectation gives clean true-negative ground truth; a
  surprising "yes, these are duplicates" result on a negative-control pair would itself be a
  significant, actionable finding.

Boundary/negative-control sampling is genuinely independent of the cluster-discovery pool — it
computes real pairwise Hamming distances directly, bins them, and samples up to `per_stratum`
(default 60) per bin deterministically (seeded). A pair already covered by a near_duplicate
cluster candidate is excluded from re-sampling (`_cluster_pair_keys`). This is O(n²) over the
scanned image set; `max_images_for_boundary_sampling` (default 800) deterministically
subsamples before that step only, so a large photo collection doesn't hang the tool — cluster
discovery itself still runs against the full scanned set regardless.

**Reason codes, human-selected, never auto-derived.** `KEEP_REASON_OPTIONS` — `sharper`,
`higher_resolution`, `better_exposure`, `better_framing_or_content`, `other` — are presented as
checkboxes at the moment GG selects a keeper. They are never pre-filled from the classical
scorer's own sub-scores: the point is an independent signal to check the scorer against, not a
tautological one. `other` has no free-text companion, deliberately — free text risks capturing
something not intended to be written to a file that (while never committed) still lives on
disk.

**Progress tracked and displayed, not enforced.** `compute_progress` folds the label store to
latest-per-cluster (same semantics as `labeled_cluster_ids`) and reports total count plus
per-stratum counts against `DEFAULT_TARGET_TOTAL = 300` and
`DEFAULT_TARGET_PER_STRATUM_MINIMUM = 40`. Rendered at the top of the review page on every
load, with an explicit "targets not yet met" / "✅ targets met" state and the reasoning spelled
out in the UI itself ("a gold set of mostly confirmed positives can't locate a decision
threshold"). Not enforced — GG can stop whenever he judges the session sufficient — but no
longer invisible.

**Every decision is commit-keyed and schema-versioned.** `LabelDecision.commit_sha` is stamped
via `reclaim.ai.eval_harness.current_commit_sha()` at label time (real `git rev-parse HEAD`,
never a placeholder — verified live: a real label written during this ADR's own verification
recorded the exact 40-character SHA of the commit that produced it).
`LabelDecision.schema_version` (currently `1`) is written on every line so a future format
change can be detected and migrated rather than silently misread. `LabelStore.read_all()`
defaults every new field when reading an older line missing it, so schema evolution doesn't
invalidate historical labels — though as of this ADR nothing has been labeled yet, so there is
no legacy data this matters for today.

## Consequences

- The default targets (300 total, 40 minimum per non-near_duplicate stratum) are a starting
  point, not a statistically derived requirement — GG may judge more or fewer are needed once
  he sees the real distribution of candidates in his own photo library. The tool surfaces the
  numbers; it doesn't claim they're sufficient for any particular confidence level.
- `_BOUNDARY_MIN_DISTANCE`/`_BOUNDARY_MAX_DISTANCE`/`_NEGATIVE_CONTROL_MIN_DISTANCE` are
  reasonable, disclosed choices given `max_hamming_distance`'s default of 15 — not derived from
  data (none exists yet). If the real gold set shows the boundary is actually elsewhere, these
  bin edges are cheap to adjust before a future labeling session.
- `per_stratum`/`max_images_for_boundary_sampling`/`seed` are all CLI flags
  (`scripts/ai_label_tool.py`) — the default sampling behavior is not hardcoded and invisible.

## Test coverage

`tests/test_ai_labeling.py::test_discover_label_candidates_covers_all_three_strata` exercises
the real bucketing logic against real computed Hamming distances between synthetic images (not
a hand-picked fixture asserting a specific count) — it only asserts that more than one stratum
is represented, which is the actual property that matters. `test_progress_*` (3 cases) cover
totals, per-stratum minimums, and the latest-decision-wins fold on relabeling. `tests/
test_ai_labeling_app.py` gained cases for reason-code capture and rejection of an invalid
reason. Verified live (chrome-devtools, this session): a synthetic 16-image directory produced
2 near_duplicate clusters, 4 boundary pairs (distance 24, straddling the 15-threshold), and 10
negative_control pairs (distance 28-40) in one run; confirming a keeper with two reason codes
selected wrote a label recording both reasons, the correct stratum, and a `commit_sha` that
matched the real repository HEAD at write time; reloading the page showed the progress summary
update from 0/300 to 1/300 with `near_duplicate: 1`, persisted correctly across the reload.
