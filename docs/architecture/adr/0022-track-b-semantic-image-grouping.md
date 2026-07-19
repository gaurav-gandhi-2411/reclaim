# 0022. Feature 1a Track B: CLIP semantic image grouping (browse-only)

## Context

GG's build instruction: Track B per the AI spec §1 — browse-only, NEVER a deletion
suggestion (the hard line: `SEMANTIC_IMAGE` stays browse-only, enforced by the existing
safety gate). OpenCLIP ViT-B/32 (or MobileCLIP — pick via a cost eval), CPU-first, embeddings
cached in SQLite keyed `(path, size, mtime, model_id)`. Two-stage: Track A's pHash near-
identical clustering already handles the near-duplicate case; Track B groups the RESIDUAL by
cosine similarity via an ANN index into "same scene/event" browse groups. Grouping quality
(BCubed P/R) measured on a realistic set, disclosed distribution, per-tier gated, precision
bar looser than dedup (browse-tidiness, not a deletion decision) but still real and reported.

## Model choice: OpenCLIP ViT-B/32, not MobileCLIP

| candidate | code license | weights license | install | verdict |
|---|---|---|---|---|
| **MobileCLIP** (Apple) | MIT | **Apple "ML Research Model" Terms of Use** — explicitly excludes "commercial exploitation, product development, or use in any commercial product or service"; any derivative model inherits the same restriction; Apple can revoke at will | not pip-installable — clone the repo + manual checkpoint download | **Rejected** — a hard commercial-use prohibition, not ambiguity, on top of failing the pip-only install bar |
| **OpenCLIP ViT-B/32** | MIT | MIT (both the original OpenAI-released weights and the LAION-trained checkpoints used through `open_clip`'s hub are MIT) | `pip install open_clip_torch`, ~338MB checkpoint, zero gating | **Selected** |

Same license discipline as every prior rejection in this project (QQP's non-commercial ToS,
the unofficial Copydays HF mirror's `trust_remote_code` loader, SROIE/RVL-CDIP's provenance
ambiguity) — MobileCLIP's restriction is actually less ambiguous than those cases (the TOU
text is explicit), which makes it an easier, not harder, call.

**A real bug caught and fixed during the build, not shipped silently**: `open_clip`'s default
`"ViT-B-32"` model config uses standard GELU activation, but the `"openai"` pretrained
checkpoint was trained with QuickGELU — pairing them logs a `UserWarning: QuickGELU mismatch`
and (more importantly) produces a genuinely different, degraded embedding space, not just a
cosmetic warning. Fixed by loading the `"ViT-B-32-quickgelu"` model variant instead (verified:
`warnings.simplefilter("error")` around a real embedding call produces zero warnings after
the fix, confirming the mismatch is actually resolved, not merely silenced).

## ANN library: FAISS, not hnswlib

The spec named "hnswlib/FAISS." `hnswlib` was tried first and rejected on pure
installability grounds: no prebuilt wheel exists for this Python/platform combination, and
building from source requires Microsoft Visual C++ Build Tools (not installed) — exactly the
"needs a system binary" problem this project's dependency discipline has avoided everywhere
else (the same reasoning that chose `rapidocr-onnxruntime` over Tesseract for Feature 2).
`faiss-cpu` (MIT, prebuilt Windows wheel) was used instead — and it ships its own `HNSW`
index implementation (`faiss.IndexHNSWFlat`), so the actual algorithm the spec names (HNSW,
Hierarchical Navigable Small World — genuine approximate nearest-neighbor search, not exact
brute-force) is still what runs; only the library changed, not the approach.

## Architecture: embedding cache + two-stage grouping

