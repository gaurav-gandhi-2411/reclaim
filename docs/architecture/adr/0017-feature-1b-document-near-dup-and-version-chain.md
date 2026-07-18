# 0017. Feature 1b: document near-dup (MinHash/LSH + MiniLM) and version-chain operating points

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

- Feature 1b's shipped defaults: `minhash_threshold = 0.2` (MEASURED, real, ADR-0016-compliant),
  `embedding_threshold = 0.6` (confirmation-safety-margin, not independently PR-curve-derived —
  disclosed as such), version-chain ordering via `order_version_chain` (measured 1.0/1.0 on a
  disclosed constructed fixture, not a public dataset).
- Re-acquiring PAN-PC-11 (RAR extraction tooling) or GG's own gold-set labels (still unrun) are
  both legitimate future upgrades to this measurement, not blockers — the current measurement is
  real and defensible, not a placeholder.
- The version-chain fixture's lack of pattern/mtime CONFLICT cases is a known, disclosed gap —
  not asserted as covered.
- Per GG's explicit "report before Track B/2/3" — this ADR is that report's technical backing.

## Test coverage

**Synthetic (CI, every run):** `evals/test_ai_document_similarity_gate.py` (4 cases — PR-curve
selection machinery, BCubed floor, keep-best, end-to-end safety-filtered orchestration),
`evals/test_ai_version_chain_gate.py` (1 case, 8 chains), `tests/test_ai_document_text.py` (8),
`tests/test_ai_minhash_lsh.py` (5), `tests/test_ai_version_chain.py` (9),
`tests/test_ai_document_keep_best.py` (3), `tests/test_ai_document_similarity.py` (3),
`tests/test_ai_eval_harness.py` (+10 for `kendall_tau`/`exact_order_accuracy`).

**Real (local, on-demand, not in CI):** `evals/test_ai_document_gold.py` (2 cases — the
MinHash MEASURED operating point + per-tier recall, and the embedding confirmation-recall
check), `evals/test_ai_paws_embedding_gold.py` (1 case — the disclosed adversarial boundary-tier
check). Same not-in-default-CI-sweep posture as `evals/test_ai_copydays_gold.py`, same reason
(network + real-dataset dependency shouldn't gate every push).
