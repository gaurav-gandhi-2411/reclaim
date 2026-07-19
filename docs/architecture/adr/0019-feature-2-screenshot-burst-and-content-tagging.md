# 0019. Feature 2: screenshot burst detection + OCR content-tag classifier

## Context

GG's build instruction: "Burst detection: dimensions==screen-resolution + capture-time
proximity + pHash — mostly deterministic. OCR via local rapidocr/tesseract → content tag
(receipt/document/code/chat/transient-UI). Operating point + tag classifier measured on a
REALISTIC distribution with per-tier gating (precision AND recall floors); source a public
human-labeled screenshot/document-type set if one exists, else transform-generated with
disclosed scope. PRIVACY LOCK (tested, non-negotiable): OCR text never logged, never
persisted beyond an on-device tag, never surfaced outside local review; bias STRONGLY toward
keep for receipt/document/code tags. Recommend-only." Same discipline as Feature 1a/1b:
ADR-0016's `DistributionDeclaration` + precision-AND-recall floors on every operating point,
ADR-0018's never-pool-across-tiers invariant for any multi-class/multi-tier measurement.

Two sub-problems:
1. Burst detection — grouping screenshots taken in rapid succession of the same content.
2. Content-tag classification — labeling each screenshot's OCR'd text as
   receipt/document/code/chat/transient-UI (or UNKNOWN), gating deletion-eligibility.

## OCR library choice

| Candidate | Fit | License / install | Verdict |
|---|---|---|---|
| **pytesseract + Tesseract** | Mature, widely used | Apache-2.0 (bindings), but Tesseract itself is a **system binary** — not pip-installable, breaks the zero-friction-install posture every other AI dependency this project has added maintains (`imagehash`, `opencv-python-headless`, `datasketch`, `sentence-transformers`, `python-docx`, `pypdf` are all pure-pip) | **Rejected** — would be the first AI-layer dependency requiring an out-of-band system install. |
| **rapidocr-onnxruntime** | Purpose-built local OCR, bundled ONNX models | Apache-2.0, pip-only, zero network calls at inference time | **Selected.** |

`rapidocr-onnxruntime>=1.3` added to `[project.optional-dependencies] ai`. **Disclosed
tradeoff**: it pulls in full `opencv-python` (GUI-capable) as a transitive dependency,
coexisting with the already-pinned `opencv-python-headless` — both provide the `cv2` module,
and whichever installs last determines the on-disk build. Verified empirically this doesn't
break existing `cv2` usage: `uv sync --extra ai` succeeds, `cv2.__version__` reports `5.0.0`,
and `tests/test_ai_keep_best.py` / `evals/test_ai_image_similarity.py` (both headless-only
`cv2` call sites — Laplacian variance, grayscale imread) pass unchanged.

## Dataset evaluation for the content-tag classifier (SROIE, RVL-CDIP assessed and rejected)

| Candidate | Task match | License | Verdict |
|---|---|---|---|
| **SROIE (ICDAR-2019 receipt OCR)** | Good — real receipt images with labeled fields | Canonical source (RRC portal) is **gated, registration-only**. A GitHub mirror (`zzzDavid/ICDAR-2019-SROIE`) carries an MIT LICENSE file, but that almost certainly covers the mirror's own scripts, not the underlying receipt images (real photographs of real store receipts) — the same real-vs-mirror-license ambiguity ADR-0015 rejected California-ND/AVA for. | **Rejected on license ambiguity.** |
| **RVL-CDIP (document image classification)** | Good — 16-class real scanned-document images | Hugging Face card (`aharley/rvl_cdip`) reports `"license": ["other"]`, not a clean SPDX tag, despite some secondary sources calling it "public domain." Provenance traces to the Legacy Tobacco Document Library / IIT-CDIP — real tobacco-litigation business records, a real (if debated) ambiguity. | **Rejected on license ambiguity.** |
| **Project Gutenberg (already downloaded, ADR-0017) + Reclaim's own source code + disclosed synthetic** | No single dataset matches this exact "screenshot content type" taxonomy — receipt/document/code/chat/transient-UI is a screenshot-specific split no public corpus labels, and it doesn't cleanly map onto either candidate above (SROIE is receipts-only, RVL-CDIP is scanned-document-only, neither covers chat/transient-UI at all) | Public domain (Gutenberg) / Reclaim's own code (zero licensing risk) / synthetic, fully disclosed | **Selected**, per class — see below. |

