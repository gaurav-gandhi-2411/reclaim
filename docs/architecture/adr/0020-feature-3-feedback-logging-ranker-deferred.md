# 0020. Feature 3: feedback-logging only — LambdaMART ranker deliberately deferred

## Context

GG's build instruction: "build ONLY Feature 3's feedback-logging (defer the LambdaMART
ranker; drop Track B). Log every accept/reject/keep decision with its feature vector (size,
ext, path-class, mtime/ctime, cluster stats, category, cloud-sync flag, sibling-decision
context — NO atime). Local, versioned, commit-keyed, same persistence discipline as the
labeling tool. The ranker stays a documented, label-gated future step (activates at ≥500 real
decisions, time-split eval) — do NOT build or ship it now; there's no data to train on. Cold-
start remains the transparent heuristic, clearly labeled non-ML."

Per spec §4 ("Feature Tier 3 — Feedback-Driven Clutter Prioritization"): this is deliberately
NOT a model shipped on day one. A supervised importance/staleness model with zero labels is
the fabricated-confidence trap the rest of this project's ADRs (0012, 0016) exist to prevent.
Logging decisions now and training a ranker later, evaluated on held-out future decisions, is
the statistically valid path.

## Decision: build the feedback store + cold-start heuristic; do not touch LightGBM

**Built:**
- `src/reclaim/ai/feedback_store.py` — `FeedbackStore` (append-only JSONL decision log),
  `FeatureVector`/`ClusterStats`/`SiblingDecisionContext` (the training-row schema),
  `classify_path_class`, `record_feedback_decision`.
