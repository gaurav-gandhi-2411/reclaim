# Merge review: `feat/ai-ranker` → `main`

**Status: NOT MERGED.** This document is the review artifact — merge is GG's explicit call
after reading it. Branch has no remote (local-only, never pushed); `main` currently contains
the full `feat/ai-layer` applied-AI base (1a Track A, 1b, 2, feedback-logging) but not the
generic clutter-likelihood ranker. 3 commits, 16 files changed, +4,093/−2 lines.

## What this branch adds

The generic clutter-likelihood ranker per ADR-0021: a LightGBM LambdaMART model that
re-orders the review queue by predicted "is this the KIND of thing usually safe-to-suggest"
— explicitly **not** a personal-preference model (that's ADR-0020's still-deferred, still-
gated feedback-logging ranker, which requires ≥500 real decisions and is untouched by this
branch). Labels come from three independent local LLMs (qwen3:8b, llama3.1:8b, gemma2:9b via
Ollama, zero paid API) rating synthetic file-record fixtures on a fixed 0–4 rubric; only
unanimous-agreement records are used for training.

`build_ranked_clutter_entries` feeds `AITrack.RANKED_CLUTTER` — a track already reserved as
ranking-only since ADR-0011, unchanged by this branch.

## 1. §7.5 safety gate — re-verified against the branch merged onto current `main`, not in isolation

Tested via a disposable local branch (`merge-preview-ranker`, `git merge --no-ff feat/ai-ranker`
onto `main` at `e3d22d2`) — never touched real `main`. Clean merge, zero conflicts (this
branch only adds new files plus small additive edits to `pyproject.toml`/`uv.lock`/
`tests/test_ai_optional_extra.py`, none of which `main` had changed since divergence).

`evals/test_ai_safety_gate.py` — **20/20 passed** at the merged commit, including both new
regression tests:
- `test_ranked_clutter_track_cannot_carry_a_deletion_suggestion` — **PASS**. Constructing an
  `AICluster` on `RANKED_CLUTTER` with a member carrying `is_recommended_keep=True` raises
  `ValueError` (`AICluster.__post_init__`'s pre-existing structural guard, not a new check
  written for this feature).
- `test_ranked_clutter_track_never_suggests_deletion_even_with_a_high_score` — **PASS**. The
  specific property task 1 asked to re-confirm post-integration: `raw_score=4.0` (the maximum
  plausible clutter-likelihood score) on a `RANKED_CLUTTER` cluster still leaves
  `suggests_deletion is False`. Holds identically merged-onto-main as it did in isolation —
  `RANKED_CLUTTER` was never in `_DELETION_SUGGESTION_ELIGIBLE_TRACKS` and this branch never
  touches that frozenset or `src/reclaim/ai/models.py` at all.

## 2. Both install profiles

**Core-only** (fresh isolated venv, `uv sync --frozen --no-dev`, zero `[ai]` extras, tested
against the merged-onto-main commit):
- `lightgbm`/`numpy` genuinely absent (`ModuleNotFoundError` confirmed directly).
- `reclaim.cli` and `reclaim.ai.clutter_ranker` both import cleanly (no eager heavy import).
- `ClutterRanker()` raises the actionable `AIExtraNotInstalledError` — *"generic
  clutter-likelihood ranking needs the optional 'lightgbm' package... Install the AI extras:
  `uv sync --extra ai`"* — never a raw traceback.

**`[ai]`-extra profile** (fresh isolated venv, `uv sync --frozen --extra ai`):
`lightgbm==4.7.0`/`numpy==2.5.1` installed; `tests/test_ai_optional_extra.py` +
`tests/test_ai_clutter_ranker.py` — **23/23 passed**.

**pkgutil backstop confirmed closed, not just flagged.** `test_every_require_call_site_
module_is_covered_by_the_block_list` passed in this run — `lightgbm` is present in
`_ALL_GATED_MODULE_NAMES`. This backstop caught the real gap during the original build
(`clutter_ranker.py`'s `require("lightgbm", ...)` call wasn't yet in the block list); this
merge-review pass re-confirms the fix holds, independently, against the merged-onto-main
state rather than trusting the original fix commit's own claim.

## 3. Harness invariants — confirmed running and passing at branch head