`image_embeddings.py`: `ImageEmbeddingCache` (SQLite, keyed `(path, size_bytes, mtime,
model_id)` exactly as specified — a cache hit requires all four to match, so a changed file
or a changed model checkpoint never silently reuses a stale/wrong embedding),
`compute_image_embedding`/`compute_embeddings_batch` (real CLIP forward pass, `None` — not an
error — for an undecodable file, same "skip, don't abort" posture as every other AI-layer
compute function), `cosine_similarity` (raw, never a manufactured probability, spec §0.6).

`semantic_image_grouping.py`: `group_by_semantic_similarity` (pure algorithm — FAISS HNSW
index, L2-normalized vectors so inner-product search equals cosine similarity, union-find
clustering on any pair clearing `similarity_threshold`), `build_semantic_image_clusters`
(orchestration — safety-filter → embed → group → `AICluster`, mirroring
`image_similarity.build_near_identical_clusters`'s exact shape, minus any keep-best step and
minus any `is_recommended_keep` ever being set).

**Two-stage discipline (spec §0.4), the caller's responsibility, documented not assumed**:
`build_semantic_image_clusters` explicitly requires `residual_image_paths` to already be the
residual after Track A's own near-identical clustering — this module never re-examines images
Track A already clustered.

## Measured: grouping quality (BCubed P/R) on real Copydays blocks

Reproduce:
```
uv run python evals/ai_fixtures/fetch_copydays.py   # if not already downloaded
uv run pytest evals/test_ai_semantic_grouping_gold.py -v -s
```

Reused the same real, license-cleared INRIA Copydays corpus ADR-0012/0015 already downloaded
for Track A — each Copydays "block" (one original photo + its real print-scan/blur/paint
attacked derivatives) treated as a real "same underlying photo" ground-truth cluster.
**Honestly disclosed as a proxy, not the exact target distribution**: Copydays' attacks are
adversarial transformations of the SAME photo, not genuinely different photos of the same
scene/event (Track B's actual target — e.g. several distinct vacation photos from one beach
visit); `DistributionDeclaration.untested_variation_note` states this explicitly. 40 of ~157
available blocks were used (a deterministic subset, sized for tractable real-CLIP-inference
runtime, not silently narrowed — the exact count is disclosed in the report).

**BCubed precision: 0.7897, recall: 0.7143, at similarity threshold 0.82** (98 images across
40 blocks — `uv run pytest evals/test_ai_semantic_grouping_gold.py -v -s`, commit `e3d22d2`).
Gated on precision ≥ 0.70 (looser than dedup's 0.95, per spec's own explicit framing that
Track B's precision bar is browse-tidiness, not a deletion decision) AND recall ≥ 0.20 (a
modest usefulness floor) — both cleared with real margin (recall in particular is strong,
0.71 vs. the 0.20 floor).

**The full precision/recall tradeoff curve, swept 0.70–1.00, tells an honest and useful
story, not just the single selected point:**

| threshold | precision | recall | F1 |
|---:|---:|---:|---:|
| 0.70 | 0.1533 | 0.9218 | 0.2629 |
| 0.76 | 0.3461 | 0.8656 | 0.4945 |
| 0.80 | 0.6150 | 0.7755 | 0.6860 |
| **0.82** | **0.7897** | **0.7143** | **0.7501** |
| 0.86 | 0.9694 | 0.5816 | 0.7270 |
| 0.90 | 1.0000 | 0.4592 | 0.6294 |
| 1.00 | 1.0000 | 0.4082 | 0.5797 |

Below ~0.80, precision collapses fast (CLIP's semantic similarity is genuinely permissive at
loose thresholds — many Copydays cross-block pairs share enough scene-level similarity to
get grouped together, a real false-positive risk this measurement exists to catch, not an
artifact). Above ~0.90, precision is perfect but recall plateaus around 0.41–0.46 — the
ceiling on how many attacked derivatives CLIP's embedding space can still recognize as "the
same photo" once the print-scan/blur/paint attacks are severe enough (Copydays' own hardest
cases). 0.82 sits at the F1-maximizing knee of the curve and is also the selected operating
point under the "maximize recall subject to both floors" rule (ADR-0018's selection
philosophy, applied here to a single-distribution BCubed sweep rather than a multi-tier
precision/recall search).

