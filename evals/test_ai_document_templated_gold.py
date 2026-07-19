from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_fixtures.build_document_realistic_tiers import (
    build_all_chunks,
    build_realistic_document_variants,
)
from ai_fixtures.build_templated_document_fixtures import (
    build_templated_documents,
    build_templated_variants,
)

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    assert_safe_to_promote_to_measured,
    current_commit_sha,
    select_joint_operating_point_per_tier,
)
from reclaim.ai.minhash_lsh import compute_document_minhash, jaccard_similarity
from reclaim.ai.text_embeddings import compute_document_embedding, cosine_similarity

# ADR-0017 follow-up: GG flagged that measuring MinHash's operating point against edited
# Gutenberg prose can't surface the failure mode real document clutter actually has — resumes,
# invoices, reports, and decks are heavily TEMPLATED, so genuinely DIFFERENT documents can
# share large blocks of identical boilerplate. This eval adds that tier and selects the JOINT
# (MinHash Jaccard, embedding cosine) operating point via `eval_harness.
# select_joint_operating_point_per_tier`, gated on BOTH tiers INDEPENDENTLY. That harness
# function exists because THIS eval's first version pooled prose (7,140 negatives) and
# templated (459 negatives) into one aggregate precision via the single-distribution
# `select_joint_operating_point` — and that pooled number was itself misleading: the templated
# tier's false positives got mathematically swamped by the much larger prose negative pool.
# ADR-0018 turned that incident into a permanent, structural harness rule (never aggregate
# across declared tiers) rather than a one-off fix in this file — see
# `tests/test_ai_eval_harness.py::test_select_joint_operating_point_per_tier_rejects_the_
# real_adr0017_incident` for the regression test reproducing the exact numbers.
#
# Not in the default CI sweep (network + multi-minute embedding compute). Reproduce with:
#   uv run python evals/ai_fixtures/fetch_gutenberg_texts.py
#   uv run pytest evals/test_ai_document_templated_gold.py -v -s

_GUTENBERG_ROOT = Path("data/ai_datasets/gutenberg_texts/cleaned")
_TARGET_PRECISION = 0.95  # spec §7.3: near-identical/deletion-suggestion tracks >= 0.95
_MIN_RECALL_FLOOR = 0.5  # ADR-0016 policy default for this track
_COMMAND = "uv run pytest evals/test_ai_document_templated_gold.py -v -s"
_FIXTURE = (
    "data/ai_datasets/gutenberg_texts (ADR-0017) + "
    "evals/ai_fixtures/build_templated_document_fixtures.py (ADR-0017 follow-up)"
)
_REPORT_PATH = Path("reports/ai/document_templated_joint_operating_point.json")
_COMBINED_DISTRIBUTION = DistributionDeclaration(
    description=(
        "Prose tier (8 public-domain Gutenberg books, 3 realistic transforms) UNION templated "
        "tier (3 synthetic-but-realistic templates -- resume/invoice/report-memo -- with "
        "varying content as negatives, same 3 realistic transforms as positives)"
    ),
    is_realistic=True,
    is_adversarial_tail_only=False,
    is_synthetic_only=False,
    untested_variation_note=(
        "templated tier covers only 3 template types (resume/invoice/report-memo) with "
        "synthetic filler content, not real decks/spreadsheets/forms or templates with less "
        "boilerplate-to-content ratio; prose tier's own gaps (see ADR-0017) still apply. "
        "Whole-document embeddings are truncated at all-MiniLM-L6-v2's 256-token limit, so "
        "very long documents are only partially represented in the Stage-2 signal."
    ),
)

pytestmark = pytest.mark.skipif(
    not _GUTENBERG_ROOT.exists() or not any(_GUTENBERG_ROOT.glob("*.txt")),
    reason=(
        "Gutenberg texts not present locally — run "
        "`uv run python evals/ai_fixtures/fetch_gutenberg_texts.py` first."
    ),
)


