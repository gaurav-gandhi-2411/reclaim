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
)
from reclaim.ai.minhash_lsh import compute_document_minhash, jaccard_similarity
from reclaim.ai.text_embeddings import compute_document_embedding, cosine_similarity

# ADR-0017 follow-up: GG flagged that measuring MinHash's operating point against edited
# Gutenberg prose can't surface the failure mode real document clutter actually has — resumes,
# invoices, reports, and decks are heavily TEMPLATED, so genuinely DIFFERENT documents can
# share large blocks of identical boilerplate. This eval adds that tier and grid-searches the
# JOINT (MinHash Jaccard, embedding cosine) operating point, gated on BOTH tiers
# INDEPENDENTLY — a first version pooled prose (7,140 negatives) and templated (459
# negatives) into one aggregate precision via `eval_harness.select_joint_operating_point`
# (kept in eval_harness.py as a reusable single-distribution primitive, unit-tested, just not
# the right tool here), and that pooled number was itself misleading: the templated tier's
# false positives got mathematically swamped by the much larger prose negative pool. This file
# grid-searches directly instead, requiring both tiers to independently clear the precision
# and recall floors before a candidate threshold pair even qualifies.
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

    # --- Joint grid search, gated on BOTH tiers INDEPENDENTLY, not the pooled aggregate ---
    # A first version of this eval pooled prose (7,140 negatives) and templated (459
    # negatives) into one combined precision calculation via select_joint_operating_point --
    # and that pooled aggregate was itself misleading: the templated tier's false positives
    # got mathematically swamped by the much larger prose negative pool, so a point that
    # looked like it cleared 0.95 precision in aggregate (0.9524) actually had only 0.8634
    # precision on the templated tier alone. This is caught here, deliberately, by gating each
    # tier independently in the grid search itself -- exactly the discipline GG asked for
    # ("gate on precision AND recall for both tiers"), not just at the end as a check on
    # whatever the aggregate search happened to produce.
    grid = [round(0.05 * n, 2) for n in range(2, 20)]  # 0.10 .. 0.95
    cosine_grid = [round(0.80 + 0.005 * n, 3) for n in range(0, 40)]  # 0.800 .. 0.995
    tiers = (("prose", prose_pos, prose_neg), ("templated", templated_pos, templated_neg))

    best: tuple[float, float, dict[str, dict[str, float]]] | None = None
    for t1 in grid:
        for t2 in cosine_grid:
            per_tier: dict[str, dict[str, float]] = {}
            qualifies = True
            for label, pos, neg in tiers:
                tp = sum(1 for j, c in pos if j >= t1 and c >= t2)
                fp = sum(1 for j, c in neg if j >= t1 and c >= t2)
                precision = tp / (tp + fp) if (tp + fp) else 0.0
                recall = tp / len(pos)
                per_tier[label] = {"precision": precision, "recall": recall}
                if precision < _TARGET_PRECISION or recall < _MIN_RECALL_FLOOR:
                    qualifies = False
            if not qualifies:
                continue
            min_recall_across_tiers = min(m["recall"] for m in per_tier.values())
            if best is None or min_recall_across_tiers > min(m["recall"] for m in best[2].values()):
                best = (t1, t2, per_tier)

    assert best is not None, (
        "no joint (minhash, embedding) threshold clears both the precision target and the "
        "recall floor on BOTH the prose and templated tiers independently — a real, "
        "reportable finding about the two-stage pipeline's viability on templated documents, "
        "not a bug to paper over"
    )
    assert_safe_to_promote_to_measured(_COMBINED_DISTRIBUTION)
    t1, t2, per_tier_report = best

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
        # Redundant with the grid search's own qualification check above, but asserted again
        # explicitly here (spec discipline: the CI gate must fail loudly if this regresses,
        # not just silently accept whatever the grid search happened to return).
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
