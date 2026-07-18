# 0012. Feature 1a Track A: pHash operating point (MEASURED) + classical keep-best weights

## Status update (see ADR-0015)

**The `max_hamming_distance` operating point is promoted from PROVISIONAL to MEASURED as of
ADR-0015.** It is now derived from a real PR curve over the public INRIA Copydays dataset (real,
human-construction-verified ground truth — not synthetic, not LLM-labeled), not from the
synthetic CI fixtures. The synthetic-fixture measurement below is kept in this ADR for history
and because it still backs the fast, deterministic CI regression test — but it is no longer the
basis for the shipped/production value. **Measured value: `max_hamming_distance = 14`,
precision = 0.96, recall = 0.0764**, against 74,305 real pairs (314 positive) from Copydays'
`original` + `strong` splits — see "Measured operating point (real, ADR-0015)" below for the
full curve and the honest caveat about what that recall number does and doesn't represent.

## Context

`reclaim-ai-features-spec.md` §7.3 requires every threshold to be "chosen from the PR curve
... at a target precision — NOT hand-set," with near-identical/deletion-suggestion tracks
targeting precision ≥ 0.95. The explicit autonomy boundary in the original build brief was
equally direct: thresholds selected on synthetic fixtures are PROVISIONAL, must be labeled as
such everywhere they appear, and must never be presented as a data-chosen final operating
point — that required either GG's real gold-set labels or another real, human-verified source.
ADR-0015 supplies the latter (INRIA Copydays) before GG's own labeling produced any data.

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