**Decision: 2 of 5 classes reuse real, already-vetted content; 3 have no plausible public
source regardless of licensing, so are synthetic-but-structurally-realistic, fully disclosed.**

| tag | source | real or synthetic |
|---|---|---|
| `document` | Project Gutenberg prose (already downloaded for 1b), truncated to a 70-word screenshot-realistic snippet | real |
| `code` | Contiguous line windows sampled from Reclaim's own `src/reclaim/**/*.py` | real |
| `receipt` | Synthetic POS-receipt generator (store/items/qty/subtotal/tax/total/cash/change), adapted from 1b's `_invoice_text` pattern | synthetic-but-structurally-realistic |
| `chat` | Synthetic timestamped back-and-forth message generator | synthetic-but-structurally-realistic |
| `transient_ui` | Synthetic short UI-state strings (Loading/Retry/Connecting/etc.) | synthetic-but-structurally-realistic |
| `unknown` (stress pool, not a scored class) | Bare ambiguous OCR-noise-like strings (`"OK"`, `"..."`, `"9:41"`, gibberish) | synthetic, included as a negative for every real tag |

`evals/ai_fixtures/build_content_tag_fixtures.py` — 261 total samples (document 120, code 39,
receipt 32, chat 32, transient_ui 30, unknown 8).

## Burst detection: reused threshold, disclosed policy, no new measurement needed

Three classical rules, ALL must agree (AND, not majority vote) to union two screenshots into
one burst: same dimensions, capture-time within a window, pHash Hamming distance within a
bound. This is deliberately **not** re-measured as a new operating point:

- **pHash Hamming distance ≤ 14** — reused as-is from ADR-0012/ADR-0015's real Copydays
  measurement. "Screenshots taken moments apart" is the same near-identical-image-detection
  problem Feature 1a already solved; re-deriving it here would be redundant, not more rigorous.
- **Capture-time gap ≤ 60 seconds** — an explicitly disclosed **policy choice, not
  data-derived**: consecutive screenshots taken while iterating on the same task are very
  rarely more than a minute apart; the margin also tolerates filesystem mtime jitter.
- **Same dimensions** — exact equality, not a measured threshold at all.

Each rule (and their conjunction) is covered by dedicated unit tests
(`tests/test_ai_screenshot_burst.py`): dimension mismatch rejected, time-gap-beyond-window
rejected, visually-different-content rejected, custom threshold override respected, singleton
clusters dropped. No additional "operating point" eval was built for burst detection — it is
a boolean AND-gate composed entirely of already-measured or already-disclosed-as-policy
signals, not a classifier requiring its own precision/recall curve.

## Content-tag classifier: MEASURED (provisional), per-tier gated

Reproduce:
```
uv run python evals/ai_fixtures/fetch_gutenberg_texts.py
uv run pytest evals/test_ai_content_tag_gold.py -v -s
```

**The safety-critical number**: TRANSIENT_UI is the *only* content tag that is ever
deletion-eligible (GG's explicit "bias STRONGLY toward keep for receipt/document/code tags"
instruction, implemented as `content_tagger.KEEP_BIASED_TAGS` = every tag except
`TRANSIENT_UI`, including `CHAT` and `UNKNOWN`). A misclassification among the other four
tags is always safe — still keep-biased either way — so only TRANSIENT_UI's precision is
gated at a strict floor; the other four are quality signals, gated at a realistic, honestly
achievable floor.

Measured on the shipped classifier (`content_tagger.tag_content`, fixed confidence threshold
`1.5`, no sweep — this is the actual production behavior, not a hypothetical operating
point):

| tag | precision | recall | floor |
|---|---:|---:|---|
| `transient_ui` | **1.0000** | 1.0000 | precision ≥ 0.95 (safety-critical) |
| `receipt` | 1.0000 | 1.0000 | precision ≥ 0.65, recall ≥ 0.5 (quality) |
| `code` | 0.8261 | 0.9744 | precision ≥ 0.65, recall ≥ 0.5 (quality) |
| `chat` | 0.6667 | 1.0000 | precision ≥ 0.65, recall ≥ 0.5 (quality) |
| `document` | 1.0000 | 0.6583 | precision ≥ 0.65, recall ≥ 0.5 (quality) |

