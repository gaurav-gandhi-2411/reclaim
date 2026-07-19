# Merge review: `feat/ai-layer` → `main`

**Status: NOT MERGED.** This document is the review artifact — merge is GG's explicit call
after reading it. Branch has no remote (local-only, never pushed); `main` currently contains
zero AI-layer code. 23 commits, 89 files changed, +14,465 lines.

## What this branch adds

The full applied-AI layer per `reclaim-ai-features-spec.md`: recommend-only review-queue
suggestions layered beside the deterministic engine, never merged into its auto-delete path.

| feature | what it does | status |
|---|---|---|
| 1a Track A | pHash/dHash near-identical image clustering + classical keep-best scorer | shipped, MEASURED |
| 1a Track B | CLIP semantic grouping (browse-only) | **not built** — `AITrack.SEMANTIC_IMAGE` remains an unimplemented placeholder |
| 1b | MinHash/LSH + sentence-embedding document near-dup, version-chain ordering | shipped, MEASURED |
| 2 | screenshot-burst detection (dimensions + time + pHash) + local OCR + content-tag classifier | shipped, PROVISIONAL |
| 3 | feedback-logging (decision store + feature vectors + cold-start heuristic) | shipped; **LambdaMART ranker deliberately deferred**, no training code exists |

Gold-set labeling tool (`scripts/ai_label_tool.py`) is built and verified but has not been run
against GG's real disk — every operating point below is validated against a public/constructed
dataset, not GG's own personal data yet.

## 1. §7.5 recommend-only safety gate — run against the full assembled branch

`evals/test_ai_safety_gate.py` is not per-feature scaffolding — its two structural checks scan
**every** `.py` file under `src/reclaim/ai/` via `rglob`/AST walk, so it automatically covers
all four features' modules (including everything Feature 2/3 added this session) with no
per-feature update required:

- `test_ai_package_never_imports_the_executor_or_send2trash` — **PASS**. Zero imports of
  `reclaim.executor` or `send2trash` anywhere under `src/reclaim/ai/`.
- `test_ai_cluster_member_fed_to_apply_batch_fails_loudly_before_any_disk_io` — **PASS**.
  Handing `apply_batch` an `AIClusterMember` instead of a `Candidate` raises `AttributeError`
  before any filesystem call; confirmed zero mutation (`target.exists()`, byte-identical
  content, no manifest/vault created).
- `test_malicious_config_cannot_inject_an_ai_category_section` /
  `..._add_an_ai_tier_field_to_an_existing_category` — **PASS**. `pydantic`'s `extra="forbid"`
  rejects both adversarial config-injection attempts at load time.