The four pre-existing invariants (hard-tier rejection, per-tier no-pooling, version-chain
conditional-keeper, near-empty-OCR→UNKNOWN) live in `tests/test_ai_eval_harness.py`,
`tests/test_ai_version_chain.py`, `tests/test_ai_content_tagger.py` — untouched by this
branch, part of the standard `pytest tests/ -q` sweep, confirmed passing below.

Ranker-specific: `evals/test_ai_ranker_gold.py::test_cross_llm_agreement_and_ranker_
operating_point` — **PASS**, including the explicit assertion that
`assert_safe_to_promote_to_measured(_DISTRIBUTION)` **raises** `UnsafeMeasuredPromotionError`
(`_DISTRIBUTION.is_synthetic_only=True` — both records and labels are synthetic/LLM-generated).
Re-run against the merged-onto-main state, not just the isolated branch.

## 4. Order-of-merge interaction with `feat/ai-track-b`

Both branches touch four shared files: `pyproject.toml`, `uv.lock`,
`tests/test_ai_optional_extra.py`, `evals/test_ai_safety_gate.py`. Neither touches
`src/reclaim/ai/models.py` (the shared `AITrack` enum) or `AIReviewQueue`. Tested by merging
both onto one disposable branch (`merge-preview-combined`): ranker first, Track B second.
Ranker merges onto `main` with zero conflicts; Track B's merge on top produces 4 textual
conflicts, all four resolvable as pure mechanical unions (both branches independently append
new set members / new test functions / new `[project.optional-dependencies]` entries at the
same anchor line — no logic overlap, no judgment call). `uv.lock` regenerated via `uv lock`
after `pyproject.toml`'s conflict was resolved (hand-editing a lockfile is never done; it's
fully regenerated). Full detail and recommended merge order in
`docs/MERGE_REVIEW_feat-ai-track-b.md` §4 (same finding, documented once).

## Residual risks, disclosed

- **Provisional, permanently.** `_DISTRIBUTION.is_synthetic_only=True` is structurally
  enforced (the promotion-to-measured call raises), not just narrated — but this means the
  ranker's operating point can never be cited as "MEASURED" in the ADR-0016 sense. Both the
  file records and the cross-LLM labels are synthetic; no real personal data anywhere.
- **Small-n.** 79/120 records survived unanimous-agreement filtering (34.2% excluded);
  trained on 62 records (6 batches), evaluated on 17 (2 batches). NDCG@5=0.9763 and
  precision@3=1.0000 clear their a priori floors (0.70/0.50) with margin, but on a small
  held-out set — not a claim of statistical robustness at scale.
  Fleiss' κ=0.6768 ("substantial agreement," Landis & Koch) is a real, independently
  re-derivable number (re-computed from raw JSONL by a prior verifier pass, exact match) but
  reflects three LLMs agreeing with each other, not ground truth from real human
  file-deletion decisions.
- **LLM-consensus labels, not human labels.** The rubric and exclusion discipline are sound,
  but "three LLMs independently agree" is evidence the property is assessable — it is not the
  same evidentiary weight as a human-labeled gold set.
- **Environmental constraint disclosed, not hidden**: judge inference was forced CPU-only
  (`num_gpu=0`) due to unrelated GPU contention on the measurement machine — slower, not less
  correct, but a real fact about how this particular measurement was produced.
- **No real-disk validation** — same posture as `feat/ai-layer`'s own residual-risk section;
  GG's own gold-set labeling tool remains unrun against his personal data for this feature.

## Verification commands (all run against the merged-onto-main state)

```
uv run ruff check .                                          # PASS
uv run mypy                                                   # PASS (48 source files, strict)
uv run pytest tests/ -q                                       # 546 passed, 2 skipped
uv run pytest evals/test_ai_safety_gate.py -v                 # 20 passed
uv run pytest evals/test_ai_ranker_gold.py -v -s               # 1 passed (provisional-lock raise-proof included)
uv sync --frozen --extra ai   (isolated venv)                 # clean; lightgbm/numpy present
uv sync --frozen --no-dev     (isolated venv)                 # clean; lightgbm/numpy absent, degrades cleanly
```

---

**Merge is not performed by this pass.** Everything above is evidence for GG's own review —
the decision to merge `feat/ai-ranker` into `main` is his explicit call.