**Zero real-content samples (receipt/document/code/chat) were ever classified as
TRANSIENT_UI** — the strongest possible statement of the safety property, stronger than
"precision is high" alone, asserted explicitly in the eval as its own check.

**A real false-positive risk was caught and fixed during construction, not after**: the
initial `_score_transient_ui` scoring gave short/sparse text a `sparse_bonus` large enough to
clear the confidence floor on its own — meaning 3-word OCR gibberish with zero transient-UI
vocabulary (e.g. `"xk qz 42"`) was confidently tagged `transient_ui`, the one deletion-eligible
tag, purely because it was short. Fixed by capping `sparse_bonus` below the confidence floor
(`1.0` max, floor is `1.5`) so sparseness alone can never produce a confident classification —
it only amplifies an already-present keyword match. Regression-tested at both the unit level
(`tests/test_ai_content_tagger.py::test_tag_content_sparse_gibberish_is_unknown_not_transient_ui`)
and the fixture level (the `unknown` stress pool, all scoring exactly `1.0`, below the `1.5`
floor).

**Disclosed, honest limitation (not blocking)**: `chat` precision (0.667) and `document`
recall (0.658) are the two weakest quality numbers — real Gutenberg dialogue-heavy passages
often trip `chat`'s short-line heuristic, and some document snippets don't clear `document`'s
own prose-length bonus. Both are safe (misclassification stays within the keep-biased set)
and both clear the modest 0.65/0.5 quality floor; a future v2 (a learned classifier, per
spec's own framing that this classical scorer is v1, "add-only-if-measured-to-help, never a
default") could plausibly improve this without changing the safety property at all.

**Per-tier gating (ADR-0018), never pooled**: `eval_harness.select_operating_point_per_tier`
is used twice — once across the 4 quality tags (proving a threshold exists at a realistic
uniform floor), once for TRANSIENT_UI alone (proving a threshold exists at the strict safety
floor; a single-tier call is still legitimately per-tier, since ADR-0018 forbids *pooling*
tiers, not measuring one in isolation). The actual gate that ships, though, is a direct
recomputation from `tag_content`'s real output — not a swept threshold — since content-tagger
already has ONE fixed, shipped confidence constant, not a value this eval selects.

**Distribution honesty**: `DistributionDeclaration(is_synthetic_only=True)` — 3 of 5 tags (+
the unknown stress pool) are synthetic, so this measurement stays **PROVISIONAL**, never
promoted to MEASURED-in-the-ADR-0016 sense (`assert_safe_to_promote_to_measured` is
deliberately not called). `untested_variation_note` discloses: photographed receipts at an
angle, low-confidence partial OCR, non-Python code / dark-mode IDE themes, chat apps with
different UI chrome, non-English text — all uncovered.

## Follow-up: the OCR-degradation blind spot (closing F2's one real over-deletion path)

**Why this exists.** The sparse-gibberish fix above (`sparse_bonus` capped below the
confidence floor) was proven against *synthetic* short strings with no real-world source. GG
flagged the gap directly: a genuinely meaningful screenshot (a receipt, a document, code, a
chat) that OCRs *poorly* for an ordinary real-world reason — a dark/low-contrast capture, an
unusual font, a partial/cut-off screenshot, heavy blur — produces the same shape of sparse,
fragmented text as gibberish does. Nothing in the original measurement proved that path was
closed too. Since `TRANSIENT_UI` is the only deletion-eligible tag, a real, wanted screenshot
degrading into a confident `TRANSIENT_UI` classification would be exactly the over-deletion
failure "bias STRONGLY toward keep" exists to prevent — this is F2's one real risk of
recommending deletion of something the user actually wants kept.

**Built a real-image degradation tier**
(`evals/test_ai_screenshot_ocr_degradation_gate.py`): 4 base content-bearing screenshots
(receipt, document, code, chat — receipt/code/chat reuse the same real/synthetic sources as
the main content-tag fixtures; document is a self-contained prose paragraph, deliberately not
sourced from the Gutenberg corpus so this safety eval runs unconditionally, no fetch
precondition) rendered as real PNG images, each degraded 4 ways through PIL: heavy Gaussian
blur (out-of-focus/motion capture), severe contrast reduction (dark/washed-out capture),
cropping to a thin top sliver (a partial/cut-off screenshot), and all three combined
(the realistic worst case). All 16 (content × degradation) combinations run through the real
`rapidocr` engine and the real, shipped `tag_content` classifier — not a simulation.

**Result: 0 of 16 degraded real-content images were ever classified `TRANSIENT_UI`.** 12 of
16 degraded all the way to near-empty OCR output (0 characters extracted) — every one of
those resolved to `UNKNOWN`, exactly the required "OCR found little → I can't tell (browse),
never transient (deletable)" behavior. The remaining 4 (the `cropped`-only degradation, one
per content kind — cropping alone left enough legible text for OCR to still extract a
recognizable fragment) resolved to `receipt`/`code`/`chat`/`chat` respectively — the
`document` sample's cropped fragment was misread as `chat` (a keep-biased mislabel, safe, not
a safety violation, consistent with the already-disclosed document/chat confusion above).

