# 0015. Feature 1a operating point: public human-verified gold set (INRIA Copydays), not GG's disk

## Context

GG's own gold-set labeling (ADR-0013/0014) hadn't produced any labels yet when this decision
was made (`data/ai_labels/gold_labels.jsonl` didn't exist — confirmed by direct search before
starting this work). Rather than wait, or select an operating point on GG's own disk (which
would risk overfitting Reclaim's shipped default to one person's photo library), GG directed a
different, stronger source of ground truth: acquire a **public, human-labeled** near-duplicate
dataset first, and reserve LLM-as-labeler for only the features where no such dataset exists —
explicitly never as the source of a shipped operating point.

This ADR documents the dataset evaluation, the choice, its license, and exactly what was and
wasn't measurable from it. It promotes ADR-0012's `max_hamming_distance` from **provisional**
(synthetic-fixture-derived) to **MEASURED** (real-dataset-derived) — see the companion update
to ADR-0012 for the promoted numbers themselves.

## Candidates evaluated

| Dataset | Task match | License | Scripted download | Verdict |
|---|---|---|---|---|
| **INRIA Copydays** | Best — purpose-built for copy/near-duplicate detection, with three *graduated-severity* attack categories (JPEG-quality ladder, crop-percentage ladder, "strong" combined attacks) | INRIA copyright, "provided as-is," citation required — no redistribution restriction beyond that | Yes — direct HTTPS, no auth, no remote code execution required (see "Download mechanics" below) | **Selected** |
| INRIA Holidays | Weaker — designed for image *retrieval* (same scene, different viewpoint/angle/lighting), not consumer copy/resave/recompress duplicates | Same INRIA "as-is" terms as Copydays | Yes — direct HTTPS from the same host | Rejected: task mismatch. Viewpoint-varied retrieval is a different problem than the resize/recompress/minor-edit duplicates Feature 1a targets; kept as a candidate for a possible future Feature 1a Track B (semantic/viewpoint grouping) rather than Track A (near-identical). |
| California-ND | Arguably the *best* conceptual task match — 701 real personal photos, annotated by 10 human subjects specifically for near-duplicate judgment in personal collections | Creative Commons | **No** — the archive is a password-protected zip; the password is obtained by emailing the dataset's author directly, which is a manual, gated, non-scriptable, and (given the dataset's age, published 2013) uncertain-to-respond process | Rejected on the explicit "scripted download" requirement. Flagged as a good target for manual, GG-driven acquisition in the future if a maintainer response is ever obtained — not usable in this automated pass. |
| UKBench | Weak — object/scene recognition under radical viewpoint change (CD covers, objects from four angles), not photo-copy duplication | Unconfirmed / unclear in current sources; original host (vis.uky.edu) is down, only an Internet Archive mirror was found | Ambiguous | Rejected: weak task match plus unresolved licensing — didn't clear either bar cleanly enough to spend further time on. |
| MIR-Flickr / NUS-WIDE "near-dup subsets" | N/A — these are large multi-label web-image datasets (25K / 269K images) with no standard, distributed near-duplicate *pair* annotation; a "near-dup subset" would have to be derived, which is exactly the kind of unaudited labeling this build is supposed to avoid | N/A | N/A | Rejected: doesn't exist as a ready-made public human-labeled artifact for this task; using it would mean labeling it ourselves first, defeating the purpose. |

## Decision: INRIA Copydays, via the Meta/FAIR mirror

**Dataset.** INRIA Copydays — 157 original personal-holiday photos, each with one or more
artificially-attacked derivatives, built explicitly to evaluate copy-detection/near-duplicate
algorithms (Jégou, Douze & Schmid, ECCV 2008 companion dataset). Ground truth is not a human
*judgment call* the way a labeler's binary decision is — it falls directly out of how INRIA
constructed the dataset (every attacked image is a known, deliberate derivative of a known
original), which makes it exactly as reliable as a human-audited label for this purpose, with
zero LLM involvement anywhere in its construction.

