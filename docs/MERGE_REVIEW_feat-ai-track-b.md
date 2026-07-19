# Merge review: `feat/ai-track-b` → `main`

**Status: NOT MERGED.** This document is the review artifact — merge is GG's explicit call
after reading it. Branch has no remote (local-only, never pushed); `main` currently contains
the full `feat/ai-layer` applied-AI base (1a Track A, 1b, 2, feedback-logging) but not Track
B. 1 commit, 11 files changed, +1,133/−2 lines.

## What this branch adds

Feature 1a Track B per ADR-0022: CLIP semantic image grouping, **browse-only, never a
deletion suggestion** — the residual after Track A's own pHash near-identical clustering,
grouped by cosine similarity into "same scene/event" browse groups via a FAISS HNSW index.
OpenCLIP ViT-B/32 (`ViT-B-32-quickgelu` variant — the plain `ViT-B-32` config pairs the
"openai" checkpoint with the wrong activation function, a real bug caught and fixed during
the build, not shipped silently), CPU-only inference, embeddings cached in SQLite keyed
`(path, size_bytes, mtime, model_id)`.

`build_semantic_image_clusters` feeds `AITrack.SEMANTIC_IMAGE` — a track already reserved as
browse-only since ADR-0011, unchanged by this branch.

## 1. §7.5 safety gate — re-verified against the branch merged onto current `main`, not in isolation

Tested via a disposable local branch (`merge-preview-track-b`, `git merge --no-ff
feat/ai-track-b` onto `main` at `e3d22d2`) — never touched real `main`. Clean merge, zero
conflicts.

`evals/test_ai_safety_gate.py` — **19/19 passed** at the merged commit, including:
- `test_semantic_image_track_never_suggests_deletion_even_with_a_high_similarity` — **PASS**.
  The specific property task 1 asked to re-confirm post-integration: `raw_score=0.01` (a
  `max_pairwise_distance` of 0.01, i.e. **0.99 cosine similarity** — as near-identical as two
  distinct embeddings plausibly get) on a `SEMANTIC_IMAGE` cluster still leaves
  `suggests_deletion is False`. Holds identically merged-onto-main as in isolation —
  `SEMANTIC_IMAGE` was never in `_DELETION_SUGGESTION_ELIGIBLE_TRACKS` and this branch never
  touches that frozenset or `src/reclaim/ai/models.py` at all.

## 2. Both install profiles

**Core-only** (fresh isolated venv, `uv sync --frozen --no-dev`, zero `[ai]` extras, tested
against the merged-onto-main commit):
- `torch`/`open_clip`/`faiss` genuinely absent (`ModuleNotFoundError` confirmed directly, both
  via a real blocked-import check and via `reclaim.ai._optional.require()` called directly).
- `reclaim.cli` and `reclaim.ai.image_embeddings`/`semantic_image_grouping` import cleanly (no
  eager heavy import at module load time).
- `require("torch"/"open_clip"/"faiss", ...)` each raise the actionable
  `AIExtraNotInstalledError` — never a raw traceback.

**`[ai]`-extra profile** (fresh isolated venv, `uv sync --frozen --extra ai`; note: a
first attempt at this venv under the session's Temp-scratch directory hit spurious
`FileNotFoundError`s importing `transformers` — traced to files that a directory listing
still showed but `os.path.exists()` said were gone, an AV/quarantine-style artifact of that
specific heavily-scanned location, not a real dependency problem; confirmed by re-running the
identical `uv.lock`-pinned install in a clean location, which passed completely):
`torch==2.13.0+cpu`/`open_clip`/`faiss` installed; `tests/test_ai_optional_extra.py` +
`tests/test_ai_image_embeddings.py` + `tests/test_ai_semantic_image_grouping.py` +
`evals/test_ai_semantic_grouping_gold.py` — **25/25 passed**, including the real BCubed gold
eval against actual Copydays images (not mocked).