**CI regression gate now locked at the measured real value (14), not the old synthetic-margin
guess (10).** The fast, deterministic CI floor test
(`test_clustering_bcubed_precision_recall_meets_floor_at_measured_threshold`) hardcodes
`max_hamming_distance = 14` — still evaluated against the synthetic fixtures for speed and
determinism (a CI gate can't depend on a 268MB third-party download succeeding on every push),
but the constant itself now comes from ADR-0015's real measurement below, not from an
arbitrary margin above the synthetic within-cluster max. Confirmed to still pass cleanly at 14
against the synthetic fixtures' clean 0–2/22–40 separation.

## Measured operating point (real, ADR-0015)

Derived from the actual PR-curve/`select_operating_point` machinery (the same functions used
above, same selection method) run over **74,305 real pairwise Hamming distances** (314
positive / 73,991 negative) from the public INRIA Copydays dataset — see ADR-0015 for the
dataset, license, and download provenance. Command, commit, and fixture path are printed by the
eval itself (`EvalReport`), reproduced here:

```
uv run python evals/ai_fixtures/fetch_copydays.py
uv run pytest evals/test_ai_copydays_gold.py -v -s
```

Representative points from the real curve — **cumulative** precision/recall for "every pair
with distance ≤ X" (computed directly from the same 74,305 real Hamming distances, not read off
`precision_recall_curve`'s raw per-pair point stream: that stream emits one point per pair in
tie-broken sort order, so at a shared distance value the *first* pair processed can show a
lower/higher precision snapshot than the true value once *every* pair at that distance is
included — `select_operating_point` itself always finds the correct max-recall-at-≥0.95-
precision point regardless, since it scans every individual point, but a human-readable summary
table needs the true per-cutoff cumulative numbers, not an arbitrary mid-tie snapshot, to avoid
implying a row means something it doesn't):

| max_hamming_distance | precision (cumulative) | recall (cumulative) |
|---:|---:|---:|
| 10 | 1.0000 | 0.0350 |
| 12 | 0.9444 | 0.0541 |
| **14** | **0.9600** | **0.0764** |
| 16 | 0.6735 | 0.1051 |
| 18 | 0.3256 | 0.1338 |
| 20 | 0.1164 | 0.1720 |
| 30 | 0.0060 | 0.6656 |
| 42 | 0.0042 | 1.0000 |

**Chosen operating point: `max_hamming_distance = 14`, precision = 0.9600, recall = 0.0764** —
the highest-recall point on the real curve that still clears the spec's ≥0.95 precision target
for the deletion-suggestion track. This value replaces the synthetic-fixture value (2) as the
MEASURED default; it is close to, and slightly above, the old CI-margin guess of 10.

**Honest generalization caveat (the recall number specifically).** 0.0764 recall looks low, and
the reason is disclosed, not hidden: ADR-0015's download only reached Copydays' `original` +
`strong` splits. `strong` (print-and-scan, blur, paint, and combinations) is Copydays' single
*hardest, deliberately adversarial* attack tier — designed by its creators to stress-test
whether an algorithm can survive someone actively trying to defeat copy detection. It is a much
harder distribution than Feature 1a's actual target: a consumer's photo library with ordinary
resize/recompress/re-export duplicates, closer to the milder `jpeg` (graduated JPEG-quality
75→3) and `crop` (graduated 10%–80%) splits, which were **not reachable** on the available
mirror (see ADR-0015). The measured recall of 0.0764 is therefore a **real, honest floor on an
adversarial subset** — not a direct estimate of how often Reclaim will actually flag ordinary
consumer duplicates. It should not be read as "Feature 1a only catches 7.6% of real
duplicates," and this ADR explicitly forbids that reading in any user-facing copy. The precision
side of the measurement (0.96 at the chosen threshold) carries no such caveat — that number is
a direct, reliable measurement of false-positive risk on real data, which is the number that
protects users from bad delete-suggestions and matters most for the "never delete on an AI
hunch" hard gate.

**Keep-best, measured on the same real dataset.** `evals/test_ai_copydays_gold.py::
test_keep_best_against_copydays_original_vs_attacked` uses Copydays' 157 original-vs-attacked
blocks as real (non-fabricated, non-LLM) keep-best ground truth — the unmodified original is
the real "should be kept" answer relative to its print-and-scanned/blurred/painted derivatives.
Measured: **top-1 agreement = 0.8726** (137/157 blocks), **never-picks-worst-quartile safety
rate = 1.0000** (never once, across 157 blocks). The 20 disagreement blocks (scorer picked an
attacked variant over the real original) are written to
`reports/ai/copydays_keep_best_disagreements.json` for GG's optional one-click review — not
auto-resolved, not silently discarded, per the explicit instruction against fabricating
preference labels.

**No AVA (or similar) general-aesthetic-correlation check was run.** See ADR-0015's "What was
explicitly NOT done" section for the full reasoning (dataset size, licensing ambiguity) — this
is a disclosed scope decision, not a silent gap. The keep-best measurement above covers the
operationally relevant question ("does the scorer pick the right copy to keep within a
near-dup group") with real data; it does not cover the separate, secondary question of whether
`combined`'s raw magnitude correlates with generic human aesthetic taste across unrelated
photos.

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

- **`max_hamming_distance = 14` may now be cited as Reclaim's MEASURED near-identical
  operating point**, sourced from a real, human-construction-verified public dataset
  (ADR-0015) — with the recall caveat above stated alongside it every time, since precision and
  recall tell honestly different stories on this particular real measurement. The old
  "provisional, synthetic-fixture-derived" qualifier no longer applies to this number.
- Whether `max_hamming_distance` generalizes as a single global constant vs. needing
  per-content-type tuning remains open. GG's own gold-set labeling (ADR-0014's tool, still
  unrun as of this ADR) would add a second, independent, consumer-realistic real-data point —
  if it disagrees materially with this measurement, that disagreement is itself the signal to
  investigate, not a reason to prefer one source over the other by default.
- Re-acquiring Copydays' `jpeg`/`crop` splits (or an equivalent milder, graduated-severity
  public source) from a different mirror is an explicit, disclosed follow-up that would let this
  measurement's recall number be re-derived on a distribution closer to Feature 1a's actual
  target use case.
- The classical scorer's weights remain a documented, inspectable formula, never presented as
  a calibrated confidence score (spec §0.6) — `combined` is explicitly labeled as a ranking
  signal, not a probability, everywhere it's used. They are now real-data-checked (0.8726 top-1
  agreement, 1.0 safety rate on Copydays) in addition to directionally-correct-by-construction
  on synthetic fixtures — still not re-fit to either, per the "directional, not fit" reasoning
  above, which continues to hold.

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

**Synthetic (CI, every run):** `evals/test_ai_image_similarity.py` (5 cases: PR-curve-derived
operating point, BCubed precision/recall floor at the measured threshold, keep-best safety
metric, keep-best top-1 agreement, end-to-end orchestration with a safety-filtered path).
`tests/test_ai_phash.py` (7 cases), `tests/test_ai_keep_best.py` (6 cases).

**Real (local, on-demand, not in CI — see ADR-0015):** `evals/test_ai_copydays_gold.py` (2
cases: the real PR curve + operating-point selection, and keep-best top-1/safety/disagreement
reporting), against the actual downloaded INRIA Copydays dataset. This is the source of every
MEASURED number in this ADR; the synthetic suite continues to serve as the fast CI regression
gate at the value this real suite established.