**License.** INRIA holds copyright on all images; the dataset is "provided as is," with a
citation requirement (Jégou, Douze, Schmid, "Hamming Embedding and Weak Geometry Consistency
for Large Scale Image Search," ECCV 2008). No clause restricts using the images locally to
compute measurements — this is the same license under which the dataset has been used in
hundreds of published papers and by Meta/FAIR's own research (see "Download mechanics"). The
dataset is never committed to this repo (`.gitignore`: `data/ai_datasets/`) and never
redistributed by Reclaim — only downloaded locally, hashed, and measured, on demand, by
`evals/ai_fixtures/fetch_copydays.py`.

**Download mechanics — a real problem, and the actual fix.** The dataset's original host
(`pascal.inrialpes.fr`, still linked from the live description page at
`thoth.inrialpes.fr/~jegou/data.php.html`) is unreachable — confirmed via a direct TCP connect
attempt (21s timeout, not a DNS or auth failure; the legacy INRIA server appears to be down).
Meta/FAIR re-hosts the identical files at a stable CDN
(`dl.fbaipublicfiles.com/vissl/datasets/`) for their own published copy-detection research
(`facebookresearch/vissl`, `facebookresearch/sscd-copy-detection`) — verified reachable
(`HTTP 200`) for `copydays_original.tar.gz` and `copydays_strong.tar.gz`. An unofficial
third-party Hugging Face mirror (`galilai-group/INRIA-CopyDays`) was found and explicitly
**rejected**: it hosts no actual image data (`usedStorage: 0`) and instead ships a
`trust_remote_code=True` Python loader script that would execute arbitrary code from an
unverified community account — a supply-chain risk with no offsetting benefit once a direct,
checksum-verified HTTPS path exists. `evals/ai_fixtures/fetch_copydays.py` downloads directly
from the FAIR CDN via `urllib.request` (no remote code execution, no `trust_remote_code`),
verifies SHA-256 against the checksums recorded from this session's own download before
extracting, and refuses to extract on a mismatch.

**A real, disclosed coverage gap: no `copydays_jpeg` or `copydays_crop` split.** The original
host advertises four splits: `original` (208MB), `crop` (2.0GB, 10%–80% surface removed, a
*graduated* severity ladder), `jpeg` (77MB, scale 1/16 + JPEG quality 75→3, also *graduated*),
and `strong` (60MB, 229 images — print-and-scan, blur, paint, and combinations, a **single**,
deliberately adversarial severity tier, not graduated). Only `original` and `strong` were
reachable on the FAIR mirror; `jpeg` and `crop` returned `403 Forbidden` on every URL pattern
tried. This means the real gold set used for measurement in ADR-0012's update is built entirely
from `original` + `strong` — **the milder, more consumer-realistic graduated-severity splits
were not available**, and the measured operating point should be read with that gap in mind
(see ADR-0012's promoted "Consequences" section for what this means for the recall number
specifically).

## Feature 1a's two labeled artifacts, both from this one dataset

1. **Near-duplicate pair labels** (ADR-0012 update): every within-block pair (same source
   photo, however attacked) is a positive; every cross-block pair is a negative.
   `evals/ai_fixtures/copydays_loader.py::all_pairs` derives all ~74k pairs; the block
   convention (`BBBBSS.jpg` — first 4 digits = block, last 2 = `00` for the original, `01+` for
   attacked variants) was verified against the real extracted files, not assumed from
   documentation.
2. **Keep-best preference labels**: within each of the 157 blocks, the unmodified original
   (`SS == "00"`) is the real, non-fabricated "should be kept" ground truth relative to its
   print-and-scanned/blurred/painted derivatives — these are quality-degrading operations by
   construction, not an assumption Reclaim invented. `evals/test_ai_copydays_gold.py::
   test_keep_best_against_copydays_original_vs_attacked` uses this directly (see ADR-0012's
   companion update for the numbers).

## What was explicitly NOT done, and why

- **No AVA (or similar) general-aesthetic-correlation check.** GG's instruction named AVA as an
  example source for validating the classical scorer's *general* quality signal (independent of
  the keep-best "which specific copy" question). AVA itself is ~255,500 images, distributed as a
  32GB torrent or a 49GB single-file Hugging Face zip (`Iceclear/AVA`) sourced from
  `dpchallenge.com` photo-contest submissions — each image is an individual photographer's
  copyrighted contest entry; the HF re-uploader's own `apache-2.0` tag on their packaging does
  not resolve the underlying per-photographer copyright status of the source images the way
  INRIA's own blanket "as-is" grant does for Copydays. Given the size (two orders of magnitude
  larger than Copydays for a *secondary*, directional sanity check, not the primary
  threshold-selection gate) and the murkier licensing, this was skipped rather than partially
  and inconsistently sampled. This is a disclosed scope decision, not a silent gap: the more
  operationally important half of GG's keep-best instruction — real quality-ordered near-dup
  pairs, evaluated with residual disagreements surfaced for optional human confirmation, never
  fabricated by an LLM — **was** completed, using Copydays' own original-vs-attacked structure
  (see above).