def _score_pairs(
    base_mh: dict, base_emb: dict, variant_records: list, negative_id_pairs: list[tuple[str, str]]
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    positive_scores = []
    for chunk_id, _profile, _tier, vmh, vemb in variant_records:
        j = jaccard_similarity(base_mh[chunk_id], vmh)
        c = cosine_similarity(base_emb[chunk_id], vemb)
        positive_scores.append((j, c))

    negative_scores = []
    for id_a, id_b in negative_id_pairs:
        j = jaccard_similarity(base_mh[id_a], base_mh[id_b])
        c = cosine_similarity(base_emb[id_a], base_emb[id_b])
        negative_scores.append((j, c))

    return positive_scores, negative_scores


def test_joint_operating_point_across_prose_and_templated_tiers() -> None:
    # --- Prose tier (ADR-0017's original measurement) ---
    chunks = build_all_chunks(_GUTENBERG_ROOT)
    prose_variants = build_realistic_document_variants(chunks)
    prose_base_mh = {c.chunk_id: compute_document_minhash(Path(c.chunk_id), c.text) for c in chunks}
    prose_base_emb = {
        c.chunk_id: compute_document_embedding(Path(c.chunk_id), c.text) for c in chunks
    }
    prose_variant_records = [
        (
            v.chunk_id,
            v.profile,
            v.tier,
            compute_document_minhash(Path(f"{v.chunk_id}__{v.profile}"), v.text),
            compute_document_embedding(Path(f"{v.chunk_id}__{v.profile}"), v.text),
        )
        for v in prose_variants
    ]
    prose_chunk_ids = [c.chunk_id for c in chunks]
    prose_negative_ids = [
        (prose_chunk_ids[i], prose_chunk_ids[j])
        for i in range(len(prose_chunk_ids))
        for j in range(i + 1, len(prose_chunk_ids))
    ]
    prose_pos, prose_neg = _score_pairs(
        prose_base_mh, prose_base_emb, prose_variant_records, prose_negative_ids
    )

    # --- Templated tier (this follow-up) ---
    templated_docs = build_templated_documents()
    templated_variants = build_templated_variants(templated_docs)
    templated_base_mh = {
        d.doc_id: compute_document_minhash(Path(d.doc_id), d.text) for d in templated_docs
    }
    templated_base_emb = {
        d.doc_id: compute_document_embedding(Path(d.doc_id), d.text) for d in templated_docs
    }
    templated_variant_records = [
        (
            v.chunk_id,
            v.profile,
            v.tier,
            compute_document_minhash(Path(f"{v.chunk_id}__{v.profile}"), v.text),
            compute_document_embedding(Path(f"{v.chunk_id}__{v.profile}"), v.text),
        )
        for v in templated_variants
    ]
    by_template: dict[str, list[str]] = {}
    for doc in templated_docs:
        by_template.setdefault(doc.template, []).append(doc.doc_id)
    templated_negative_ids = [
        (ids[i], ids[j])
        for ids in by_template.values()
        for i in range(len(ids))
        for j in range(i + 1, len(ids))
    ]
    templated_pos, templated_neg = _score_pairs(
        templated_base_mh, templated_base_emb, templated_variant_records, templated_negative_ids
    )

    assert len(prose_pos) == 360
    assert len(prose_neg) == 7_140
    assert len(templated_pos) == 162
    assert len(templated_neg) == 459

    # --- Honest baseline: today's shipped thresholds (0.2, 0.6) on the templated tier ---
    shipped_fp = sum(1 for j, c in templated_neg if j >= 0.2 and c >= 0.6)
    shipped_tp = sum(1 for j, c in templated_pos if j >= 0.2 and c >= 0.6)
    shipped_precision = shipped_tp / (shipped_tp + shipped_fp) if (shipped_tp + shipped_fp) else 0.0
    print(  # noqa: T201
        f"\n=== Shipped thresholds (0.2, 0.6) on templated tier: "
        f"precision={shipped_precision:.4f} ({shipped_fp} FP / {shipped_tp} TP) — "
        f"this is the real problem this eval exists to catch ==="
    )
    assert shipped_precision < 0.5, (
        "sanity: the whole point of this eval is that today's shipped thresholds fail badly "
        "on templated documents — if this ever passes, the templated fixture stopped "
        "reproducing the boilerplate-collision failure mode and needs investigation"
    )

    # --- Joint grid search via the reusable, ADR-0018-hardened harness primitive ---
    # This eval originally hand-rolled this grid search inline, and its FIRST version pooled
    # prose (7,140 negatives) and templated (459 negatives) into one combined precision
    # calculation -- misleading, because the templated tier's false positives got
    # mathematically swamped by the much larger prose negative pool (a point that looked like
    # 0.9524 precision in aggregate was actually only 0.8634 on the templated tier alone).
    # That incident is now a structural, permanent harness rule (ADR-0018): `eval_harness.
    # select_joint_operating_point_per_tier` has NO code path that pools tiers together --
    # every tier's precision/recall is computed from only that tier's own pairs, always. This
    # eval now calls that shared primitive instead of re-implementing the gate inline, so the
    # fix lives in one tested place, not copy-pasted into every future multi-tier eval.
    grid = [round(0.05 * n, 2) for n in range(2, 20)]  # 0.10 .. 0.95
    cosine_grid = [round(0.80 + 0.005 * n, 3) for n in range(0, 40)]  # 0.800 .. 0.995

    operating_point = select_joint_operating_point_per_tier(
        {"prose": (prose_pos, prose_neg), "templated": (templated_pos, templated_neg)},
        stage1_candidates=grid,
        stage2_candidates=cosine_grid,
        target_precision=_TARGET_PRECISION,
        min_recall=_MIN_RECALL_FLOOR,
        distribution=_COMBINED_DISTRIBUTION,
        source_description=(
            f"prose + templated, gated per-tier (ADR-0018), commit {current_commit_sha()}"
        ),
    )
    assert operating_point is not None, (
        "no joint (minhash, embedding) threshold clears both the precision target and the "
        "recall floor on BOTH the prose and templated tiers independently — a real, "
        "reportable finding about the two-stage pipeline's viability on templated documents, "
        "not a bug to paper over"
    )
    assert_safe_to_promote_to_measured(_COMBINED_DISTRIBUTION)
    t1, t2 = operating_point.stage1_threshold, operating_point.stage2_threshold
    per_tier_report = {
        name: {"precision": metrics.precision, "recall": metrics.recall}
        for name, metrics in operating_point.per_tier.items()
    }

    report = EvalReport(
        metric_name="document_joint_operating_point_minhash_threshold",
        value=t1,
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path=_FIXTURE,
    )
    print(f"\nMEASURED joint operating point (gated per-tier): {report}")  # noqa: T201
    print(f"  embedding_threshold={t2:.4f}")  # noqa: T201
    for label, metrics in per_tier_report.items():
        print(  # noqa: T201
            f"  tier={label}: precision={metrics['precision']:.4f} recall={metrics['recall']:.4f}"
        )
        # Redundant with select_joint_operating_point_per_tier's own qualification check, but
        # asserted again explicitly here (spec discipline: the CI gate must fail loudly if
        # this regresses, not just silently accept whatever the harness happened to return).
        assert metrics["precision"] >= _TARGET_PRECISION
        assert metrics["recall"] >= _MIN_RECALL_FLOOR

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": _FIXTURE,
                "shipped_thresholds_templated_precision_baseline": shipped_precision,
                "joint_operating_point": {"minhash_threshold": t1, "embedding_threshold": t2},
                "per_tier": per_tier_report,
                "note": (
                    "Replaces the prior (0.2, 0.6) thresholds -- those were measured on prose "
                    "only and produce a "
                    f"{shipped_precision:.1%} precision catastrophe on templated documents. "
                    "See ADR-0017's follow-up section for the full writeup."
                ),
            },
            indent=2,
        )
    )
    print(f"\nFull report: {_REPORT_PATH}")  # noqa: T201
