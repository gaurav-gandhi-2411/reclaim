# Reclaim — Applied AI Feature Plan & Evaluation Protocol

Reference spec for the AI/ML layer. Do NOT start implementation until the Stage 1
security work is committed. This document defines what to build, how it is
architected, and — the part that makes it real applied AI rather than rules —
how each feature is evaluated with labeled data and a measured operating point.

---

## 0. Non-Negotiable Principles (apply to every feature below)

1. **Recommend-only.** The AI layer NEVER auto-deletes and NEVER feeds the
   deterministic auto-delete path. Every AI output lands in a separate AI review
   queue with raw scores/distances shown. The deterministic engine + SafetyValidator
   remain the sole authority for any deletion. This is enforced by an integration
   test (see §7.5), not just convention.
2. **Local, offline, zero-cost.** All models run on-device. No paid APIs, no
   ANTHROPIC_API_KEY, no content leaves the machine. Bundled models must be
   license-clean for redistribution (record each license in the ADR).
3. **CPU-first, GPU-optional.** Every model must run acceptably on CPU. GPU is a
   speedup, never a requirement. Document CPU-only latency in the cost eval.
4. **Two-stage compute, always.** Cheap deterministic/hash prefilter → expensive
   embedding/model only on the residual. Same "filter before you spend, before I/O
   or FLOPs" lesson the deterministic engine learned four times. Never embed a file
   a hash could have excluded.