**A direct, image-independent proof of the specific invariant** was added alongside the image
tier: `tag_content` on `None`, `""`, whitespace, and a handful of 1-2 character stray-OCR-like
strings (`"."`, `"1"`, `"x"`, `".."`, `"||"`) all resolve to `UNKNOWN` — locking in "near-empty
OCR text is UNKNOWN, never TRANSIENT_UI" as a permanent regression check independent of
whatever any specific degraded image happens to produce on a given run.

Report: `reports/ai/screenshot_ocr_degradation_tier.json`. Reproduce:
```
uv run pytest evals/test_ai_screenshot_ocr_degradation_gate.py -v -s
```

This closes F2's one real over-deletion path. No code change to `content_tagger.py` was
needed — the `sparse_bonus` cap fix already made during the original build (see above)
generalizes correctly from synthetic gibberish to real degraded OCR output, because both
produce the same underlying signal (few or no keyword hits, low word count) that fix already
guards against. This follow-up's contribution is the *proof*, not a new fix.

## PRIVACY LOCK — structural + runtime tested

GG's instruction: OCR text never logged, never persisted beyond an on-device tag, never
surfaced outside local review. Two layers, both tested, mirroring `document_text.py`'s
zero-logging precedent from Feature 1b:

1. **Structural**: `screenshot_ocr.py`, `content_tagger.py`, and `screenshot_review.py`
   contain zero logging/print calls anywhere — not even at debug level, not even a
   filename-only line. Proven by an AST scan
   (`tests/test_ai_screenshot_ocr.py::test_no_module_touching_ocr_text_contains_a_logging_or_print_call`),
   not just narrative claim.
2. **Runtime**: a real OCR extraction on a synthetic image containing a unique canary string,
   with every Python logger forced to `DEBUG`/`NOTSET` so nothing is filtered before
   `caplog` sees it, asserting the canary appears in **zero** captured log records — proving
   reclaim's own code never hands the text to any logger, not merely that its own modules
   don't call logging directly (`test_ocr_text_never_appears_in_any_log_record_at_any_level`).
   A second, end-to-end version
   (`tests/test_ai_screenshot_review.py::test_ocr_secret_text_never_appears_anywhere_in_the_returned_clusters`)
   proves the canary never appears in the `AICluster`/`AIClusterMember` objects
   `screenshot_review.build_screenshot_burst_clusters` actually returns to a caller, via
   `repr()` over the whole result as a structural catch-all.

`screenshot_ocr.extract_screenshot_text`'s docstring states the contract explicitly: its
return value must never be logged, printed, or persisted beyond deriving a `ContentTag` —
the function's own guarantee ends at "return the text as a Python string in memory."

## Safety/architecture

`AITrack.SCREENSHOT_BURST` joined `_DELETION_SUGGESTION_ELIGIBLE_TRACKS` — but
**conditionally**, at the orchestration level, not unconditionally like the other three
tracks. `screenshot_review.build_screenshot_burst_clusters` only ever sets
`is_recommended_keep` on a member when **every** member of the burst's OCR content tag is
`TRANSIENT_UI`; a single member tagged receipt/document/code/chat/unknown downgrades the
*whole* cluster to browse-only (no keeper, `AICluster.suggests_deletion` is `False`) — the
same conditional-keeper posture Feature 1b's `version_chain.version_signals_agree` already
established for version-chain's own safety property. Two new regression tests
(`test_screenshot_burst_track_with_a_keeper_does_suggest_deletion`,
`test_screenshot_burst_track_without_a_keeper_is_browse_only_not_a_suggestion`) prove the
track itself gates on an identified keeper exactly like the other three deletion-eligible
tracks; `test_review_queue_partitions_all_five_tracks_correctly` proves `AIReviewQueue`
partitions all five correctly together. Orchestration-level tests
(`tests/test_ai_screenshot_review.py`) prove the actual conditional gate: an all-transient-UI
burst gets a keeper, a burst with one receipt-tagged member does not.

