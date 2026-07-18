# 0012. Feature 1a Track A: pHash operating point (MEASURED) + classical keep-best weights

## Status update (see ADR-0015, then the realistic-distribution follow-up below)

**The `max_hamming_distance` operating point is promoted from PROVISIONAL to MEASURED as of
ADR-0015.** It is now derived from a real PR curve over the public INRIA Copydays dataset (real,
human-construction-verified ground truth — not synthetic, not LLM-labeled), not from the
synthetic CI fixtures. The synthetic-fixture measurement below is kept in this ADR for history
and because it still backs the fast, deterministic CI regression test — but it is no longer the
basis for the shipped/production value.

**Second status update — the recall number above was measured on the WRONG distribution, and
GG caught it.** ADR-0015's only reachable real data was Copydays' `strong` split — its single
hardest, deliberately adversarial attack tier (print-and-scan/blur/paint), not representative
of Feature 1a's actual target (ordinary consumer duplicate accumulation: re-saves, resizes,
format conversions, messaging-app re-compression). Measuring recall against that tier alone and
treating it as "the" operating distribution was flagged as not shippable. A follow-up
measurement against a programmatically-generated REALISTIC distribution (see "Realistic-
distribution measurement" below) found **precision = 0.9987, recall = 1.0000** at the same
`max_hamming_distance = 14` — the earlier 0.0764 recall figure was real but was measuring the
wrong thing, not revealing an actual pHash limitation. `max_hamming_distance = 14` is
**reaffirmed**, now for the correct reason.

**Third status update — required gate-hardening disclosure (ADR-0016), applied retroactively
here.** ADR-0016 requires every operating point to carry an explicit `DistributionDeclaration`
stating what was and wasn't measured, precisely because the incident above showed a real
measurement can still be silently non-representative. Applying that requirement to THIS ADR's
own MEASURED claim, stated plainly:

> **5 transforms measured; uncommon transforms unmeasured.** `max_hamming_distance = 14`'s
> MEASURED status rests on 5 deterministic transform profiles (mild recompress, mild resize,
> moderate resize+recompress, moderate PNG round-trip, messaging-app-style resave — see
> `build_realistic_recompression_tiers.py`) applied to 157 real photos. It has NOT been measured
> against: rotation, cropping, aspect-ratio changes, watermarks/text overlays, color/contrast/
> filter edits, heavier JPEG recompression than quality 65, multi-generation chains beyond two
> re-saves, or any combination of the above. These are all plausible real-world sources of
> consumer photo duplicates that this measurement is silent on. `max_hamming_distance = 14`'s
> margin above the bare-minimum-2 needed for the 5 tested profiles (see "Operating-point
> decision" below) is the disclosed mitigation for this gap — not a claim the gap doesn't exist.

This is the same content the "Realistic-distribution measurement" section below states in
narrative form (`untested_variation_note` on `_REALISTIC_DISTRIBUTION` in
`evals/test_ai_copydays_realistic_distribution.py` carries it as machine-checked data, not just
prose) — repeated here explicitly, at the top of the ADR, per ADR-0016's requirement that this
disclosure be visible wherever MEASURED is claimed, not buried in a later section a reader might
skip.

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

## Realistic-distribution measurement (follow-up — this is what actually governs the operating point)

**Why this exists.** GG's read of the 0.0764 recall figure above was correct to distrust: a
recall number is only meaningful relative to the distribution it was measured against, and
`strong` is Copydays' single most adversarial tier by design — nobody's consumer photo library
accumulates duplicates via deliberate print-and-scan or paint-filter attacks. The instruction
was explicit: either recover Copydays' milder graduated splits, or generate the realistic
transformations programmatically from clean real images with known ground truth — and re-measure
recall per tier, not as one blended adversarial-only number.

**A second search for the milder splits found nothing new.** Before generating anything,
`copydays_jpeg`/`copydays_crop` were searched for again across Kaggle, Zenodo, Academic
Torrents, the Wayback Machine (specifically for those two files, not just the tarball already
tried in ADR-0015), and two more third-party dataset-hosting platforms (Graviti, TIB's LDM
service) — none had them reachable. This confirms ADR-0015's original finding rather than
reopening it; the programmatic-generation path is the one actually taken.

**Generated a realistic distribution from Copydays' own 157 real originals.**
`evals/ai_fixtures/build_realistic_recompression_tiers.py` applies five deterministic,
named transform profiles to every real original photo — no synthetic drawn shapes, real
photographic content throughout:

| tier | profile | what it simulates |
|---|---|---|
| mild | `mild_recompress_q92` | re-export at the same size, high JPEG quality |
| mild | `mild_resize_97_q90` | barely-perceptible downscale + high quality |
| moderate | `moderate_resize_80_q70` | "resize to share" copy, moderate quality |
| moderate | `moderate_roundtrip_png_q65` | two compression generations (edit-then-re-export) |
| messaging_app | `messaging_app_resave` | WhatsApp/Instagram-style downscale to ≤1600px long edge, quality 75, metadata stripped |

157 originals × 5 profiles = 785 real-content-derived positive pairs (each original vs. its own
variant), with the existing `hard` tier (229 real Copydays `strong`-attack pairs) kept and
reported alongside for honest comparison — never discarded, never blended into the distribution
used to select the operating point. Negatives are the 12,246 real original-vs-original
cross-block pairs (deliberately excluding any `strong`-tainted image from the negative pool —
a heavily blurred/painted image could show an artificially large Hamming distance for reasons
unrelated to genuine dissimilarity, which would inflate the realistic-distribution precision
estimate if it leaked into "negative" pairs).

Reproduce:
```
uv run pytest evals/test_ai_copydays_realistic_distribution.py -v -s
```

**Per-tier recall at the currently-locked threshold (`max_hamming_distance = 14`):**

| tier | recall | pairs caught |
|---|---:|---:|
| mild | **1.0000** | 314/314 |
| moderate | **1.0000** | 314/314 |
| messaging_app | **1.0000** | 157/157 |
| hard (Copydays `strong`, for comparison only) | 0.0961 | 22/229 |

pHash catches **every single** mild/moderate/messaging-app-resave duplicate at the current
threshold. The `hard` tier's 9.6% recall is real, honestly reported, and **irrelevant to Feature
1a's actual target failure mode** — pHash was never expected to survive a deliberate anti-
forensic attack, and Reclaim's target user doesn't produce print-and-scanned copies of their own
phone photos by accident. Confirming `mild` recall exceeds `hard` recall is asserted as a
regression-catching sanity check in the eval itself (an inversion would mean the hash pipeline
is broken, not just that recall happens to be low).

**Full precision-recall tradeoff on the realistic distribution** (mild+moderate+messaging_app
positives, clean original-vs-original negatives; cumulative "every pair with distance ≤ X"):

| max_hamming_distance | precision | recall |
|---:|---:|---:|
| 0 | 1.0000 | 0.9248 |
| **2** | **1.0000** | **1.0000** |
| 12 | 0.9987 | 1.0000 |
| **14 (locked)** | **0.9987** | **1.0000** |
| 16 | 0.9975 | 1.0000 |
| 18 | 0.9849 | 1.0000 |
| 20 | 0.9268 | 1.0000 |

**Recall at precision ≥0.95 / ≥0.90 / ≥0.85 — all three collapse to the same point:**
`max_hamming_distance = 2`, precision = 1.0000, recall = 1.0000. This isn't a copy-paste
artifact: recall saturates at 1.0000 by distance 2 and precision stays at 1.0000 all the way to
distance 10, so the highest-recall point clearing *any* of the three targets is the same point —
there is no precision/recall tradeoff to make in this range at all, because recall has nothing
left to gain.

**Operating-point decision: KEEP `max_hamming_distance = 14`, and do NOT loosen toward a 0.90
precision target.** GG's instruction asked to re-evaluate whether 0.90 precision might be a
better operating point than 0.96, given the recommend-only human-confirmed review-queue design
(a lower-precision, higher-recall point can be worth it when a human always double-checks
before deletion). The realistic-distribution measurement answers this directly: **there is no
recall to buy by loosening past distance 2.** Every point from distance 2 through at least
distance 20 shows recall = 1.0000 on this measured set; loosening the threshold only trades away
precision (1.0000 → 0.9268 by distance 20) for zero additional recall, which would only add more
false positives to the human reviewer's queue for no offsetting benefit. The "0.90 precision
buys meaningfully more recall" premise, which was reasonable to raise given the ADR-0015-only
data, does not hold once the realistic distribution is the one being measured.

Given that, why keep 14 instead of tightening to 2 (which shows the same perfect 1.0000/1.0000
on this exact measured set)? **Deliberate margin beyond the 5 tested transform profiles.** These
5 profiles don't exhaust real-world duplicate variation — a slightly more aggressive resave, a
minor color/contrast edit, a different resize algorithm, a watermark, could plausibly push a
real duplicate a few Hamming bits further than anything tested here. `max_hamming_distance = 14`
retains the same measured precision (0.9987 — a difference of one single false positive across
12,246 real negative pairs) while giving real headroom for duplicate variation this specific
measurement didn't cover, consistent with the original margin reasoning from the synthetic-
fixture era, now grounded in a real quantified cost (0.13 percentage points of precision) rather
than an arbitrary guess.

**Track B (CLIP embeddings) trigger: NOT triggered by this data.** The build brief asked this
measurement to double as the "do embeddings earn their compute" decision input for Track A
specifically. It does not: pHash already achieves near-perfect precision and perfect recall on
the realistic near-identical/copy-detection distribution. There is no Track-A recall gap for
embeddings to close. Track B's own motivation — semantic/viewpoint grouping (near-identical
bursts, different photos of the same subject/scene) — is a genuinely different problem from
copy/near-duplicate detection and remains independently justified on its own terms, not as a
Track A recall rescue. If a real Track-A recall gap ever does appear (e.g. from GG's own
gold-set labels showing a case these 5 synthetic profiles didn't cover), that would be the
actual trigger — this measurement is not it.

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

- **This ADR's own operating-point selection now runs through ADR-0016's hardened gate**:
  `evals/test_ai_copydays_gold.py`'s hard-tier curve is asserted to be REJECTED by
  `select_operating_point`'s recall floor (proving the original mistake can't recur silently),
  and `evals/test_ai_copydays_realistic_distribution.py` calls
  `assert_safe_to_promote_to_measured` on the distribution this ADR actually cites, proving that
  citation is structurally safe, not just asserted to be in prose.
- **`max_hamming_distance = 14` is Reclaim's MEASURED near-identical operating point**, and —
  as of the realistic-distribution follow-up — may be cited with confidence on the distribution
  that actually matters: precision 0.9987, recall 1.0000 on mild/moderate/messaging-app-style
  consumer duplicates. The `hard`-tier-only figures (precision 0.9600, recall 0.0764) remain in
  this ADR for the record and must always carry the "Copydays' adversarial `strong` tier only,
  not the operating distribution" qualifier if cited at all — user-facing copy should cite the
  realistic-distribution numbers, not the hard-tier ones.
- Whether `max_hamming_distance` generalizes as a single global constant vs. needing
  per-content-type tuning remains open. GG's own gold-set labeling (ADR-0014's tool, still
  unrun as of this ADR) would add a third, independent, real-photo-library data point — if it
  disagrees materially with either measurement here, that disagreement is itself the signal to
  investigate, not a reason to prefer one source over the other by default.
