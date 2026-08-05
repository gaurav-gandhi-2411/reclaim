# Third-party notices

Reclaim is built on open-source software and, for its optional AI layer, open-source pretrained
model weights. This document lists every one of them, their license, and a one-line
redistribution note — required for a tool that is packaged and distributed, not just run from
source.

**Verification method**: every package entry below was verified against this repository's own
`uv.lock`-resolved, installed environment via `importlib.metadata` (the same authoritative,
no-guessing method `docs/architecture/adr/0011-ai-layer-architecture-and-licenses.md`
established) — `uv run python -c "import importlib.metadata as md; ..."` reading each package's
`License-Expression`/`License`/`Classifier` metadata fields directly, not recalled from memory or
inferred from a package's name. Every model-weight entry cites the exact source consulted (a URL
and what it returned) rather than stating a license as fact without a check — see "Model weights"
below.

## What ships where

**The standard Windows installer (`reclaim-setup.exe`) bundles the core dependencies only** — no
AI extra, no model weights (`docs/architecture/adr/0024-stage2-installer-and-ai-bundle-size.md`:
core-only `site-packages` measured at 13.6 MB vs. ~1,042 MB with the AI extra enabled; shipping
~1GB of ML dependencies in a disk-cleanup tool's own installer was rejected as disproportionate).
The AI-extra section below applies only if you separately run `pip install reclaim[ai]` /
`uv sync --extra ai` on the same machine — same disclosure posture as PRIVACY.md's "AI features"
section. This document lists both groups so it stays accurate regardless of which path you took
to get here.

## 1. Core dependencies (always installed)

| Package | Version | License | Redistribution |
|---|---|---|---|
| fastapi | 0.139.0 | MIT | permitted |
| uvicorn[standard] | 0.51.0 | BSD-3-Clause | permitted |
| pydantic | 2.13.4 | MIT | permitted |
| pydantic-settings | 2.14.2 | MIT | permitted |
| structlog | 26.1.0 | MIT OR Apache-2.0 (dual-licensed; either applies) | permitted |
| send2trash | 2.1.0 | BSD-3-Clause | permitted |
| blake3 | 1.0.9 | CC0-1.0 OR Apache-2.0 (dual-licensed; either applies) | permitted |
| jinja2 | 3.1.6 | BSD-3-Clause (Pallets project license) | permitted |

All permissive, no copyleft (GPL/AGPL) terms anywhere in this group's dependency closure.

## 2. `[ai]` extra dependencies (`pip install reclaim[ai]` / `uv sync --extra ai` — optional, not
in the standard installer)

| Package | Version | License | Redistribution |
|---|---|---|---|
| imagehash | 4.3.2 | BSD-2-Clause | permitted |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 | permitted |
| pillow | 12.3.0 | MIT-CMU | permitted |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | permitted |
| scipy | 1.18.0 | BSD-3-Clause (code) — see note below | permitted, with a disclosed carve-out |
| pywavelets | 1.9.0 | MIT AND BSD-3-Clause | permitted |
| datasketch | 2.0.0 | MIT | permitted |
| tokenizers | 0.22.2 | Apache-2.0 | permitted (Wave 1 P0-B — MiniLM WordPiece tokenization, replaces sentence-transformers) |
| python-docx | 1.2.0 | MIT | permitted |
| pypdf | 6.14.2 | BSD-3-Clause | permitted |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 (code) — bundled ONNX models covered separately, see §3 | permitted |
| lightgbm | 4.7.0 | MIT | permitted |
| onnxruntime | 1.27.0 | MIT | permitted (Wave 1 P0-B — CLIP + MiniLM inference, replaces torch/open-clip-torch/sentence-transformers; also already a transitive dependency of rapidocr-onnxruntime, so this is a shared runtime, not a second payment) |
| faiss-cpu | 1.14.3 | MIT | permitted |

All permissive, no copyleft (GPL/AGPL) terms anywhere in this group's *own* dependency closure.