- `test_ai_category_group_prefix_is_never_emitted_by_the_deterministic_detectors` — **PASS**.
- `test_review_queue_partitions_all_five_tracks_correctly` — **PASS**. All five tracks
  (`NEAR_IDENTICAL_IMAGE`, `NEAR_DUP_DOCUMENT`, `VERSION_CHAIN`, `SCREENSHOT_BURST` — all four
  features' deletion-eligible tracks — plus `SEMANTIC_IMAGE` browse-only) partition correctly
  through one shared `AIReviewQueue`.
- `test_cli_and_api_service_never_import_reclaim_ai_today` / `test_apply_batch_signature_has_
  no_ai_layer_parameter` — **PASS**. No dashboard/CLI wiring exists yet at all; `apply_batch`/
  `Candidate` have no AI-related parameter or field.

**Full result: 18/18 passed** (`uv run pytest evals/test_ai_safety_gate.py -v`, commit
`36ddbd0`). No AI candidate can reach `apply_batch` or the deterministic Tier-A path across all
four features integrated.

## 2. Both install profiles

**Core-only (no `ai` extra) — genuinely tested, not simulated.** Built a fresh, isolated venv
(`uv venv` + `uv pip install <repo>`, zero `[ai]` extras), confirmed:
- `reclaim.cli` imports cleanly.
- Every `reclaim.ai.*` submodule imports cleanly (including all of Feature 2/3's new modules)
  — no eager heavy-dependency import anywhere.
- `cv2`/`rapidocr_onnxruntime` genuinely absent (`ModuleNotFoundError` confirmed directly).
- Calling an AI function anyway (`keep_best.score_image_quality`) raises the actionable
  `AIExtraNotInstalledError` — *"sharpness/exposure quality scoring needs the optional 'cv2'
  package... Install the AI extras: `uv sync --extra ai`"* — never a raw traceback.
- Core functionality (`Config`, `SafetyValidator`) instantiates and runs normally.

**A real gap was found and fixed during this check, not just narrated as passing.**
`tests/test_ai_optional_extra.py`'s "core tool degrades cleanly" proof predated Feature 1b/2/3:
it hardcoded a 5-submodule import list and only simulated blocking `cv2`/`imagehash` — every
module Feature 2/3 added, and every dependency they introduced (`rapidocr_onnxruntime`,
`datasketch`, `sentence_transformers`, `docx`, `pypdf`, `numpy`, `PIL`), was never exercised by
this test at all. Fixed: submodule discovery via `pkgutil` (not a hand-maintained list), the
full dependency set in the block list, and a structural backstop that greps every `require(...)`
call site via `ast` and fails if its module name is missing from the block list — so a future
feature adding a new optional dependency and forgetting to update this test fails loudly instead
of silently under-blocking. Committed `36ddbd0`.

**`[ai]`-extra profile** (`uv sync --frozen --all-groups --extra ai`, replaying CI's
`ai-layer-with-extras` job verbatim):
- `ruff check .` — **PASS**.
- `mypy` — **PASS** (45 source files, strict).
- `pytest tests/ evals/test_ai_safety_gate.py -v` — **530 passed, 2 skipped**.

Both profiles clean.

## 3. Four harness invariants — regression tests confirmed running in CI, passing at branch head

| invariant | test | location | CI coverage |
|---|---|---|---|
| Hard-tier rejection (1a) | `test_assert_safe_to_promote_to_measured_rejects_adversarial_tail_only` | `tests/test_ai_eval_harness.py` | unit-level, always runs (`ci.yml`'s `pytest --cov` step, every push/PR to `main`) |
| Per-tier no-pooling (1b) | `test_select_joint_operating_point_per_tier_rejects_the_real_adr0017_incident` | `tests/test_ai_eval_harness.py` | same — always runs |
| Version-chain conditional-keeper (1b) | `test_build_version_chain_cluster_flags_for_review_when_signals_disagree` | `tests/test_ai_version_chain.py` | same — always runs |
| Near-empty-OCR → UNKNOWN (F2) | `test_tag_content_sparse_gibberish_is_unknown_not_transient_ui` (unit) + `test_degraded_real_content_never_tags_transient_ui` (real-OCR eval) | `tests/test_ai_content_tagger.py` + `evals/test_ai_screenshot_ocr_degradation_gate.py` | unit always runs; the eval file has **no** `skipif` precondition (unlike the gold-set measurement evals, which self-skip in CI when a corpus isn't fetched) — it runs unconditionally in `eval.yml`'s `pytest evals/ -v` |

All four run individually at branch head, confirmed:

```
tests/test_ai_eval_harness.py::test_assert_safe_to_promote_to_measured_rejects_adversarial_tail_only PASSED
tests/test_ai_eval_harness.py::test_select_joint_operating_point_per_tier_rejects_the_real_adr0017_incident PASSED
tests/test_ai_version_chain.py::test_build_version_chain_cluster_flags_for_review_when_signals_disagree PASSED
tests/test_ai_content_tagger.py::test_tag_content_sparse_gibberish_is_unknown_not_transient_ui PASSED
4 passed in 0.12s
```

```
evals/test_ai_screenshot_ocr_degradation_gate.py::test_degraded_real_content_never_tags_transient_ui PASSED
16 (content_kind x degradation) combinations: 0 ever tagged transient_ui;
12/16 degraded to near-empty OCR output, all 12 -> UNKNOWN.
1 passed in 15.03s
```

## 4. Merge-review summary

### The four ADRs' operating points

| ADR | feature | shipped operating point | distribution | caveat |
|---|---|---|---|---|
| **0012** | 1a — pHash near-identical images | `max_hamming_distance = 14` | **realistic** transforms (mild/moderate/messaging-app resave on real Copydays photos): precision 0.9987, recall 1.0000 — MEASURED, `assert_safe_to_promote_to_measured` passes | hard tier (Copydays `strong`, adversarial): precision 0.9600, recall **0.0764** — real, kept, but explicitly forbidden from being cited as "how often Feature 1a catches real duplicates" anywhere user-facing |
| **0017** | 1b — document near-dup + version-chain | `minhash_threshold = 0.1`, `embedding_threshold = 0.95` (joint, per-tier gated) | prose (real Gutenberg): precision 1.0000/recall 0.8694; templated (synthetic-but-realistic): precision 0.9627/recall 0.7963 — MEASURED | recall cost is real, not free: 0.9917→0.8694 prose, and templated recall caps at 0.7963. Only 3 template types tested (resume/invoice/report-memo). Version-chain: 1.0/1.0 exact-order/Kendall's-tau across 8 chains + 0 safety violations across a dedicated 4-chain filename-vs-mtime conflict fixture |
| **0019** | 2 — screenshot burst + content-tag classifier | pHash ≤14 (reused, not re-measured) + 60s window (disclosed policy) for bursts; content-tag confidence floor 1.5 (shipped constant, not swept) | transient_ui (safety-critical): precision 1.0000/recall 1.0000. receipt 1.0/1.0, code 0.826/0.974, chat 0.667/1.0, document 1.0/0.658 | **PROVISIONAL, permanently** — `is_synthetic_only=True` (3 of 5 tags are synthetic), `assert_safe_to_promote_to_measured` deliberately never called. `chat` precision (0.667) and `document` recall (0.658) are disclosed weaknesses — safe (both keep-biased either way) but imperfect |
| **0020** | 3 — feedback-logging | **none** — no ranker exists to have an operating point | n/a | the cold-start heuristic (`compute_cold_start_priority`) is explicitly NOT a measured/fit value — a documented formula, `is_heuristic=True` hardcoded on every result. The ranker's `≥500`-decision activation gate and time-split eval protocol are documented obligations for a future PR, not implemented here |

### The safety boundary proof

Structural, not conventional: `AICluster`/`AIClusterMember` share zero field names with
`Candidate` (a caller handing one to `apply_batch` gets `AttributeError` before any disk I/O,
because the object literally lacks the field, not because a convention was followed), an AST
scan re-verified on every CI run that no file under `src/reclaim/ai/` imports the executor or
`send2trash`, and `pydantic`'s `extra="forbid"` closes the named adversarial config-injection
path. Re-confirmed against the fully assembled branch in §1 above — 18/18 safety-gate tests
pass with all four features integrated, not just per-feature in isolation.

### Residual risks, disclosed

- **The ADR-0018 pooling-footgun backstop is code-review discipline, not a runtime guard —
  named explicitly, not glossed over.** `select_operating_point`/`select_joint_operating_point`
  (the original single-distribution functions, still valid for a genuinely single-tier
  measurement) remain fully callable with pooled multi-tier data — nothing in either function
  can detect from a flat list of pairs that it came from more than one source, so the exact
  0.9524-pooled/0.8634-real-templated-tier incident *could* recur if a future eval hand-rolls a
  multi-tier measurement instead of calling `select_operating_point_per_tier`/
  `select_joint_operating_point_per_tier`. Both docstrings carry an explicit "STOP: is this
  pooled?" warning naming ADR-0018, but the actual backstop is a human or an automated reviewer
  catching a hand-rolled multi-tier loop in a future PR — the same class of trust any
  misusable-but-documented API carries. `tests/test_ai_eval_harness.py`'s regression test
  proves the *fix* holds; it cannot prove a *future* eval won't reintroduce the mistake by a
  different path.
- **1a's realistic-distribution measurement covers 3 named transform profiles** (mild
  recompress/resize, moderate resize+recompress+PNG-roundtrip, messaging-app resave) — real
  photographic content, but not an exhaustive simulation of every real-world duplicate-creation
  path (e.g., a screenshot of a screenshot, heavy watermarking).
- **1b's templated-document tier is synthetic-but-realistic, not sampled from a real templated-
  document corpus** — no license-clean public dataset of resumes/invoices/reports was found;
  disclosed in ADR-0017 as a constructed fixture, same posture as the version-chain fixture.
- **F2's content-tag classifier is permanently PROVISIONAL** by its own distribution
  declaration — 3 of 5 tags are synthetic. The safety property (TRANSIENT_UI precision, the
  only number that actually matters for over-deletion risk) is measured at 1.0000 with margin
  and independently re-proven against real image degradation (§3), but the classifier's overall
  *tagging quality* (chat/document confusion) has real, disclosed room to improve.
- **F3 has no real feedback data yet** — the feature vector schema and cold-start heuristic are
  both unvalidated against actual accept/reject/keep decisions, by construction (there are none
  yet). This is the intended state, not a gap to close before merge.
- **No real-disk validation of any operating point above** — GG's own gold-set labeling tool
  (`scripts/ai_label_tool.py`) is built, verified, and untouched. Every number in this document
  comes from a public dataset or a disclosed constructed fixture, never GG's own photos/
  documents/screenshots.
- **No dashboard/CLI wiring exists** — `test_cli_and_api_service_never_import_reclaim_ai_today`
  is a deliberate, currently-true assertion, not a stale one. This is the strongest possible
  safety posture at this stage (nothing to wire, nothing to accidentally connect to
  `apply_batch`), but it also means none of this is reachable by an end user yet — that's a
  separate, future scope decision, not evaluated here.

## Verification commands (all run against branch head, commit `36ddbd0`)

```
uv run ruff check .                                         # PASS
uv run ruff format --check .                                # PASS (all files)
uv run mypy                                                  # PASS (45 source files, strict)
uv run pytest tests/ -q                                      # 512 passed, 2 skipped
uv run pytest evals/ -q --ignore=evals/test_dedup.py         # 50 passed
uv run pytest evals/test_ai_safety_gate.py -v                # 18 passed
uv sync --frozen --all-groups --extra ai                     # clean
(isolated venv, core deps only) reclaim.cli + reclaim.ai.*   # import clean, degrade correctly
```

---

**Merge is not performed by this pass.** Everything above is evidence for GG's own review —
the decision to merge `feat/ai-layer` into `main` is his explicit call.
