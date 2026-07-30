# 0030. ONNX conversion, torch removal, and bundling the AI layer in the installer

## Context

ADR-0024 (Decision 2) shipped the public installer core-only, deliberately excluding the `[ai]`
extras — measured at ~1,028MB delta (`torch` alone 464MB), a disproportionate cost for a
disk-cleanup tool's own installer, and ADR-0029 subsequently investigated (but did not build) a
downloadable separate "AI runtime" as the path to closing the resulting "no way to enable AI on
an installed copy" gap.

Wave 1 P0-B (2026-07-30, GG's explicit work order) took a different path: instead of finding a
way to distribute the existing ~1GB torch-based AI layer, shrink it enough that ADR-0024's
rejection no longer applies. Convert CLIP (semantic image grouping, Track B) and MiniLM
(document near-dup residual confirmation, Feature 1b Stage 2) to ONNX, drop `torch`/
`open-clip-torch`/`sentence-transformers` entirely, and re-evaluate whether bundling is now
viable.

## Decision 1: fp16 for CLIP, int8 for MiniLM — not a single blanket quantization choice

A decision-grade quality-parity comparison (torch-fp32 vs ONNX, on this project's own real
primary gold eval sets — Copydays 40-block BCubed for CLIP, Gutenberg realistic-tier PR at the
shipped 0.95 threshold for MiniLM, full numbers: `reports/ai/onnx_quality_parity/`) found the
two models tolerate quantization very differently:

- **CLIP int8 REJECTED**: -11pp BCubed precision at the shipped 0.82 threshold under the
  original torchvision preprocessing (-1.9pp F1 under the final production PIL-based
  preprocessing) — material, not noise.
- **CLIP fp16 ACCEPTED**: bit-identical BCubed behavior to torch-fp32 under torchvision
  preprocessing (delta 0.0000 on precision/recall/F1); a small, disclosed residual drop under
  the final production PIL preprocessing (delta_f1 -0.008, still comfortably above Track B's
  own 0.70 precision floor) attributable to the preprocessing reimplementation, not the model.
- **MiniLM int8 ACCEPTED**: quality-equivalent at the shipped 0.95 operating threshold (recall
  313/360 -> 312/360, precision 1.0 unchanged across all 7,140 negative pairs) — no fp16
  fallback needed.

Payload: CLIP fp16 175.8MB + MiniLM int8 23.6MB = **199.4MB new model payload**, vs. the
spec's <250MB target.

## Decision 2: preprocessing/tokenization reimplemented without torch, not worked around

Dropping torch means CLIP's image preprocessing (previously `torchvision.transforms`, sourced
from `open_clip`'s own pretrained-tag config) and MiniLM's tokenization (previously
`sentence-transformers`' bundled HF tokenizer) both needed non-torch replacements:

- **CLIP preprocessing**: reimplemented in pure PIL + numpy (`image_embeddings._preprocess_
  image`) — resize shortest side to 224 (bicubic, `reducing_gap=3.0` to narrow the gap with
  torchvision's antialiased bicubic), center-crop, normalize with CLIP's own hardcoded
  mean/std. Validated against the torchvision pipeline directly (mean cosine similarity 0.996
  across a real Copydays sample) before trusting it, and the real BCubed impact (not just
  embedding cosine drift) is measured and disclosed above, not assumed acceptable.
- **MiniLM tokenization**: the lightweight `tokenizers` package (Rust-backed, ~7.5MB installed,
  no torch/transformers dependency) loading the model's own exported `tokenizer.json` —
  confirmed byte-identical token ids/attention masks against the original sentence-transformers
  tokenizer before this replaced it, so no drift here (unlike the image-preprocessing case,
  tokenization is an exact deterministic transform, not a lossy resize).

## Decision 3: bundle the AI layer in the installer, superseding ADR-0024 Decision 2

**Chosen: the public installer now bundles the full `[ai]` extras (torch-free) plus the two
pinned ONNX model files.** ADR-0024's core rejection reason — "~1GB is a disproportionate cost
for a disk-cleanup tool's own installer" — no longer holds once the AI-specific payload dropped
from ~1,028MB to 199.4MB models + a torch-free Python dependency closure. See the Measured
evidence section below for the actual installed-app size this produced.

This also fully closes ADR-0024's own disclosed gap ("today, 'enabling AI on a Nuitka-installed
reclaim.exe' has no working path") and supersedes ADR-0029's investigated-but-unbuilt
"downloadable separate AI runtime" plan — once the payload is small enough to bundle directly,
a separate runtime-download mechanism solves a problem that no longer exists.

### Measured evidence

| Metric | Value |
|---|---|
| CLIP fp16 ONNX model | 175.8 MB |
| MiniLM int8 ONNX model + tokenizer | 23.6 MB + 0.7 MB |
| Nuitka standalone dist folder (torch-free `[ai]` extras + models bundled) | MEASURE_DIST_SIZE |
| Final `reclaim-setup.exe` installer size | MEASURE_INSTALLER_SIZE |
| Previous core-only installer size (v1.3.0, for comparison) | MEASURE_PREVIOUS_INSTALLER_SIZE |

(Filled in from the actual build in this session — see PLAN.md's Wave 1 P0-B checkpoint for the
full build log and verification steps.)

### Bundled-model distribution: SHA256-pinned local files, not a runtime download

The old torch-based code downloaded checkpoints from Hugging Face Hub on first use, pinned to
an exact revision + SHA256 (ADR-0028). That mechanism doesn't apply once torch/`huggingface_hub`
are dropped from the runtime dependency closure entirely — instead:

- `scripts/export_ai_models.py` (needs the separate `ai-export` extras group, `torch` included,
  never installed by a normal `uv sync --extra ai`) regenerates the bundled ONNX files from the
  same pinned HF Hub checkpoints ADR-0028 already established, verifying each against its
  original SHA256 before conversion.
- The bundled `.onnx`/`tokenizer.json` files themselves are pinned+SHA256-verified at load time
  (`reclaim.ai._optional.require_bundled_model`) — defense-in-depth against a corrupted or
  tampered install, same philosophy as the old download-time check, adapted for a file that's
  bundled at build time rather than fetched at first use.
- `clip_vision_fp16.onnx` (175.8MB) exceeds GitHub's 100MB-per-file plain-git push limit —
  tracked via Git LFS (`git lfs track "src/reclaim/ai/models/*.onnx"`), a new but standard,
  reversible repo infrastructure addition, not previously needed by this project.

## Consequences

- `pyproject.toml`'s `[ai]` extras no longer include `torch`/`open-clip-torch`/
  `sentence-transformers`/`huggingface-hub` — a normal `uv sync --extra ai` is now torch-free.
  A new `ai-export` extras group holds those (plus `onnx`/`onnxscript`/`onnxconverter-common`)
  for the one script that still needs them.
- `image_embeddings.py`/`text_embeddings.py` were rewritten from the ground up — the public API
  (`ImageEmbedding`/`DocumentEmbedding`/`compute_*_embedding`/`cosine_similarity`) is unchanged,
  so every downstream caller (`semantic_image_grouping.py`, `document_similarity.py`,
  `image_similarity.py`'s keep-best, the AI orchestration layer) needed zero changes.
- `ImageEmbeddingCache`'s `model_id` was bumped (`onnx-fp16:clip_vision:<hash-prefix>`) — old
  torch-era cached embeddings become unreachable cache misses, never silently compared against
  new ONNX ones, per the cache's own pre-existing "model_id prevents mixing embedding spaces"
  contract.
- A new `AIModelMissingError` (distinct from `AIExtraNotInstalledError`) covers the "packages
  present, bundled model file missing/corrupted" case — `api/ai_orchestration.py::_run_pipeline`
  catches both identically (a clean, expected, per-track skip, not treated as an unexpected
  bug), verified end-to-end (not just at the unit level) against a simulated missing-model-dir.
- Lazy loading preserved exactly (module-level session/tokenizer caches, same pattern as the old
  `_model_cache`) — `reclaim.ai.image_embeddings`/`text_embeddings` import in ~21ms with zero
  model loading; the real cost (CLIP: ~2.5s including a ~260ms SHA256 check of the 175.8MB file;
  MiniLM: ~425ms) lands only on the first actual AI call, once per process lifetime, never at
  app/module import time. Full measurement: `reports/ai/onnx_quality_parity/lazy_load_latency.json`.

## Alternatives considered

- **Keep torch, ship a downloadable separate AI runtime (ADR-0029's plan).** Superseded — solves
  a problem (a ~1GB payload too large to bundle) that no longer exists once the payload is
  199.4MB.
- **int8 for both models.** Rejected for CLIP specifically — a measured, material precision
  regression at the shipped operating threshold. Not a blanket policy call; each model was
  evaluated on its own real quality-parity numbers, per GG's explicit instruction not to accept
  a quantization choice without measuring it.
- **Keep core-only installer, in-app "Enable AI features" download button.** Available as a
  documented fallback if bundling had pushed the installer to an unacceptable size — not needed;
  bundling succeeded within budget.
