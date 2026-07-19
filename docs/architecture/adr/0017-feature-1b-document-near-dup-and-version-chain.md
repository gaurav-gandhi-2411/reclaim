# 0017. Feature 1b: document near-dup (MinHash/LSH + MiniLM) and version-chain operating points

## Status update — the shipped MinHash/embedding thresholds changed after a templated-document follow-up

**`minhash_threshold`/`embedding_threshold` are now `0.1` / `0.95` (jointly, gated on BOTH a
prose and a templated-document tier), not the `0.2` / `0.6` this ADR originally measured.**
GG flagged that measuring against edited literary prose alone can't surface real document
clutter's actual failure mode — heavy TEMPLATING (resumes, invoices, reports, decks), where
genuinely different documents share large blocks of identical boilerplate. Measured against a
new templated-document tier, the original thresholds produced a **71% false-positive rate**
(precision 0.2893) — a real, serious finding. See "Follow-up: the templated-document blind
spot" below for the full measurement; the sections immediately following this one are kept
as-written for history (they were true of the prose-only measurement) but are **no longer the
shipped operating point** — read the follow-up section for what actually ships.

## Context

Following Feature 1a's pattern exactly, per GG's explicit instruction — and applying ADR-0016's
gate-hardening lesson from the *start* this time, not as a retroactive fix: every operating
point in this ADR is measured against a distribution honestly declared via
`eval_harness.DistributionDeclaration`, with both a precision floor AND a recall/usefulness
floor, and `assert_safe_to_promote_to_measured` is called on whichever distribution actually
backs a MEASURED claim.

Three sub-problems, three operating points:
1. MinHash/LSH Stage-1 Jaccard-similarity threshold (document near-dup prefilter).
2. Sentence-embedding Stage-2 cosine-similarity threshold (residual confirmation only).
3. Version-chain ordering (a heuristic, not a threshold — evaluated on exact-order accuracy
   and Kendall's tau instead).

## Dataset evaluation (GG's instruction: assess PAN plagiarism, Quora Question Pairs, PAWS, or
a documented dedup benchmark)

| Candidate | Task match | License | Verdict |
|---|---|---|---|
| **Quora Question Pairs (QQP)** | Good — real human-labeled duplicate-question pairs | Quora's own Terms of Service carry a **no-commercial-use** restriction; Hugging Face's own dataset card lists the license as "unknown." Reclaim's stated long-term posture (CLAUDE.md: "could compete with the best in market before monetization") makes a non-commercial-restricted dataset an inappropriate basis for a shipped operating point. | **Rejected on license** — the same discipline ADR-0015 applied to California-ND/AVA. |
| **PAWS (Paraphrase Adversaries from Word Scrambling)** | Real Wikipedia sentences, but adversarial **by construction and by name** — negatives are generated via word-order scrambling and back-translation specifically to defeat naive similarity measures | CC-BY-4.0 (content) / Apache-2.0 (code), clean | **Used as a disclosed secondary/boundary check only** (`evals/test_ai_paws_embedding_gold.py`) — never the basis for MEASURED status, exactly the ADR-0016 lesson. See "PAWS-Wiki disclosure" below for what it actually showed. |
| **PAN plagiarism corpus (PAN-PC-11)** | Strong task match (real document-length plagiarism/copy detection, graduated artificial/simulated severity) | CC-BY-4.0, open access on Zenodo, no registration gate | **Not used** — reachable and clean-licensed, but ~1.7GB across two RAR-archive parts, a format this environment has no extraction tooling for without adding a new dependency purely to unpack one dataset. A reasonable future upgrade path (see Consequences), not pursued this pass given the programmatic-generation alternative was already available and proven (ADR-0012's realistic-tier pattern). |
| **A documented dedup benchmark, generated from clean public-domain content** | Direct match by construction | Public domain (Project Gutenberg, pre-1928 works) — zero licensing risk | **Selected as the primary source** — see below. |

**Decision: programmatically-generated realistic distribution from Project Gutenberg, same
pattern as ADR-0012's Copydays follow-up, plus PAWS-Wiki as a disclosed secondary check.**
`evals/ai_fixtures/fetch_gutenberg_texts.py` downloads 8 public-domain novels (Pride and
Prejudice, Frankenstein, Alice in Wonderland, The Adventures of Sherlock Holmes, Moby Dick, A
Tale of Two Cities, Adventures of Huckleberry Finn, Dracula — all pre-1928, unambiguously public
domain), strips Gutenberg's boilerplate header/footer (which would otherwise be spurious
"near-duplicate" text shared across every unrelated book). `evals/ai_fixtures/
build_document_realistic_tiers.py` chunks each book into 15 real, paragraph-aligned,
document-length pieces (300+ words each — 120 chunks total) and applies 3 deterministic
realistic transform profiles per chunk (360 real-content variants):