- `src/reclaim/ai/cold_start_priority.py` — `compute_cold_start_priority`, a transparent,
  documented, non-ML heuristic (`size × mtime-staleness × location-weight ×
  cluster-membership`, per spec §4's own formula) that orders the review queue until the
  ranker activates.

**Not built, on purpose:** any LightGBM import, any training code, any model artifact. No
`lightgbm` dependency was added to `pyproject.toml`. There is no code path today that could
even attempt to train a ranker — the label threshold (`FeedbackStore.count() >= 500`, spec
§4's number) is documented here and in the module docstrings as the gate a *future* PR must
implement, not a constant checked anywhere in this codebase yet. `AITrack.RANKED_CLUTTER`
(Feature 3's eventual review-queue track, already reserved in `models.py` since ADR-0011)
remains unimplemented — nothing in this PR emits it.

**Track B (Feature 1a semantic/CLIP grouping) is also explicitly dropped from this pass**,
per GG's instruction — `AITrack.SEMANTIC_IMAGE` remains the unimplemented browse-only
placeholder it has been since ADR-0011; no code changes here.

## Feature vector schema

Spec §4's exact field list, implemented as `feedback_store.FeatureVector`:

| field | source | notes |
|---|---|---|
| `size_bytes` | `AIClusterMember.size_bytes` | |
| `ext` | `member.path.suffix.lower()` | |
| `path_class` | `classify_path_class` | categorical: `cloud_sync_placeholder` > `git_repo` > `downloads`/`desktop`/`documents`/`temp` (path-segment match) > `other`, in that priority order — a path can technically match more than one signal; this picks one dominant class for a single categorical feature (`cloud_sync_flag` still separately carries the cloud signal even when it doesn't win path_class) |
| `mtime`, `ctime` | `Path.stat()` | **no `atime` field exists on this type at all** — not omitted by convention, structurally absent (spec: "unreliable on NTFS," where `NtfsDisableLastAccessUpdate` commonly disables atime tracking system-wide) |
| `cluster_stats` | `AICluster`/`AIClusterMember` | `cluster_size`, `position_in_cluster` (VERSION_CHAIN only, else `None`), `raw_score`, `score_kind`, `is_recommended_keep` |
| `category` | `cluster.track.value` | the `AITrack` this decision came from |
| `cloud_sync_flag` | caller-supplied (same signal `SafetyValidator`/`FileRecord.is_cloud_placeholder` already computes, not re-derived) | |
| `sibling_decision_context` | `FeedbackStore`'s own prior history for the same `cluster_id`, computed at decision time | `prior_accepted`/`prior_rejected`/`prior_kept` — a real, informative ranking signal (a cluster where 3 siblings were already accepted is a strong prior the 4th is clutter too) that no single member's own attributes can capture alone |

`FeedbackDecisionKind = Literal["accepted", "rejected", "kept"]` — "accepted" (user approved
an AI suggestion), "rejected" (user declined a suggestion for this specific member),
"kept" (user explicitly marked this member a permanent keeper — a stronger, deliberate
signal than a bare rejection).

## Persistence discipline — mirrors `labeling.LabelStore` exactly

- **Append-only JSONL**, never rewritten in place — same event-log pattern as
  `executor.QuarantineManifestEntry` and `labeling.LabelStore`.
- **Commit-keyed**: every `FeedbackDecision` carries `commit_sha` via
  `eval_harness.current_commit_sha()` — the same metric-provenance discipline (house rule
  65b) `LabelDecision` already holds itself to.
- **Versioned**: `schema_version: int = 1` on every entry, same field name and default as
  `LabelDecision.schema_version`.
- **Local, never committed**: real callers point `FeedbackStore` at a path under
  `data/ai_feedback/`, added to `.gitignore` in this same change — real, personal accept/
  reject/keep decisions and file paths from GG's own disk, same posture as `data/ai_labels/`.
- **Deterministic for testing**: `record_feedback_decision` accepts an injectable `now`
  (mirrors `labeling.record_decision`'s pattern) so tests never depend on wall-clock time.

## Cold-start heuristic (`cold_start_priority.py`) — clearly labeled non-ML

Spec's own formula: `size × mtime-staleness × location-weight × cluster-membership`.
Implemented as a documented, transparent weighted combination (log-compressed on the two
unbounded terms — size in bytes, staleness in days — for the same reason
`keep_best._combine` log-compresses sharpness/resolution: one extreme outlier shouldn't
dominate). Every component is exposed on `ColdStartPriority`, not just the combined score, so
a future review UI can show *why* an item ranked where it did — same diagnosability posture
as `keep_best.QualityScore`.

`ColdStartPriority.is_heuristic` is hardcoded `True` on every result, structurally, so
nothing downstream can mistake this priority number for a model's prediction (spec §0.6's
"never a manufactured confidence" — this field exists specifically so a UI or log line can
assert on it rather than trusting a docstring). Location weights (`downloads`/`temp` highest,
`git_repo` lowest) are a policy choice, not fit/measured — same disclosed-policy status as
`screenshot_burst.py`'s 60-second capture-time window (ADR-0019).

## Consequences

- The ranker's activation gate (`>= 500` real decisions) and its time-split evaluation
  protocol are **documented here, not implemented** — a future PR building the actual
  LightGBM LambdaMART ranker must: (1) check `FeedbackStore.count() >= 500` before training
  anything, (2) split train/eval by `decided_at` (train on the past, evaluate on strictly
  later decisions — the same disjointness discipline used elsewhere in this project), (3)
  record the trained model's artifact + time-split metrics with the same
  `EvalReport`/commit-SHA provenance discipline every other operating point in this codebase
  carries, (4) never let the ranker's output reach `apply_batch` directly — it ranks a review
  queue, it never deletes.
- No `lightgbm` dependency exists in this codebase yet — adding it is explicitly future work,
  gated on real label volume existing first.
- The cold-start heuristic's weights (`_LOCATION_WEIGHTS` in `cold_start_priority.py`) are
  disclosed as directional, not measured — a legitimate target for future refinement once
  real feedback data exists to check them against, but not blocking today.
- This closes the applied-AI layer's build order: Feature 1a (near-identical images) + 1b
  (document near-dup + version-chain) + 2 (screenshot burst + content tagging) +
  feedback-logging (this ADR). `AITrack.RANKED_CLUTTER` and `AITrack.SEMANTIC_IMAGE` remain
  documented, unimplemented placeholders for future work, not silently dropped features.

## Test coverage

`tests/test_ai_feedback_store.py` (10 cases — `classify_path_class`'s priority ordering, the
structural no-atime proof, append/read round-trip, sibling-decision-context computation
across multiple decisions in the same cluster, cross-cluster isolation, missing-file
handling), `tests/test_ai_cold_start_priority.py` (9 cases — each component's monotonicity
in the expected direction, unknown-path_class graceful default, the `is_heuristic` structural
label). Both modules are automatically covered by `evals/test_ai_safety_gate.py`'s AST scan
(no `reclaim.executor`/`send2trash` import, via the same `src/reclaim/ai/**` `rglob`) with no
manual update needed.
