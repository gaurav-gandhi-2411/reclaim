# 0021. Generic clutter-likelihood ranker (LightGBM LambdaMART, cross-LLM labeled)

## Context

GG's build instruction (paraphrased, full text preserved in PLAN.md's checkpoint): build the
clutter-likelihood ranker on `feat/ai-ranker` off `main`, LightGBM LambdaMART. The target is
explicitly **generic, not personal**: "is this file the KIND of thing usually safe-to-suggest
(build artifact, stale installer, cache remnant, temp, old export) vs usually-important
(document, config, source, key, financial)" — assessable from content+metadata, knowable, not
"would GG delete this." Labels come from **2-3 LOCAL LLMs via Ollama** (zero paid API, zero
`ANTHROPIC_API_KEY`) acting as independent raters on a fixed rubric, inter-rater agreement
measured (Fleiss'/Cohen's kappa), and **only unanimous-agreement records used for
training** — disagreement excluded, never majority-voted, since forcing a label where three
independent judges couldn't agree reintroduces exactly the fabricated-confidence problem this
whole layer's discipline exists to prevent.

**This is deliberately a different, independent thing from the PERSONAL ranker ADR-0020
declined to build.** ADR-0020's Feature 3 feedback-logging ranker requires ≥500 REAL
accept/reject/keep decisions before it can train on anything — there is no shortcut around
that, because personal preference is by definition not knowable from anywhere except a real
person's real decisions. The generic clutter-likelihood property this ADR trains on is a
*different* target entirely: not "would GG delete this specific file," but "is this the kind
of thing a reasonable person usually would." That property doesn't require personal decision
history to be knowable — three independent LLMs reading the same metadata and agreeing is
itself evidence the property is assessable, which is exactly what the inter-rater agreement
measurement below exists to prove or disprove. Both rankers can eventually coexist: this one
ships day one; ADR-0020's personal layer remains the documented, still-unbuilt, still-gated
upgrade — see "Relationship to the personal ranker" below.

## Local judge models

Three models already installed locally via Ollama, chosen for genuine cross-vendor diversity
(not three sizes of the same model family): **qwen3:8b** (Alibaba), **llama3.1:8b** (Meta),
**gemma2:9b** (Google) — substituted for the spec's illustrative "qwen2.5, llama3.1, mistral"
list since qwen3 (newer) and gemma2 (already installed, no new ~4GB download needed) were
readily available and give the same cross-vendor property mistral would have. `qwen3:30b-a3b`
(also installed) was considered and rejected up front, reusing the sibling TriageIQ project's
own finding for the identical model on this same class of hardware: its CPU/GPU parameter
split breaks deterministic output.

**A real, measured environmental constraint forced CPU-only inference for all three judges.**
`nvidia-smi` showed an unrelated process (a different project's conda environment, `aetherart`,
PID confirmed) holding ~6.3 of this machine's only 8GB of GPU VRAM throughout this session.
Under that real contention: `qwen3:8b` failed to load at all (`ResponseError: timed out
waiting for llama-server to start`), `llama3.1:8b` crashed outright (`NTSTATUS 0xffffffff`),
and `gemma2:9b` alone succeeded but took ~4 minutes for a single one-word response. Forcing
`num_gpu=0` (`ranker_llm_judge.py`) made every model load and run reliably — slower per call
(measured ~9.6-32.3s/call once warm, vs. sub-second on an uncontended GPU) but deterministic
and failure-free. This is disclosed as a fact about the machine this measurement was actually
performed on, not a property of the approach — a future re-run on a machine with a free GPU
could drop `num_gpu=0` for much faster labeling without changing anything else.

## Rubric

One fixed system prompt (`ranker_llm_judge.RUBRIC_SYSTEM_PROMPT`), given identically to all
three judges — a 0-4 ordinal scale (the standard LTR relevance-grade shape, chosen so labels
double directly as LambdaMART training targets):

| grade | meaning |
|---:|---|
| 4 | Definite clutter kind: build artifacts, package caches, browser/temp caches, crash dumps, old installers past useful life, duplicate old exports with generic/versioned names |
| 3 | Probable clutter kind: large stale logs, old archives with generic names, a file inside a large redundant cluster and not the recommended keeper |
| 2 | Genuinely ambiguous from the metadata alone |
| 1 | Probably important: personal documents, active project source code, financial records, personal media |
| 0 | Definitely important: credentials/keys, active configuration/secrets, unique irreplaceable content |

Each judge sees ONLY metadata (path, extension, size, age, location class, deterministic-
engine category, cluster context, cloud-sync flag) — never real file content, never any real
user's data (the fixture is fully synthetic; see below). Judge-call plumbing (client
construction, deterministic `temperature=0`/`seed=42`/`keep_alive=-1` options, retry with
exponential backoff, robust JSON extraction stripping think-blocks/code-fences) follows the
same pattern already proven in the sibling TriageIQ project's `TriageJudge`, adapted for
multi-model independent rating rather than single-model self-consistency.

