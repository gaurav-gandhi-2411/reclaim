# 0012. Feature 1a Track A: provisional pHash operating point + classical keep-best weights

## Context

`reclaim-ai-features-spec.md` §7.3 requires every threshold to be "chosen from the PR curve
... at a target precision — NOT hand-set," with near-identical/deletion-suggestion tracks
targeting precision ≥ 0.95. The explicit autonomy boundary in the build brief is equally
direct: thresholds selected on synthetic fixtures are PROVISIONAL, must be labeled as such
everywhere they appear, and must never be presented as a data-chosen final operating point —
that requires GG's real gold-set labels (not yet collected; the labeling tool this same build
delivers is how they will be).

## Decision

**Selection method is real; data source is synthetic — both facts recorded together, always.**
`evals/test_ai_image_similarity.py::test_phash_operating_point_meets_target_precision_on_synthetic_fixtures`
runs the actual `eval_harness.precision_recall_curve` / `select_operating_point` machinery
(spec §7.3's real selection method) over every pairwise Hamming distance in the synthetic
fixture set, targeting precision ≥ 0.95. `OperatingPoint.is_provisional` is hardcoded `True` by
`select_operating_point` itself — there is no code path that returns a non-provisional
`OperatingPoint` today, so a caller cannot accidentally present one as final.

**Measured provisional threshold:** `max_hamming_distance = 2` at precision 1.0 on the
synthetic fixture set (`evals/ai_fixtures/build_image_similarity_fixtures.py`, 6 clusters × 4
members + 8 distractors, SEED = 42). Within-cluster pairwise Hamming distances measured 0–2;
cross-cluster distances measured 22–40 — a wide, clean separation on this synthetic data.

**CI regression gate uses a looser, explicitly-margined threshold (10), not the measured 2.**
The fast, deterministic CI floor test (`test_clustering_bcubed_precision_recall_meets_floor_at_
provisional_threshold`) hardcodes `max_hamming_distance = 10` rather than re-deriving the PR
curve on every run (that derivation is already proven by the test above; re-running it per-CI-
invocation would be redundant, not additional evidence). 10 was chosen with deliberate margin
above the measured synthetic within-cluster max (2) because real photos — genuine re-saves,
actual capture-time-adjacent shots, real compression artifacts — are expected to show higher
within-cluster Hamming distances than these comparatively clean synthetic transforms
(resize/recompress/mild-brightness-shift). This is an explicit acknowledgment that the
synthetic fixtures are an easier case than reality, not a claim that 10 is itself a validated
real-world threshold — it remains provisional, chosen for CI regression-catching headroom, not
for production use.

**Classical keep-best weights are directional, not fit.** `keep_best._combine`'s weights
(`sharpness × 2.0 + resolution × 1.0 + exposure_penalty × 0.05`, each log-compressed) are
chosen so the DIRECTION of each signal is correct (sharper/higher-resolution/well-exposed
scores higher) — they are not fit against any labeled data, because none exists yet. The
fixture's ground truth is deliberately unambiguous by construction (the "worst" member is
genuinely blurred + downscaled + heavily compressed; the "best" member is genuinely full-
resolution and unblurred) specifically so passing the eval requires correct direction, not
finely-tuned weights — this was validated empirically: an earlier fixture revision that
resized every cluster member back to identical final dimensions accidentally zeroed out the
resolution signal's discriminating power and produced an arbitrary (0.667) top-1 agreement
between two members that were, by the scorer's actual measured signals, genuinely tied; fixing
the fixture to preserve real resolution differences (matching how real near-dup copies usually
do differ in actual pixel count) resolved it to 1.0 without touching the weights at all —
evidence the weights were never the problem, the fixture's realism was.

## Consequences

- Nothing in this ADR, the code, or the eval report may be cited as "Reclaim's near-duplicate
  threshold is 2 Hamming distance" or similar in any user-facing copy, README, or pitch
  material — every occurrence must carry the "provisional, synthetic-fixture-derived, pending
  gold-set validation" qualifier, per the explicit autonomy-boundary instruction this build
  operated under.
- The real operating point — and whether `max_hamming_distance` even generalizes as a single
  global constant vs. needing per-content-type tuning — is a follow-up requiring GG's gold-set
  labels (delivered as a tool, not run, this same build — see the labeling-tool section of
  PLAN.md's checkpoint).
- The classical scorer's weights remain a documented, inspectable formula, never presented as
  a calibrated confidence score (spec §0.6) — `combined` is explicitly labeled as a ranking
  signal, not a probability, everywhere it's used.

## Alternatives considered

- **Hand-set a threshold from visual inspection of a few images, skip the PR-curve
  machinery entirely.** Rejected outright — this is exactly the "magic constant" spec §7.3
  explicitly distinguishes applied AI from, and the autonomy boundary explicitly forbids
  presenting a hand-set value as measured.
- **Wait to build anything until a real gold set exists.** Rejected — the build brief is
  explicit that blocking on GG's labeling is not permitted; the correct sequence is deliver the
  fixture-green pipeline + labeling tool, then report, with real-data threshold selection as
  an explicit, separate follow-up.

## Test coverage

`evals/test_ai_image_similarity.py` (5 cases: PR-curve-derived operating point, BCubed
precision/recall floor, keep-best safety metric, keep-best top-1 agreement, end-to-end
orchestration with a safety-filtered path). `tests/test_ai_phash.py` (7 cases), `tests/
test_ai_keep_best.py` (6 cases). All against synthetic fixtures only — no gold-set dependency,
consistent with this ADR's own provisional-only scope.