**Wave 1 P0-B (2026-07-30): torch, open-clip-torch, sentence-transformers, and huggingface-hub
were removed from this group entirely.** CLIP and MiniLM now ship as pre-converted, pinned,
SHA256-verified ONNX files bundled directly with the app (see §3) instead of torch models
downloaded from Hugging Face Hub on first use — see
`reports/ai/onnx_quality_parity/` for the quality-parity measurement that justified this and
`image_embeddings.py`/`text_embeddings.py`'s own module docstrings for the technical detail.
`huggingface-hub` (and the torch-based packages above) still appear in the separate
`ai-export` extras group (`scripts/export_ai_models.py`, used only to regenerate the bundled
ONNX files from the original checkpoints — never installed by a normal `uv sync --extra ai`),
not reflected in this table since it's not part of what ships to users.

**scipy's Windows wheel bundles two additional pieces of software statically/dynamically linked
into `scipy.libs\libscipy_openblas*.dll`**, disclosed by scipy's own installed-package `License`
metadata field (read via the same `importlib.metadata` method as everything else in this
document — not asserted from memory):
- **OpenBLAS** — BSD-3-Clause.
- **A GCC runtime library** (`libgfortran`) — GPL-3.0-or-later **WITH the GCC Runtime Library
  Exception, version 3.1**. The exception exists specifically to permit exactly this scenario
  ("propagate a work of Target Code formed by combining the Runtime Library with Independent
  Modules... under terms of your choice") — linking GCC-compiled runtime code into a
  non-GPL/proprietary distribution is the exception's stated purpose, not a gap it leaves open.
  This is the standard, widely-shipped arrangement for scientific-Python Windows wheels (numpy/
  scipy have shipped this way for years); it is disclosed here rather than silently omitted.

**rapidocr-onnxruntime's bundled ONNX models are covered separately** because their license is
not expressed in the wheel's own package metadata (`importlib.metadata` has no field for bundled
non-Python model assets) — see §3.

## 3. Model weights (downloaded or bundled by the `[ai]` extra — not pip-installed Python code)

None of these are pip package metadata; each was verified against its own model-card/repo
source, cited below, not assumed from memory.