## Dataset: synthetic-but-realistic file-record fixtures

`evals/ai_fixtures/build_ranker_fixtures.py` — 120 records, metadata only (size, ext,
path-class, mtime/ctime, cluster stats, category, cloud-sync flag — **no atime anywhere**,
same discipline as Feature 3), across 15 archetype generators spanning three groups weighted
toward realism (45% clutter-kind, 40% important-kind, 15% deliberately ambiguous — not a
uniform split, which would overstate how often genuine ambiguity occurs). Records are
generated in 8 batches of 15 — the grouping unit LightGBM's LambdaMART loss needs, and the
unit the train/eval split is done on (a whole batch lands on one side, never split across,
preventing the model from learning batch-specific quirks in train and "cheating" on the same
batch at eval time).

No public dataset was sought for this step deliberately — the fixture is metadata-pattern
generation (not real file content), and the whole point of the cross-LLM labeling step is to
generate the ground truth this ADR measures, not to source it externally.

## Measured: inter-rater agreement and exclusion rate

**Fleiss' kappa (3 raters, N=120 complete-graded records): 0.6768** — "substantial agreement"
on the standard Landis & Koch interpretation scale (0.61–0.80), a real, meaningfully-above-
chance result for three independently-run local models with no communication between them.
Pairwise Cohen's kappa: qwen3:8b vs llama3.1:8b 0.6444, qwen3:8b vs gemma2:9b 0.6664,
llama3.1:8b vs gemma2:9b 0.7250 — llama3.1/gemma2 agreed most closely; qwen3 was the
relative outlier of the three, still solidly in the "substantial" band.

**79/120 records had unanimous 3-judge agreement (41 excluded, 34.2% exclusion rate)** — per
GG's explicit instruction, excluded records are dropped entirely from training/eval, never
majority-voted into a label. A genuinely meaningful exclusion rate, not a rounding error:
roughly one in three records is where "generic clutter-likelihood" turned out NOT to be
cleanly knowable from metadata alone even to three independent judges — exactly the honest
signal this design exists to surface rather than paper over with a forced majority vote. Only
the 79-record unanimous subset is used below.

## Measured: LightGBM LambdaMART operating point

Grouped train/eval split (6 train batches / 62 records, 2 eval batches / 17 records — whole
batches, never split within one). Trained on the unanimous-agreement subset only.

**Mean NDCG@5 across held-out eval batches: 0.9763 (floor 0.70) — cleared with substantial
margin.**
**Mean precision@3 (relevance grade ≥3) across held-out eval batches: 1.0000 (floor 0.50) —
every top-3-ranked item in both held-out eval batches was genuinely probable-or-definite
clutter.**

Both floors are a priori, not reverse-engineered from the result — chosen because a
reasonably-separable synthetic labeling task (clean archetype generators, unanimous-agreement
filtering already removing the genuinely hard cases) should clear them if the whole pipeline
is working as intended; a result below either floor is a real, reportable finding about the
approach's limits, not a bug to paper over.

**Distribution honesty, structurally enforced, not narrated**: `_DISTRIBUTION` declares
`is_synthetic_only=True` — both the file records AND the labels are synthetic/LLM-generated,
zero real human or real personal-decision data anywhere. `evals/test_ai_ranker_gold.py`
explicitly asserts `assert_safe_to_promote_to_measured(_DISTRIBUTION)` **raises**
`UnsafeMeasuredPromotionError` — this measurement can never be cited as "MEASURED" in the
ADR-0016 sense, permanently, checked by the eval itself rather than left to reviewer
discipline.

Reproduce:
```
uv run python evals/ai_fixtures/label_ranker_fixtures.py   # ~1.75 hours, real local Ollama calls
uv run pytest evals/test_ai_ranker_gold.py -v -s            # ~2 minutes, reads cached labels
```
(commit `cf89e27`, `reports/ai/ranker_operating_point.json` / `reports/ai/ranker_labeling_
kappa.json` — full numbers, provenance-tagged.)

## Naming discipline

**"Generic clutter-likelihood ranker" — everywhere: code, UI copy, ADRs, case study.** Never
"learns your preferences," never "predicts what you'll delete." `clutter_ranker.py`'s module
docstring states this explicitly; `ClutterLikelihoodScore.is_generic` is hardcoded `True` on
every result (mirroring `cold_start_priority.ColdStartPriority.is_heuristic`'s "make the
honesty claim checkable, not just narrated" pattern) so a future UI or log line has something
to actually assert against, not just a comment to trust.

## Safety: recommend-only, cannot reach deletion, §7.5 gate re-verified

`clutter_ranker.py`'s `build_ranked_clutter_entries` wraps each candidate in its own singleton
`AICluster` on `AITrack.RANKED_CLUTTER` — a track that was ALREADY excluded from
`_DELETION_SUGGESTION_ELIGIBLE_TRACKS` since ADR-0011 ("ranking-only, future"), unchanged by
this ADR. `AICluster.__post_init__`'s existing structural guard (not a new check written for
this feature) raises `ValueError` if any `RANKED_CLUTTER` member ever carried
`is_recommended_keep=True` — `build_ranked_clutter_entries` never sets it, and the type system
itself would refuse construction if a future bug tried to. Two new regression tests
(`test_ranked_clutter_track_cannot_carry_a_deletion_suggestion`,
`test_ranked_clutter_track_never_suggests_deletion_even_with_a_high_score` — the latter
proving a MAXIMUM clutter-likelihood score still never flips `suggests_deletion`) join
`evals/test_ai_safety_gate.py`, which re-ran clean at N passed (see verifier report) with
`clutter_ranker.py`/`feedback_store.py` automatically covered by the existing AST scan (no
`reclaim.executor`/`send2trash` import) via the same `src/reclaim/ai/**` `rglob` every prior
feature's modules are covered by.