## Safety: browse-only, structurally enforced, §7.5 gate re-verified

`AITrack.SEMANTIC_IMAGE` was ALREADY excluded from `_DELETION_SUGGESTION_ELIGIBLE_TRACKS`
since ADR-0011 ("browse-only, future") — unchanged by this ADR, which only implements the
track spec had already reserved a browse-only slot for. `build_semantic_image_clusters` never
sets `is_recommended_keep` on any member, and `AICluster.__post_init__`'s existing structural
guard (not a new check written for Track B) would refuse construction if a future bug tried
to. Two regression tests: `test_semantic_image_track_never_suggests_deletion_even_with_a_
high_similarity` (a near-perfect 0.99 cosine similarity still never flips `suggests_deletion`
— the strongest possible statement: not just "usually browse-only," genuinely incapable of
becoming a deletion suggestion regardless of how confident the similarity signal is) joins the
pre-existing `test_browse_only_track_cannot_carry_a_deletion_suggestion` (which already used
`SEMANTIC_IMAGE` as its literal example track, since ADR-0011). `evals/test_ai_safety_gate.py`
re-ran clean at 19/19 with `image_embeddings.py`/`semantic_image_grouping.py` automatically
covered by the existing AST scan (no `reclaim.executor`/`send2trash` import) via the same
`src/reclaim/ai/**` `rglob` every prior feature's modules are covered by.

## Zero-cost/local + license summary of new dependencies

- `open-clip-torch>=3.3.0` — MIT — CLIP ViT-B/32 embeddings, CPU inference.
- `torch>=2.0` — Apache-2.0/BSD/MIT component mix (PyTorch's own permissive summary) —
  already a transitive dependency of `sentence-transformers` and `open-clip-torch`; listed
  explicitly since imported directly via `require("torch", ...)`.
- `faiss-cpu>=1.14.3` — MIT — ANN search (ships its own HNSW implementation, see above).

No paid API, no GPU requirement, no network access at runtime once the CLIP checkpoint is
cached locally (`open_clip` downloads it once via Hugging Face Hub, then runs fully offline —
same pattern as `sentence-transformers`' `all-MiniLM-L6-v2`).

## Consequences

- The measured BCubed numbers above are a real, disclosed proxy measurement — a future
  re-measurement against a genuinely public scene-grouped dataset (e.g. a licensed subset of
  a photo-album/event-clustering corpus, if one with a clean license is ever found) would be
  a legitimate upgrade, not a correction of an error.
- Branch-only (`feat/ai-track-b`), unmerged, per GG's explicit instruction — this ADR is the
  report due before merge is even considered, not a claim merge has happened.
- `data/ai_datasets/copydays/` stays gitignored (ADR-0015's existing posture, unchanged);
  `reports/ai/semantic_grouping_operating_point.json` is committed — the provenance-tracked
  summary.

## Test coverage

**Synthetic (CI, every run):** `tests/test_ai_image_embeddings.py` (9 cases — cache
correctness including two dedicated cache-miss proofs for the `(path,size,mtime,model_id)`
key, real-embedding similarity ordering), `tests/test_ai_semantic_image_grouping.py` (7 cases
— pure-algorithm grouping logic on hand-constructed vectors, end-to-end orchestration,
embedding-cache wiring), `evals/test_ai_safety_gate.py` (+1 case for `SEMANTIC_IMAGE`'s
high-similarity-never-suggests-deletion property).

**Real (local, on-demand, not in CI):** `evals/test_ai_semantic_grouping_gold.py` (1 case —
the BCubed measurement on real Copydays blocks). Same not-in-default-CI-sweep posture as
every other gold-set eval in this project (real dataset + real CLIP inference takes several
minutes).