`evals/test_ai_safety_gate.py`'s AST scan (`test_ai_package_never_imports_the_executor_or_
send2trash`) automatically covers every new module under `src/reclaim/ai/` via `rglob` — no
manual update needed for `screenshot_burst.py`/`screenshot_ocr.py`/`content_tagger.py`/
`screenshot_review.py` to be included in that guarantee.

## Zero-cost/local + license summary of new dependencies

Added to `[project.optional-dependencies] ai`, lazy-imported via
`reclaim.ai._optional.require`, never pulled in by a bare `pip install reclaim`:

- `rapidocr-onnxruntime>=1.3` — Apache-2.0 — local OCR, bundled ONNX models, zero network
  calls at inference time. Pulls in full `opencv-python` transitively (see coexistence note
  above).

No paid API, no `ANTHROPIC_API_KEY`, no network access at runtime once the bundled ONNX
models are present (they ship with the package).

## Consequences

- Feature 2 is fully recommend-only, matching every prior AI track: `screenshot_review.py`
  imports neither `reclaim.executor` nor `send2trash` (verified by the shared safety-gate AST
  scan), and `AIClusterMember` still shares no fields with `reclaim.models.Candidate`.
- The content-tag classifier's real weaknesses (chat precision, document recall) are honestly
  disclosed, not hidden — they represent a genuine, measured limitation of a classical v1
  keyword scorer, not a fabricated pass. A future learned classifier is the natural v2 upgrade
  path, per spec's own explicit framing.
- `score_all_tags` was extracted as a new public function on `content_tagger.py` (the raw
  per-class scores before the confidence-floor cutoff `tag_content` applies) — a clean,
  behavior-preserving refactor that let the eval sweep candidate confidence thresholds without
  duplicating the scoring logic. `tag_content`'s own behavior is unchanged (proven by the
  existing unit tests passing unmodified).
- Burst detection's reuse of ADR-0012's pHash threshold means any future re-measurement of
  that threshold (a new Copydays-style dataset, a different hash algorithm) automatically
  carries forward to screenshot bursts too — a deliberate coupling, not an oversight.
- The 60-second capture-time window is a policy choice, not measured; a future telemetry-
  informed value (if real usage data ever became available) would be a legitimate revision,
  not a correction of an error.

## Test coverage

**Synthetic (CI, every run):** `tests/test_ai_screenshot_burst.py` (9 cases),
`tests/test_ai_screenshot_ocr.py` (5 cases — including the structural privacy-lock AST scan
and the runtime canary-log-capture proof), `tests/test_ai_content_tagger.py` (8 cases —
including the sparse-gibberish safety regression), `tests/test_ai_screenshot_review.py` (4
cases — conditional-keeper gate both directions, empty-burst, end-to-end privacy-lock proof),
`evals/test_ai_safety_gate.py` (+4 new cases — `SCREENSHOT_BURST` track gating both
directions, 5-track queue partitioning).

**Real (local, on-demand, not in CI):** `evals/test_ai_content_tag_gold.py` (1 case — the
per-tier-gated content-tag operating point measurement, the TRANSIENT_UI-specific safety
floor, and the zero-dangerous-false-positives proof). Same not-in-default-CI-sweep posture as
`evals/test_ai_document_gold.py`/`evals/test_ai_document_templated_gold.py` (reads Reclaim's
own source tree + requires the Gutenberg corpus fetch).
`evals/test_ai_screenshot_ocr_degradation_gate.py` (1 case — the real-image-degradation
over-deletion-path closure above) runs unconditionally (no corpus-fetch precondition) since
it's proving a safety property, not measuring an operating point, and belongs in the eval
suite alongside the other real-OCR-engine tests.