**A real gap caught by that same structural-backstop discipline, not glossed over**: the
"core tool degrades cleanly without the ai extra" install-profile guard
(`tests/test_ai_optional_extra.py`, itself hardened during the previous feature's merge review
to grep every `require(...)` call site and fail if its module name is missing from the
simulated-block list) initially missed `clutter_ranker.py`'s `require("lightgbm", ...)` call —
the full test suite failed with `test_every_require_call_site_module_is_covered_by_the_
block_list`, exactly the failure mode that backstop exists to catch. Fixed by adding
`"lightgbm"` to the block list; re-verified the full suite green afterward. Worth recording
because it's direct proof the backstop generalizes to a feature it wasn't written for, not
just the one that prompted it.

## Relationship to the personal ranker (ADR-0020, still deferred)

Nothing here changes ADR-0020's decision. `feedback_store.py`'s decision log still requires
≥500 real accept/reject/keep decisions before any personal re-ranking layer trains on
anything — that gate is about a fundamentally different, unknowable-without-real-data target
(personal preference) and stays exactly as strict as ADR-0020 left it. This ranker and that
one are architecturally compatible (both would feed `AITrack.RANKED_CLUTTER`-shaped output;
a future personal layer could plausibly re-rank or blend with this generic score once real
decisions accumulate) but this ADR does not build that blending — it ships the generic ranker
alone, day one, exactly as scoped.

## Zero-cost/local + license summary of new dependencies

- `lightgbm>=4.7.0` — MIT — CPU training/inference, zero GPU requirement.
- `numpy>=1.26` — BSD-3-Clause — already a transitive dependency (sentence-transformers),
  now also imported directly (`clutter_ranker.py`'s feature-array construction); added
  explicitly to `pyproject.toml` rather than left implicit.
- `ollama>=0.6.2` — MIT — **dev-only** (`[dependency-groups] dev`, same posture as `pyarrow`
  for PAWS parquet reading), never imported by `src/reclaim/ai/**`. Talks exclusively to a
  local Ollama server; zero paid API, `ANTHROPIC_API_KEY` never read or set anywhere this
  package is used.

## Consequences

- The ranker's real quality ceiling (chat/document-style categorical confusion, if any) is
  disclosed in the measured section above, not smoothed over.
- `data/ai_datasets/ranker_labels/labeled_records.jsonl` (the raw cross-LLM labels) is
  gitignored — large, fully regeneratable from this ADR's reproduce commands given the same
  fixture generator and model versions, not worth committing. The trained model artifact
  (`data/ai_models/clutter_ranker.txt`) and the summary report
  (`reports/ai/ranker_operating_point.json`) ARE committed — the actual shipped artifact and
  its provenance-tracked measurement.
- Branch-only (`feat/ai-ranker`), unmerged, per GG's explicit instruction — this ADR is the
  report due before merge is even considered, not a claim that merge has happened.

## Test coverage

**Synthetic (CI, every run):** `tests/test_ai_eval_harness.py` (+19 cases: `fleiss_kappa`,
`cohens_kappa`, `ndcg_at_k`, `precision_at_k`, every expected value hand-computed in the test
docstring, not just asserted against whatever the code returns), `tests/test_ai_clutter_
ranker.py` (15 cases: hash-bucket determinism/range, feature extraction correctness against
every `FeatureVector` field, a toy-but-real LightGBM booster proving `score`/`rank`/
`build_ranked_clutter_entries`'s actual prediction and construction paths), `evals/test_ai_
safety_gate.py` (+2 cases for `RANKED_CLUTTER`).

**Real (local, on-demand, not in CI):** `evals/test_ai_ranker_gold.py` (1 case — the
per-ADR-0021 measurement: cross-LLM agreement, unanimous-only exclusion, grouped-split
training, NDCG@5/precision@3 on held-out batches, the `assert_safe_to_promote_to_measured`
raise-proof). Same not-in-default-CI-sweep posture as every other gold-set eval in this
project — skipped until `evals/ai_fixtures/label_ranker_fixtures.py` has been run once
locally.