| profile | tier | simulates |
|---|---|---|
| `mild_whitespace_cleanup` | mild | re-saved through a different text editor |
| `moderate_paragraph_trim_and_reorder` | moderate | a meaningfully revised copy |
| `collab_tool_paste` | collab_paste | pasting into a chat/email/collab tool — flattens paragraph structure, truncates to ~70% |

## MinHash/LSH Stage-1: MEASURED operating point

Reproduce:
```
uv run python evals/ai_fixtures/fetch_gutenberg_texts.py
uv run pytest evals/test_ai_document_gold.py -v -s
```

360 positive pairs (each variant vs. its real base chunk) + 7,140 clean negative pairs (all
`C(120, 2)` base-chunk-vs-different-base-chunk combinations, deliberately excluding any variant
from the negative pool for the same "don't taint negatives" reasoning as ADR-0012's follow-up)
= 7,500 total real pairs.

**Measured (mechanically selected, bare minimum): Jaccard threshold = 0.1328, precision =
1.0000, recall = 1.0000.** Every one of the 7,140 negative pairs scored below 0.1328 — the
highest real cross-book negative similarity observed was only **0.0234** (between two chunks
of the *same* book, "A Tale of Two Cities" — same author/style, still clearly separated).

**Chosen production value: `minhash_jaccard_threshold = 0.2`** — not the bare-measured minimum.
Margin reasoning, mirroring ADR-0012's threshold-14-over-2 precedent, now with real numbers:
0.2 is still comfortably above the highest observed real negative (0.0234 — over 8x margin),
costs almost nothing in recall (0.9917 aggregate vs. 1.0000 — 3 of 360 pairs, all from the
`moderate` tier's most heavily-reordered outliers), and protects against a real, disclosed gap
in the negative pool: every negative here is a pair of *unrelated* books, not two *topically
similar but genuinely different* documents (e.g., two different quarterly reports) — the kind
of harder negative this measurement doesn't cover and a production threshold should have margin
against.

Per-tier recall at 0.2: mild 100%, moderate 97.5%, collab_paste 100%.

`assert_safe_to_promote_to_measured` is called on this distribution in
`evals/test_ai_document_gold.py` and does not raise (`is_realistic=True`,
`is_adversarial_tail_only=False`, `is_synthetic_only=False`) — this MEASURED claim is
structurally, not just narratively, compliant with ADR-0016.

## Sentence-embedding Stage-2: confirmation-only, not independently PR-curve-derived — disclosed

**A real, honest finding: on this measured distribution, Stage 1 alone already achieves
precision 1.0 / recall 0.99 — there is no ambiguous residual left for Stage 2 to resolve.**
Every one of the 357 Stage-1-approved candidate pairs (at threshold 0.2) is a true positive;
zero negatives ever reach Stage 2 in this measurement. This means Stage 2's cosine threshold
cannot be meaningfully selected via a PR curve *on this dataset* — there's nothing for it to
discriminate against. This is reported honestly rather than manufacturing a PR curve out of a
residual that doesn't exist.

**What Stage 2's threshold is actually set from:** the minimum cosine similarity observed among
genuine (Gutenberg-realistic) positive pairs — **0.6467** (the same `moderate`-tier outlier
that also had the lowest Jaccard similarity) — with a margin below it. **Chosen:
`embedding_threshold = 0.6`** — safely under every real positive observed (mild/collab_paste
both scored a clean 1.0000; moderate's minimum was 0.6467), so Stage 2 essentially never
falsely rejects a Stage-1-approved candidate on this measured distribution, which is its actual
job (confirmation, not primary classification).

**PAWS-Wiki disclosure (the adversarial boundary-tier check, never the MEASURED basis):**
`evals/test_ai_paws_embedding_gold.py` ran the full embedding PR curve against PAWS-Wiki's
8,000 real, human/machine-labeled Wikipedia sentence pairs. Result: **≥0.95 and ≥0.90 precision
were NOT achievable at all**; ≥0.85 precision was achievable only at **0.23% recall**
(cosine ≥ 1.0000 — essentially requiring near-exact duplicates). This is a real, honestly
reported finding, and it is *exactly* what PAWS is designed to do — defeat naive similarity
measures via high lexical overlap with low semantic similarity. It does **not** mean Feature
1b's embedding stage is broken; it means naive sentence-embedding similarity, used **alone**,
cannot cleanly separate PAWS's specific adversarial construction, which is a different and much
harder problem than Stage 2's actual job (confirming candidates a real document-length MinHash
prefilter already approved). Citing this PAWS result as "Feature 1b's real precision" would be
the exact ADR-0012-incident mistake this ADR is built to avoid — `_ADVERSARIAL_DISTRIBUTION`'s
`is_adversarial_tail_only=True` flag exists specifically to prevent that.

## Follow-up: the templated-document blind spot (and why prose-only measurement couldn't see it)

**Why this exists.** Everything above was measured against edited *prose* — real book text
with realistic edits applied. GG's insight: prose has no structural analog to what a resume,
invoice, or report actually looks like. Two different people's resumes share section headers
("OBJECTIVE," "EXPERIENCE," "EDUCATION," "SKILLS"), standard connective phrasing ("results-
driven professional," "led cross-functional initiatives"), and formatting scaffolding — while
being genuinely unrelated documents. Word-shingle MinHash is exactly the kind of algorithm
this can fool: high shingle overlap from shared boilerplate can push two unrelated documents'
Jaccard similarity well above a threshold tuned on prose, where no comparable shared-structure
problem exists at all.

**Built a templated-document tier** (`evals/ai_fixtures/build_templated_document_fixtures.py`):
3 synthetic-but-structurally-realistic templates (resume, invoice, report-memo), each with
substantial real boilerplate (the kind an actual template carries) and a pool of varying
synthetic fields (20 first names, 20 last names, 12 companies, 8 job titles, 6 degrees/schools,
8 line-item types — no real PII) large enough that any two instances share no specific
personal/business detail. 18 instances per template = 54 base documents. **Negatives**: every
same-template, different-instance pair — `C(18,2) x 3 = 459` pairs, the exact failure mode
being tested (genuinely different documents, heavy shared boilerplate). **Positives**: the same
3 realistic transform profiles from the prose tier (mild/moderate/collab_paste) applied to each
of the 54 base documents = 162 true near-dup pairs.

**The predicted failure reproduced immediately and severely.** Same-template-different-content
Jaccard similarity: resume pairs averaged **0.51** (max 0.65), invoice pairs up to 0.62, report
pairs up to **0.84** — all far above the prose-measured threshold of 0.2.
**At the originally-shipped thresholds (`minhash=0.2`, `embedding=0.6`): precision on the
templated tier was 0.2893** (398 false positives out of 560 flagged pairs) — a feature that
would confidently tell a user two different people's resumes are duplicates, roughly 7 times
out of 10 it flags anything. Reproduce:
```
uv run pytest evals/test_ai_document_templated_gold.py -v -s
```

**A second, more sobering finding: Stage 2 (embeddings) does NOT cleanly solve this alone
either.** The build brief predicted embeddings "can distinguish same template, different
content semantically where MinHash can't" — measured, that's only partly true. The maximum
cosine similarity among templated negatives was **0.976**, which is *higher* than the minimum
cosine similarity among some genuine positive pairs (moderate-tier prose positives went as low
as 0.6467). `all-MiniLM-L6-v2` truncates at 256 tokens — for a ~300-400 word document, the
model sees most or all of the shared boilerplate, and a general-purpose sentence embedding
pooled over mostly-identical text is dominated by that shared structure, not by the smaller
proportion of genuinely distinguishing detail (names, numbers, specific claims). Neither stage
alone is sufficient for templated documents.

**What DOES work: a much stricter JOINT (AND-gated) threshold, gated independently on both
tiers — at a real, measured recall cost.** `eval_harness.select_joint_operating_point` (a new,
unit-tested, reusable 2D analog of `select_operating_point` for exactly this two-stage
AND-gated shape) was added, then a per-tier-INDEPENDENT grid search was run directly in the
eval (not the pooled/aggregate version — see the important correction below). Grid search
result, gated so BOTH tiers clear the ≥0.95 precision target and the 0.5 recall floor
independently:

| minhash_threshold | embedding_threshold | prose precision | prose recall | templated precision | templated recall |
|---:|---:|---:|---:|---:|---:|
| 0.2 (old) | 0.6 (old) | 1.0000 | 0.9917 | **0.2893** | 0.9938 |
| **0.1 (new)** | **0.95 (new)** | 1.0000 | **0.8694** | **0.9627** | **0.7963** |

**`minhash_threshold = 0.1`, `embedding_threshold = 0.95` is now Feature 1b's shipped
operating point.** The honest cost: prose recall drops from 0.9917 to 0.8694 (mostly
`moderate`-tier pairs whose embedding similarity fell below the new, much stricter 0.95 bar),
and templated recall is 0.7963 — a real, measured, disclosed price for safety across both
distributions, not a free win. This is the spec's own stated priority in action ("precision is
favored over recall throughout: a false 'these are duplicates' ... is the expensive error") —
a resume/invoice/report false-positive is exactly that expensive error, and it costs real
recall to prevent.

**A real methodological bug caught and fixed during this measurement, worth recording:** the
first version of this eval pooled prose's 7,140 negatives and the templated tier's 459
negatives into one combined precision calculation via `select_joint_operating_point`. That
pooled aggregate was itself misleading — a threshold combination that looked like it cleared
0.95 precision in aggregate (0.9524) actually had only **0.8634** precision on the templated
tier alone, because the templated tier's false positives were mathematically swamped by the
much larger, cleanly-separated prose negative pool. The eval's own per-tier gate assertion
caught this (the test failed, correctly, the first time it ran) — proving GG's explicit
instruction ("gate on precision AND recall for both tiers") wasn't just followed narratively
but is what actually caught a real bug in the measurement itself. The eval was rewritten to
grid-search with both tiers gated independently from the start, not pooled.

**Both stages' code and docstrings updated to reflect this**: `minhash_lsh.py`'s
`cluster_by_jaccard_similarity` docstring now explicitly warns that `min_similarity=0.1` is
**only** safe when paired with Stage 2's `0.95` cosine confirmation — used alone (bypassing
Stage 2), it is *looser* than even the original, already-inadequate prose-only threshold of
0.2, and would be worse on templated documents, not better.

**Disclosed scope of this follow-up**: 3 template types only (resume/invoice/report-memo), not
real decks/spreadsheets/forms or templates with a different boilerplate-to-content ratio;
synthetic filler content, not sampled from a public templated-document corpus (none of the
license-clean options assessed for the original ADR — Gutenberg, PAWS — contain templated
documents; this is a disclosed, constructed fixture, same posture as the version-chain
fixture). `_COMBINED_DISTRIBUTION`'s `untested_variation_note` carries this as a machine-checked
field, not just prose.

## Version-chain ordering: exact-order accuracy + Kendall's tau

No public dataset fits this sub-problem — "which order did files get renamed in" is a
Reclaim-specific filename-convention question, not a task any NLP corpus labels. Per spec
§7.1's own framing, a disclosed, constructed fixture is the correct source here (
`evals/ai_fixtures/build_version_chain_fixtures.py`, 8 chains covering numbered versions,
Windows `(N)` duplicate suffixes, `copy` markers, spelled-out "version N," `final` outranking
numbered versions, an underscore-heavy real-world-style filename, a long 6-version chain, and
one chain with **no recognizable pattern at all** — pure mtime fallback).

**Measured: mean exact-order accuracy = 1.0000, mean Kendall's tau = 1.0000** across all 8
chains (`uv run pytest evals/test_ai_version_chain_gate.py -v -s`). A real bug was caught and
fixed before this measurement, not after: `\bfinal\b`/`\bcopy\b` (word-boundary regex) silently
fail to match inside underscore-separated filenames like `report_final.docx`, because `\w`
(what `\b` boundaries on) includes underscore — exactly the spec's own example filename
("final_v2_FINAL.docx") would have been mis-ranked. Fixed to treat `_`/`-`/space/`.` as real
separators (`src/reclaim/ai/version_chain.py`).

**Disclosed limitation:** a perfect score on 8 constructed chains does not mean the heuristic
is bulletproof — none of these chains deliberately pit filename-pattern rank against mtime in
*conflict* (e.g., a numerically "later" version with an *earlier* mtime, which could plausibly
happen if someone reverted to an old draft and renamed it). This is a real, disclosed gap in
the fixture, not a claim of exhaustive coverage — a good target for a future, more adversarial
fixture revision if GG's own files ever surface such a case.

## Zero-cost/local + license summary of new dependencies

All added to `[project.optional-dependencies] ai` (except `pyarrow`, dev-only — see below),
lazy-imported via `reclaim.ai._optional.require`, never pulled in by a bare `pip install
reclaim`:

- `datasketch>=1.6` — MIT — MinHash/LSH.
- `sentence-transformers>=3.0` — Apache-2.0 — wraps `all-MiniLM-L6-v2` (also Apache-2.0), the
  exact model spec §2 names: tiny (~90MB), CPU-fast, no GPU requirement — satisfies the
  zero-cost/local-first default (house rule: "Every LLM-dependent system gets an
  Ollama or free-tier path as the default").
- `python-docx>=1.1` — MIT — local `.docx` text extraction.
- `pypdf>=5.0` — BSD-3-Clause — local `.pdf` text extraction.
- `pyarrow>=17.0` — Apache-2.0 — **dev-only** (`[dependency-groups] dev`), reads the PAWS-Wiki
  parquet files for this ADR's measurement tooling; never imported by `src/reclaim/ai/**` or
  any production code path.

No paid API, no `ANTHROPIC_API_KEY`, nothing requiring network access at runtime once the model
weights are cached locally (`sentence-transformers` downloads `all-MiniLM-L6-v2` once, then
runs fully offline).

## Safety/architecture

`AITrack.NEAR_DUP_DOCUMENT` and `AITrack.VERSION_CHAIN` joined
`_DELETION_SUGGESTION_ELIGIBLE_TRACKS` (spec §2: "deletion suggestions only for high-similarity
near-dups; version-chains are ordered recommendations") — a real, deliberate widening of the
safety-gate's own eligible-tracks set, verified NOT to weaken it: `SEMANTIC_IMAGE` remains
browse-only (unchanged), two new regression tests
(`test_near_dup_document_track_with_a_keeper_does_suggest_deletion`,
`test_version_chain_track_with_a_keeper_does_suggest_deletion`) prove both new tracks gate on
an identified keeper exactly the way `NEAR_IDENTICAL_IMAGE` already did, and a queue-level test
(`test_review_queue_partitions_all_four_tracks_correctly`) proves `AIReviewQueue` partitions all
four tracks correctly together, not just each in isolation. `AIClusterMember` gained an
`position: int | None` field for chain order — the only model change beyond the eligible-tracks
set. `document_text.py` contains zero logging calls anywhere, structurally, so extracted
document content can never reach a log line by accident.

## Consequences

- **Feature 1b's shipped defaults are now `minhash_threshold = 0.1`, `embedding_threshold =
  0.95`** (MEASURED jointly, real, ADR-0016-compliant, gated independently on a prose AND a
  templated-document tier — see the follow-up section above). The originally-measured `0.2` /
  `0.6` values are superseded, not just supplemented — citing them as Feature 1b's operating
  point anywhere would now be stale and unsafe. Version-chain ordering via
  `order_version_chain` (measured 1.0/1.0 on a disclosed constructed fixture) is unaffected by
  this follow-up — it doesn't use MinHash/embeddings at all.
- **The recall cost is real and should inform future work, not be treated as free.** 0.87
  prose recall / 0.80 templated recall at the new joint threshold is a genuine tradeoff for
  precision safety, not a rounding error. A future, more capable Stage 2 (e.g., a model that
  weights distinguishing details — names, numbers — over shared structure, or a dedicated
  field-level/named-entity diffing signal instead of whole-document embedding similarity)
  could plausibly recover recall without sacrificing the templated-tier precision this follow-
  up required — a legitimate, disclosed future improvement, not attempted in this pass.
  Per-document-type-aware thresholds (detecting "this looks templated" and applying a stricter
  gate only then) is another legitimate future direction not pursued here — the single global
  threshold chosen is the honest, simpler, currently-shipped answer.
- Re-acquiring PAN-PC-11 (RAR extraction tooling), a real public templated-document corpus (none
  found among the license-clean options assessed), or GG's own gold-set labels (still unrun) are
  all legitimate future upgrades to this measurement, not blockers.
- The version-chain fixture's lack of pattern/mtime CONFLICT cases is a known, disclosed gap —
  not asserted as covered.
- Per GG's explicit "report before Track B/2/3" — this ADR (including this follow-up) is that
  report's technical backing.

## Test coverage

**Synthetic (CI, every run):** `evals/test_ai_document_similarity_gate.py` (4 cases — PR-curve
selection machinery, BCubed floor, keep-best, end-to-end safety-filtered orchestration),
`evals/test_ai_version_chain_gate.py` (1 case, 8 chains), `tests/test_ai_document_text.py` (8),
`tests/test_ai_minhash_lsh.py` (5), `tests/test_ai_version_chain.py` (9),
`tests/test_ai_document_keep_best.py` (3), `tests/test_ai_document_similarity.py` (3),
`tests/test_ai_eval_harness.py` (+13 for `kendall_tau`/`exact_order_accuracy`/
`select_joint_operating_point`).

**Real (local, on-demand, not in CI):** `evals/test_ai_document_gold.py` (2 cases — the
original prose-only MinHash measurement + embedding confirmation-recall check, kept for
history, no longer the shipped basis), `evals/test_ai_paws_embedding_gold.py` (1 case — the
disclosed adversarial boundary-tier check), `evals/test_ai_document_templated_gold.py` (1
case — **the follow-up measurement that actually governs Feature 1b's shipped operating
point**: the templated-tier precision-collapse proof, the per-tier-gated joint grid search,
and the chosen `(0.1, 0.95)` threshold). Same not-in-default-CI-sweep posture as
`evals/test_ai_copydays_gold.py`, same reason (network + real-dataset dependency shouldn't
gate every push).