- The realistic-distribution measurement is itself bounded by the 5 transform profiles tested
  (see `build_realistic_recompression_tiers.py`) — real duplicate variation could exceed what
  those 5 profiles cover. `max_hamming_distance = 14`'s margin above the bare-minimum-2 needed
  for these specific profiles is the disclosed mitigation, not a claim that 5 profiles are
  exhaustive. GG's own gold-set labels remain the strongest available check on this.
- Re-acquiring Copydays' `jpeg`/`crop` splits (or an equivalent milder, graduated-severity
  public source) remains a nice-to-have, not a blocker — the programmatically-generated
  realistic distribution already answers the question those splits would have answered, and a
  second search pass for them (ADR-0012's realistic-distribution follow-up) confirmed they're
  still not reachable on any checked mirror.
- The classical scorer's weights remain a documented, inspectable formula, never presented as
  a calibrated confidence score (spec §0.6) — `combined` is explicitly labeled as a ranking
  signal, not a probability, everywhere it's used. They are now real-data-checked (0.8726 top-1
  agreement, 1.0 safety rate on Copydays) in addition to directionally-correct-by-construction
  on synthetic fixtures — still not re-fit to either, per the "directional, not fit" reasoning
  above, which continues to hold.
- Track B (CLIP embeddings) is NOT triggered by Track A's measured performance — see the
  realistic-distribution section above. It remains a separately-justified, on-hold build-order
  item for its own semantic-grouping mission, not a recall rescue for Track A.

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
cases: the real PR curve + operating-point selection against Copydays' `hard` tier, and
keep-best top-1/safety/disagreement reporting), against the actual downloaded INRIA Copydays
dataset. `evals/test_ai_copydays_realistic_distribution.py` (1 case: per-tier recall at the
locked threshold + the realistic-distribution PR tradeoff), against Copydays' 157 real originals
plus 785 programmatically-generated realistic variants
(`evals/ai_fixtures/build_realistic_recompression_tiers.py`). Together these are the source of
every MEASURED number in this ADR; the synthetic suite continues to serve as the fast CI
regression gate at the value this real suite established.