5. **Embedding cache mandatory.** SQLite cache keyed by (path, size, mtime,
   model_id). Never recompute an unchanged file. Peak RAM must be independent of
   file count (stream, don't materialize).
6. **No manufactured confidence.** A feature outputs a probability ONLY if it is
   calibrated against labels (ECE + reliability diagram in the eval). Otherwise it
   reports a raw distance/score, explicitly labeled as such.
7. **Near-identical ≠ semantically-similar.** These are separate pipelines with
   separate precision bars and separate UI treatment. Never merge them. Only the
   near-identical pipeline may produce a deletion *suggestion*; semantic similarity
   is browse-grouping only.

---

## 1. Feature Tier 1a — Image Similarity + Keep-Best Quality Scoring

**Customer value:** everyone has thousands of phone photos / screenshots synced to
their laptop. Near-duplicate photos (re-saves, resizes, burst shots) are large,
numerous, and invisible to exact-hash dedup. This is the highest-value AI feature.

### Pipeline (two-stage, two-track)
- **Stage 0 (deterministic prefilter):** exact-hash dedup (BLAKE3) already removes
  byte-identical. Remaining images enter the AI track.
- **Track A — near-identical (dedup-grade):**
  - Perceptual hash (pHash + dHash via `imagehash`) → cluster by Hamming distance.
  - This is a *hash*, not ML; it is the cheap prefilter. Produces candidate
    near-dup clusters.
  - For clusters within a tight Hamming band, this alone is a deletion *suggestion*
    (review-only), because near-pixel-identical is a defensible "keep one" case.
- **Track B — semantic grouping (browse-only, NEVER a delete suggestion):**
  - Image embeddings via **OpenCLIP ViT-B/32** (or MobileCLIP for lower-end CPUs;
    pick via the cost eval). Cache embeddings.
  - ANN index (**hnswlib** or FAISS — reuse existing FAISS familiarity) for
    nearest-neighbor grouping by cosine similarity.
  - Output: "you have ~40 photos of the same scene" groups, surfaced for the user
    to browse and manually prune. No automated keep/delete recommendation.
- **Keep-best quality scorer (applies to Track A clusters):**
  - v1 classical, no license/size risk: sharpness (Laplacian variance), resolution,
    exposure/histogram spread, file size. Combined into a transparent quality rank.
  - v2 optional upgrade: **NIMA (Neural Image Assessment)** pretrained aesthetic
    score — only add if the eval (§7.2) shows it beats the classical scorer on
    human-agreement, and only if its license permits redistribution. Do not add
    NIMA "because it's a neural net"; add it only if measured to help.
  - The scorer recommends which copy to KEEP; it never deletes. Safety metric:
    it must essentially never pick the worst-quality member as the keeper.

### Models & licenses (verify before bundling)
OpenCLIP ViT-B/32 (permissive), imagehash (BSD), opencv (Apache-2.0),
NIMA weights (check — optional). Pin versions; models bundled or first-run-downloaded
from a pinned source, then offline.

---

## 2. Feature Tier 1b — Document Near-Duplicate & Version-Chain Detection

**Customer value:** multiple near-copies of resumes, reports, decks
("final_v2_FINAL.docx"), old exports. Prosumer-valuable.

### Pipeline (two-stage)
- **Stage 0 prefilter:** exact-hash removes identical. Text extracted locally
  (docx/pdf/txt/md) — extraction is privacy-sensitive, stays on device, never logged.
- **Stage 1 — near-duplicate (cheap):** **MinHash / LSH** over text shingles
  (`datasketch`) → candidate near-dup clusters at scale. This is the workhorse.
- **Stage 2 — semantic resolution (residual only):** for clusters MinHash can't
  cleanly resolve, **sentence embeddings** (`all-MiniLM-L6-v2`, Apache-2.0, tiny,
  CPU-fast; or `bge-small-en-v1.5`). Cosine similarity to confirm/split clusters.
- **Version-chain detection:** filename patterns (`v1/v2/final/(1)/copy`) + content
  similarity + mtime → order the chain, recommend keeping the latest, surface the
  older ones for review (never auto-delete — a "final" may be the one that matters).

### Output
Near-dup clusters and ordered version-chains in the AI review queue, with pairwise
similarity shown as a raw number. Deletion suggestions only for high-similarity
near-dups; version-chains are ordered recommendations, human-confirmed.

---

## 3. Feature Tier 2 — Screenshot Burst + OCR Content-Awareness

**Customer value:** screenshots are a top clutter source; content-awareness lets the
tool avoid recommending removal of screenshots that hold real information (receipts,
confirmations) vs transient UI captures.

### Pipeline
- **Burst detection (deterministic + pHash):** dimensions == a known screen
  resolution + capture-time proximity + pHash distance ≤ threshold → burst cluster.
- **OCR content tag (local):** Tesseract or **rapidocr (ONNX, CPU)** extracts text;
  a lightweight classifier/heuristic tags category (receipt / document / code /
  chat / transient-UI). Tag informs review priority, never an auto-action.
- **Privacy hard rule:** OCR output (which may contain passwords, 2FA codes,
  personal data) is NEVER logged, NEVER surfaced outside the local review view,
  NEVER persisted beyond an on-device tag. This is a strict requirement, tested.

### Risk note
This feature "understands content," which is high-value but the highest-risk AI
feature for a deletion tool. Keep it recommend-only and conservative; when a
screenshot is content-tagged as receipt/document/code, bias STRONGLY toward keep.

---

## 4. Feature Tier 3 — Feedback-Driven Clutter Prioritization (Learning-to-Rank)

This is the honest version of the spec's "learns from your decisions / gets smarter."
It is real ML (a learned ranker), but it is **label-gated with an explicit cold
start** — no fake online learning on a handful of decisions.

### Design
- **Cold start (no model):** until enough labels exist, surface review candidates
  by a transparent heuristic priority (size × mtime-staleness × location-weight ×
  cluster-membership). Clearly labeled "heuristic," not "AI."
- **Feedback store:** every accept/reject/keep decision logged with its feature
  vector (size, ext, path-class, mtime/ctime, cluster stats, category, cloud-sync
  flag, sibling-decision context). No atime dependence (unreliable on NTFS).
- **Learned ranker (activates at N ≥ 500 labeled decisions):** **LightGBM
  LambdaMART** learning-to-rank (reuses existing LightGBM expertise). Ranks review
  candidates by predicted likelihood the user removes them, surfacing likely clutter
  first and reducing review effort. It ranks a review queue; it NEVER auto-deletes.
- **Evaluation is time-split** (train on past decisions, evaluate on future ones) to
  avoid leakage — the same disjointness discipline as the other projects.
- **Cold-start honesty:** the doc, the UI, and any pitch state plainly that the
  ranker is inactive until the label threshold is met; before that it's a heuristic.

### Why this ordering
A supervised importance/staleness model with no labels is the fabricated-confidence
trap. Logging decisions now and training a ranker later, evaluated on held-out future
decisions, is the statistically valid path and the genuinely defensible applied-AI
story.

---

## 5. What Stays Deterministic (explicitly NOT "AI" — do not over-claim)

Exact duplicates (BLAKE3), package/model/browser caches, temp, crash dumps, dev
artifacts, installer supersession (filename version parsing), archive-extraction
pairing, materiality gating, hardlink-aware reclaimable accounting, SafetyValidator.
These are rules/hashing and must be described as such. The AI layer sits beside them,
not on top of them.

---

## 6. Architecture (extends the existing modular design)

New components, all feeding the AI review queue only:

- **EmbeddingService** — pluggable backends (image-CLIP, text-MiniLM, OCR). SQLite
  embedding cache keyed (path, size, mtime, model_id). Streaming, flat RAM.
- **SimilarityEngine** — two-stage per feature: hash/MinHash prefilter → ANN
  (hnswlib/FAISS) over embeddings on the residual. Emits clusters + raw distances.
- **QualityScorer** — keep-best ranking for image near-dup clusters (classical v1,
  NIMA optional v2 if measured to help).
- **ContentTagger** — local OCR + category tag; privacy-locked output.
- **FeedbackStore** — decision log with feature vectors; feeds the ranker.
- **RankerTrainer** — LightGBM LambdaMART; activates at label threshold; time-split
  eval; artifacts + metrics recorded.
- **AIReviewQueue** — separate from deterministic candidates; recommend-only;
  surfaces near-identical (deletion-suggestion-grade), semantic groups (browse-only),
  version-chains, screenshot bursts, and ranked clutter, each with raw scores.
- **EvalHarness** — per-feature datasets, metrics, PR-curve operating-point
  selection, CI quality gates (see §7).

Flow: deterministic engine and AI layer run independently. AI results NEVER merge
into the auto-delete candidate set. SafetyValidator exclusions (system paths, git
repos, environments, cloud placeholders, model caches) apply to AI candidates too.

---

## 7. Evaluation Protocol (the core of this spec)

Each feature ships with a labeled eval and a data-chosen operating point recorded in
an ADR. A feature is not "done" until its eval passes its CI gate. Precision is
favored over recall throughout: a false "these are duplicates" that leads to a bad
keep/delete is the expensive error.

### 7.1 Datasets
- **CI fixtures (checked in, synthetic, deterministic):** small labeled sets per
  feature for regression gating — near-dup image sets, near-dup doc sets, version
  chains, screenshot bursts, a labeled decision log. Enable precision-floor gates
  in CI without needing private data.
- **Local gold set (GG hand-labels, on-device, privacy-safe):** a few hundred real
  pairs/clusters from GG's own disk. This is the set operating points are chosen
  from — real distributions, not synthetic. Budget the labeling time explicitly.
- **Disjointness:** threshold-selection set and evaluation set are disjoint. For the
  ranker, time-split (past → train, future → eval). No leakage.

### 7.2 Metrics (per feature)
- **Image/doc clustering:** BCubed precision/recall (cluster-appropriate, better
  than pairwise) + a pair-level precision-recall curve for threshold selection.
- **Keep-best quality:** top-1 agreement with human choice + a safety metric
  ("never selects a bottom-quartile-quality member as keeper"), which matters more
  than raw agreement.
- **Version-chain ordering:** exact-order accuracy and Kendall's tau vs human order.
- **Screenshot content tag:** per-class precision/recall; the operational metric is
  "recall of keep-worthy content" (receipts/docs must not be tagged transient).
- **Learning-to-rank:** NDCG@k and precision@k of surfaced clutter, plus
  "wasted-review-rate" (fraction of surfaced items the user keeps) as the
  product-level metric.
- **Calibration (only if a probability is exposed):** ECE + reliability diagram.
  Default is to expose raw distances, not probabilities.

### 7.3 Operating-point selection
Thresholds (pHash Hamming band, CLIP/MiniLM cosine cutoff, MinHash Jaccard) are
chosen from the PR curve on the gold set at a target precision — NOT hand-set. Target
precision: near-identical/deletion-suggestion tracks ≥ 0.95; semantic/browse tracks
may be lower (they're non-destructive). Every chosen threshold + its PR curve is
recorded in an ADR. This data-driven operating point is the line between applied AI
and a magic constant.

### 7.4 CI quality gates
Per-feature precision floor asserted in CI against the fixture set; build fails on
regression (same posture as the TriageIQ eval gates). Clustering purity floor.
Ranker NDCG floor once active.

### 7.5 Safety eval (mandatory, non-negotiable)
An integration test asserting that AI-layer candidates can NEVER reach the
auto-delete path and NEVER appear in the deterministic Tier-A set — under any config,
including adversarial. AI is provably recommend-only. This is the single most
important test in the AI layer.

### 7.6 Cost / performance eval
CPU-only embedding throughput (items/sec), cache hit-rate on rescan, peak RAM vs
file count (must be flat), incremental rescan cost (< small % of full). Scale test on
a large synthetic image/doc corpus. A feature that can't run CPU-only at acceptable
cost is not shippable, regardless of quality.

### 7.7 Reporting
Every metric reported with the command + fixture/gold-set + commit that produced it.
Numbers over adjectives. Distinguish measured from believed.

---

## 8. Build Order (after Stage 1 security lands)

1. EvalHarness + fixtures + the §7.5 safety eval FIRST — the harness and the
   recommend-only guarantee exist before any model is wired in.
2. Feature 1a Track A (pHash near-identical) + keep-best classical scorer — highest
   value, mostly hash, quickest real win with a clean eval.
3. Feature 1b (MinHash doc near-dup + version-chain).
4. Feature 1a Track B (CLIP semantic grouping) — only after Track A proves the
   pipeline; measure whether embeddings earn their compute.
5. Feature 2 (screenshot burst + OCR) — with the privacy lock tested.
6. Feature 3 (feedback logging now; LambdaMART ranker gated at label threshold).

Each stage: executor → verifier → eval gate green → ADR for the operating point →
commit. No feature merges without its eval passing.

---

## 9. Honest Positioning

The applied-AI value here is a **local-first, privacy-preserving similarity +
learning-to-rank system with a rigorous labeled-eval methodology and a
provably-recommend-only safety boundary** — not "AI that decides what to delete."
Pitch the eval rigor and the safety architecture; that is the defensible,
principal-grade story. Do not claim autonomous AI deletion — the tool deliberately
does not do that, and the deliberate choice is the stronger narrative.