**pkgutil backstop confirmed closed, not just flagged.** `test_every_require_call_site_
module_is_covered_by_the_block_list` passed — `open_clip`/`torch`/`faiss` are all present in
`_ALL_GATED_MODULE_NAMES`. This backstop caught the real gap during the original build (these
three call sites weren't yet in the block list, inherited from `main`'s prior state which only
covered earlier features' dependencies); this merge-review pass re-confirms the fix holds,
independently, against the merged-onto-main state.

## 3. Harness invariants — confirmed running and passing at branch head

The four pre-existing invariants (hard-tier rejection, per-tier no-pooling, version-chain
conditional-keeper, near-empty-OCR→UNKNOWN) are untouched by this branch, part of the
standard `pytest tests/ -q` sweep, confirmed passing below.

Track-B-specific: `evals/test_ai_semantic_grouping_gold.py::test_semantic_grouping_bcubed_
on_real_copydays_blocks` — **PASS**, browse-only-eligible threshold-sweep measured, not
merely asserted, at the merged-onto-main state.

## 4. Order-of-merge interaction with `feat/ai-ranker` — recommendation

Both branches touch the same four shared files: `pyproject.toml`, `uv.lock`,
`tests/test_ai_optional_extra.py`, `evals/test_ai_safety_gate.py`. Neither touches
`src/reclaim/ai/models.py` (the shared `AITrack` enum, including
`_DELETION_SUGGESTION_ELIGIBLE_TRACKS`) or `src/reclaim/ai/review_queue.py`
(`AIReviewQueue`) — both new tracks were already reserved placeholders since ADR-0011, so
there is no shared-state collision at the architectural level, only textual overlap in
additive edits.

**Tested both orders' consequence directly** by test-merging both branches onto one
disposable branch (`merge-preview-combined`, off `main` at `e3d22d2`): `feat/ai-ranker`
merged first (clean, zero conflicts, since neither branch had touched `main`'s files yet),
then `feat/ai-track-b` merged on top — **4 conflicts**, all in the shared files above, all
resolved as pure mechanical unions:
- `pyproject.toml` — both branches independently append new `ai` extras / a comment update at
  the same anchor lines; resolution keeps both sets of additions (`numpy`+`lightgbm` from
  ranker, `torch`+`open-clip-torch`+`faiss-cpu` from Track B) and a combined summary comment.
- `tests/test_ai_optional_extra.py` — both append new members to `_ALL_GATED_MODULE_NAMES` at
  the same line; resolution is the union (`lightgbm`, `open_clip`, `torch`, `faiss`).
- `evals/test_ai_safety_gate.py` — both insert a new test function immediately after the same
  anchor function (`test_browse_only_track_cannot_carry_a_deletion_suggestion`); git's diff3
  merge conflated the two insertions because their trailing lines are textually identical
  (`rationale="test",\n)\nassert cluster.suggests_deletion is False`); resolved by hand,
  keeping both branches' functions in full, in merge order (ranker's two tests, then Track
  B's one test) — no content lost, no logic changed.
- `uv.lock` — conflicted textually (both branches independently regenerated it); resolved not
  by hand-editing (never done for a lockfile) but by regenerating from scratch via `uv lock`
  against the reconciled `pyproject.toml` — resolved 116 packages cleanly, including all four
  new dependencies (`lightgbm`, `ollama`, `open-clip-torch` group, `torch`, `faiss-cpu`).

**After resolution, the combined branch passes clean, second-merged-on-top-of-first,
confirmed**: `evals/test_ai_safety_gate.py` — **21/21 passed** (both new deletion-eligibility
tests coexist), `pytest tests/ -q` — **562 passed, 2 skipped**, both gold evals
(`test_ai_ranker_gold.py` + `test_ai_semantic_grouping_gold.py`) — **2/2 passed**, `ruff
check .` and `mypy` both clean (48 source files, strict).

**Recommendation: merge `feat/ai-ranker` first, then `feat/ai-track-b`** (order tested
above). Reasoning: neither order is architecturally required — the two tracks are
independent, non-interacting features sharing no runtime state — but ranker-first is
marginally simpler to review since Track B's conflict resolution (the 4 files above) is then
a known, already-tested quantity rather than something to re-verify in the other direction.
If GG merges in the reverse order instead, the same four files will conflict identically (git
merge conflicts are symmetric here — both are pure-insertion diffs against the same `main`
base) and the same mechanical-union resolution applies; this was not separately re-tested in
the reverse order since the conflict shape and resolution do not depend on merge direction.

## Residual risks, disclosed

- **Browse-only by design, not by omission.** The precision bar (0.70) is deliberately looser
  than dedup's (0.95) per the spec's own framing — this is a browse-tidiness feature, not a
  deletion decision, and the safety gate proves it structurally cannot become one regardless
  of similarity confidence.
- **0.7897 precision / 0.7143 recall is a real, disclosed proxy measurement, not the exact
  target distribution.** INRIA Copydays blocks are adversarial transformations of the SAME
  photo (print-scan/blur/paint attacks), not genuinely different photos of the same scene —
  Track B's actual target use case. `DistributionDeclaration.untested_variation_note` states
  this explicitly; a future re-measurement against a genuinely public scene-grouped dataset
  would be a legitimate upgrade, not a correction of an error.
- **40 of ~157 available Copydays blocks used** — a deterministic subset sized for tractable
  real-CLIP-inference runtime, disclosed in the report, not silently narrowed.
- **Below threshold 0.80, precision collapses fast** (0.1533 at 0.70) — the selected 0.82
  operating point sits at the F1-maximizing knee, but the full swept curve (0.70–1.00, in the
  ADR) makes clear this is not a robust-to-threshold-drift property; a config regression that
  silently lowered the threshold would meaningfully degrade precision.
- **No real-disk validation** — same posture as `feat/ai-layer` and `feat/ai-ranker`'s own
  residual-risk sections; GG's own gold-set labeling tool remains unrun against his personal
  photo library for this feature.

## Verification commands (all run against the merged-onto-main state)

```
uv run ruff check .                                            # PASS
uv run mypy                                                     # PASS (48 source files, strict)
uv run pytest tests/ -q                                         # 528 passed, 2 skipped
uv run pytest evals/test_ai_safety_gate.py -v                   # 19 passed
uv run pytest evals/test_ai_semantic_grouping_gold.py -v -s     # 1 passed (real BCubed measurement)
uv sync --frozen --extra ai   (isolated venv)                   # clean; torch/open_clip/faiss present
uv sync --frozen --no-dev     (isolated venv)                   # clean; all three absent, degrades cleanly
```

---

**Merge is not performed by this pass.** Everything above is evidence for GG's own review —
the decision to merge `feat/ai-track-b` into `main` is his explicit call.