| Model | Source (as pinned in this repo) | License | Redistribution |
|---|---|---|---|
| CLIP ViT-B/32 ("openai" QuickGELU checkpoint), converted to fp16 ONNX (`clip_vision_fp16.onnx`, 175.8MB, bundled under `src/reclaim/ai/models/`, Wave 1 P0-B) | Originally Hugging Face Hub `timm/vit_base_patch32_clip_224.openai`, commit `a6f597a30f7b82c51704746581f9a4e41421e878` (pinned per ADR-0028); converted via `scripts/export_ai_models.py`, weights unchanged, precision reduced fp32->fp16 | **Apache-2.0** | permitted |
| all-MiniLM-L6-v2, converted to int8 ONNX (`minilm_int8.onnx`, 23.6MB, plus `minilm_tokenizer.json`, bundled under `src/reclaim/ai/models/`, Wave 1 P0-B) | Originally Hugging Face Hub `sentence-transformers/all-MiniLM-L6-v2`, commit `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (pinned per ADR-0028); converted via `scripts/export_ai_models.py`, weights quantized fp32->int8 (dynamic) | **Apache-2.0** | permitted |
| RapidOCR bundled OCR models (detection/classification/recognition ONNX files, ship inside the `rapidocr-onnxruntime` wheel itself — no separate download) | `RapidAI/RapidOCR` GitHub repo, converted from PaddleOCR | **Apache-2.0** | permitted |

**Wave 1 P0-B (2026-07-30)**: CLIP and MiniLM's weights are unchanged from the sources cited
below (same Apache-2.0 checkpoints) — only the file FORMAT (ONNX instead of a torch
`.safetensors` checkpoint) and PRECISION (fp16/int8 instead of fp32) changed, and only via this
project's own conversion (`scripts/export_ai_models.py`), not a third-party redistribution of
already-modified weights. A quantized/format-converted model derived from a permissively
licensed (Apache-2.0) original remains covered by that same license — Apache-2.0 explicitly
permits creating and redistributing derivative works, which is exactly what a format/precision
conversion is. Quality-parity measurement for the conversion: `reports/ai/onnx_quality_parity/`.

**Sources cited, exactly as consulted:**

- **CLIP ViT-B/32**: `src/reclaim/ai/image_embeddings.py` requests `open_clip.create_model_and_transforms("ViT-B-32-quickgelu", pretrained="openai")`, which ADR-0028 traced (via `open_clip==3.3.0`'s own `get_pretrained_cfg`) to Hugging Face Hub repo `timm/vit_base_patch32_clip_224.openai` — **not** the more obviously-named `openai/clip-vit-base-patch32`. Queried `https://huggingface.co/api/models/timm/vit_base_patch32_clip_224.openai` (2026-07-24): response includes `"tags":[...,"license:apache-2.0",...]` and `"cardData":{"license":"apache-2.0",...}`. **This corrects `docs/architecture/adr/0022-track-b-semantic-image-grouping.md`'s Decision table**, which stated "MIT (both the original OpenAI-released weights and the LAION-trained checkpoints used through `open_clip`'s hub are MIT)" as a general claim about `open_clip`'s hub — the specific pinned checkpoint this codebase actually downloads (`timm/vit_base_patch32_clip_224.openai`, a `timm`-maintained repackaging of the OpenAI weights) declares Apache-2.0 on its own Hugging Face repo, not MIT. Both licenses are permissive and commercial-redistribution-safe, so this correction does not change the redistribution verdict — only the specific SPDX tag recorded here, which should be treated as superseding ADR-0022's more general claim for this specific checkpoint.
- **all-MiniLM-L6-v2**: `src/reclaim/ai/text_embeddings.py` requests `sentence_transformers.SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")`. Queried `https://huggingface.co/api/models/sentence-transformers/all-MiniLM-L6-v2` (2026-07-24): response includes `"tags":[...,"license:apache-2.0",...]` and `"cardData":{...,"license":"apache-2.0",...}`. Matches `docs/architecture/adr/0017-feature-1b-document-near-dup-and-version-chain.md`'s existing Apache-2.0 claim — confirmed, not just carried forward.
- **RapidOCR bundled models**: `rapidocr-onnxruntime`'s own PyPI metadata declares `License: Apache-2.0` for the wheel as a whole. The `RapidAI/RapidOCR` GitHub repo's README ("License" section, `https://raw.githubusercontent.com/RapidAI/RapidOCR/main/README.md`, fetched 2026-07-24) states verbatim: *"The copyright of the OCR model is held by Baidu, while the copyrights of all other engineering scripts are retained by the repository's owner. This project is released under the Apache 2.0 license."* — i.e., the bundled models are ONNX conversions of PaddleOCR's pretrained models (PaddleOCR's own GitHub repo, `PaddlePaddle/PaddleOCR`, also declares `license: apache-2.0` via its GitHub API metadata, fetched the same session), redistributed by RapidOCR under Apache-2.0. No separate, more restrictive model-only license was found at either source.

**Why this matters (and what was deliberately rejected elsewhere in this codebase)**:
`docs/architecture/adr/0022-track-b-semantic-image-grouping.md` rejected Apple's MobileCLIP
specifically because its pretrained weights carry Apple's "ML Research Model" Terms of Use, which
explicitly excludes commercial use — a hard prohibition, not a license-text technicality. The same
diligence applies here: every model weight this project downloads or bundles is confirmed
permissively licensed (Apache-2.0 in all three cases above) directly from its own declared source,
not assumed "probably fine" because the wrapping Python package is permissively licensed.

## Full license texts

This document is a summary attribution notice, not a substitute for each package's own license
file. Full license text for any package above is included in that package's own PyPI/GitHub
distribution (typically a `LICENSE`/`LICENSE.txt`/`COPYING` file in its source tree) and is not
reproduced verbatim here to keep this document a readable index rather than a multi-hundred-page
concatenation. Reclaim's own license terms are in `LICENSE` at the repository root (also served
at `/LICENSE` in the dashboard).

---

*This document reflects the dependency set at the version of Reclaim it ships with. It is kept in
the repository, versioned alongside the code, so any change to a bundled/downloaded dependency's
license is visible in version-control history the same way PRIVACY.md's claims are.*