- **No LLM-as-labeler fallback anywhere in this feature.** GG's instruction permits an audited
  LLM-labeler fallback only where no public human-verified dataset exists for a given signal.
  For Feature 1a, both required signals — near-duplicate ground truth and keep-best preference
  ground truth — are fully covered by Copydays' real, construction-verified labels. No LLM was
  used to generate, pre-label, or audit anything in this feature. (Build-order items 1b, Track
  B, Feature 2, and Feature 3 are explicitly on hold per GG's instruction and get their own
  per-feature assessment when their turn comes — this ADR does not attempt to answer for them.)

## Consequences

- Feature 1a's operating point is now grounded in real, human-construction-verified data instead
  of synthetic fixtures or GG's own (still-unlabeled) disk — a meaningfully stronger claim than
  ADR-0012 could make before this.
- The measured recall number (see ADR-0012's update) is depressed by the adversarial-only
  composition of the available real data (`strong` attacks only, no milder `jpeg`/`crop`
  ladder) — this is disclosed as a measurement-coverage gap, not smoothed over.
- If GG's own gold-set labeling (ADR-0014's tool) later produces a mild-consumer-duplicate
  distribution absent here, that data should be used to refine — not silently override — this
  measurement, with its own ADR entry citing GG's gold-set commit, same discipline as this one.
- `evals/ai_fixtures/fetch_copydays.py` and `evals/test_ai_copydays_gold.py` are checked in and
  reproducible, but deliberately **not** part of the default CI sweep (`.github/workflows/
  eval.yml` runs `pytest evals/`, and this test file's `pytestmark` skips cleanly whenever the
  dataset isn't present locally) — a ~268MB third-party network download has no business being
  a CI dependency that must succeed on every push; it is a manual, on-demand reproduction step,
  the same posture this repo already uses for real-disk validation (`data/real-disk-run/`).

## Alternatives considered

- **Wait for GG's own labels instead.** Rejected — GG's explicit instruction for this pass was
  to source from public human-labeled data *first*, specifically to avoid grounding the shipped
  default in one person's disk before a public, broadly-representative source was ruled out.
- **Use the unofficial Hugging Face `trust_remote_code=True` mirror.** Rejected — see "Download
  mechanics" above; a direct, checksum-verified HTTPS path made the remote-code-execution
  tradeoff unnecessary.
- **Bulk-download AVA anyway, accept the size/licensing tradeoff.** Rejected — disproportionate
  to a secondary sanity check, and the licensing ambiguity (per-photographer copyright under an
  uploader-asserted `apache-2.0` wrapper) doesn't clear the same bar Copydays clears cleanly.

## Test coverage / reproduction

```
uv run python evals/ai_fixtures/fetch_copydays.py   # idempotent, checksum-verified download
uv run pytest evals/test_ai_copydays_gold.py -v -s   # real PR curve + keep-best measurement
```

`evals/ai_fixtures/copydays_loader.py` has no dedicated unit test file of its own — it's
exercised end-to-end by `evals/test_ai_copydays_gold.py` against the real dataset, which is the
only environment where "does this loader correctly parse Copydays' real filenames" is a
meaningful question to ask (a synthetic fixture standing in for Copydays' own naming convention
would just be re-asserting the convention, not testing the loader against it).
