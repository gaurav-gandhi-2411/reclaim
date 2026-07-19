from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_fixtures.build_document_realistic_tiers import (
    build_all_chunks,
    build_realistic_document_variants,
)

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    assert_safe_to_promote_to_measured,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)
from reclaim.ai.minhash_lsh import compute_document_minhash, jaccard_similarity
from reclaim.ai.text_embeddings import compute_document_embedding, cosine_similarity

# Feature 1b's REAL operating-point measurement (ADR-0017) — the realistic distribution,
# applying the ADR-0016 gate-hardening lesson from the start (never measure only the
# adversarial/synthetic case and call it MEASURED). Real content: 8 public-domain Gutenberg
# books (fetch_gutenberg_texts.py), chunked into document-length pieces, each with 3
# deterministic realistic transform profiles applied (build_document_realistic_tiers.py) — no
# fabricated sentences, no synthetic-only data backing the MEASURED claim.
#
# Not in the default CI sweep (network + multi-minute embedding compute) — same posture as
# evals/test_ai_copydays_gold.py. Reproduce with:
#   uv run python evals/ai_fixtures/fetch_gutenberg_texts.py
#   uv run pytest evals/test_ai_document_gold.py -v -s

_GUTENBERG_ROOT = Path("data/ai_datasets/gutenberg_texts/cleaned")
_TARGET_PRECISION = 0.95  # spec §7.3: near-identical/deletion-suggestion tracks >= 0.95
_MIN_RECALL_FLOOR = 0.5  # ADR-0016 policy default for this track
_COMMAND = "uv run pytest evals/test_ai_document_gold.py -v -s"
_FIXTURE = (
    "data/ai_datasets/gutenberg_texts (8 public-domain books, ADR-0017) + "
    "evals/ai_fixtures/build_document_realistic_tiers.py"
)
_REPORT_PATH = Path("reports/ai/document_realistic_distribution_pr_curve.json")
_REALISTIC_DISTRIBUTION = DistributionDeclaration(
    description=(
        "3 deterministic transform profiles (mild whitespace cleanup, moderate paragraph "
        "trim+reorder, collab-tool-paste flattening+truncation) applied to 120 real "
        "document-length chunks from 8 public-domain Gutenberg books"
    ),
    is_realistic=True,
    is_adversarial_tail_only=False,
    is_synthetic_only=False,
    untested_variation_note=(
        "does not cover: format-conversion artifacts from real docx/pdf extraction (hyphen-"
        "break, header/footer injection), multi-generation edit chains beyond one transform, "
        "translation/back-translation paraphrase, heavy manual rewrite, or documents shorter "
        "than ~300 words — see ADR-0017's PAWS-Wiki cross-check for a real, human/machine-"
        "labeled boundary tier on short-text paraphrase specifically (reported separately, "
        "never the basis for this measurement's MEASURED status)"
    ),
)

pytestmark = pytest.mark.skipif(
    not _GUTENBERG_ROOT.exists() or not any(_GUTENBERG_ROOT.glob("*.txt")),
    reason=(
        "Gutenberg texts not present locally — run "
        "`uv run python evals/ai_fixtures/fetch_gutenberg_texts.py` first. Not auto-downloaded "
        "by this test or by CI (see module docstring)."
    ),
)


def test_minhash_realistic_pr_curve_and_per_tier_recall() -> None:
    chunks = build_all_chunks(_GUTENBERG_ROOT)
    assert len(chunks) == 120, f"expected 120 chunks (8 books x 15), got {len(chunks)}"
    variants = build_realistic_document_variants(chunks)
    assert len(variants) == 360

    minhash_by_id = {}
    for chunk in chunks:
        record = compute_document_minhash(Path(chunk.chunk_id), chunk.text)
        assert record is not None, f"real Gutenberg chunk failed to shingle: {chunk.chunk_id}"
        minhash_by_id[chunk.chunk_id] = record

    variant_minhash: dict[tuple[str, str], object] = {}
    for variant in variants:
        record = compute_document_minhash(
            Path(f"{variant.chunk_id}__{variant.profile}"), variant.text
        )
        assert record is not None, (
            f"variant failed to shingle: {variant.chunk_id}/{variant.profile}"
        )
        variant_minhash[(variant.chunk_id, variant.profile)] = record

    # Positives: each variant vs its own base chunk. Negatives: base-chunk-vs-base-chunk
    # across DIFFERENT chunks only — a clean, untainted "two different real documents" pool
    # (same reasoning as Copydays' original-vs-original negatives in ADR-0012's follow-up).
    tier_pairs: dict[str, list[float]] = {"mild": [], "moderate": [], "collab_paste": []}
    scored: list[tuple[float, bool]] = []
    for variant in variants:
        base_record = minhash_by_id[variant.chunk_id]
        variant_record = variant_minhash[(variant.chunk_id, variant.profile)]
        similarity = jaccard_similarity(base_record, variant_record)
        tier_pairs[variant.tier].append(similarity)
        scored.append((similarity, True))

    chunk_ids = [chunk.chunk_id for chunk in chunks]
    negative_count = 0
    for i in range(len(chunk_ids)):
        for j in range(i + 1, len(chunk_ids)):
            similarity = jaccard_similarity(
                minhash_by_id[chunk_ids[i]], minhash_by_id[chunk_ids[j]]
            )
            scored.append((similarity, False))
            negative_count += 1
    assert negative_count == 7_140  # C(120, 2)

    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)
    operating_point = select_operating_point(
        curve,
        target_precision=_TARGET_PRECISION,
        min_recall=_MIN_RECALL_FLOOR,
        distribution=_REALISTIC_DISTRIBUTION,
        source_description=(
            f"Gutenberg realistic distribution (ADR-0017), commit {current_commit_sha()}"
        ),
    )

    print(f"\n=== Document MinHash realistic PR curve ({len(scored)} pairs, 360 positive) ===")  # noqa: T201
    for tier, similarities in tier_pairs.items():
        recall_at_03 = sum(1 for s in similarities if s >= 0.3) / len(similarities)
        recall_at_05 = sum(1 for s in similarities if s >= 0.5) / len(similarities)
        print(  # noqa: T201
            f"  tier={tier:14s} min={min(similarities):.4f} max={max(similarities):.4f} "
            f"recall@0.3={recall_at_03:.4f} recall@0.5={recall_at_05:.4f}"
        )

    assert operating_point is not None, (
        f"no MinHash Jaccard threshold clears both precision {_TARGET_PRECISION} and recall "
        f"{_MIN_RECALL_FLOOR} on the realistic document distribution — a real, reportable "
        "finding about the MinHash pipeline's viability for Feature 1b"
    )
    assert_safe_to_promote_to_measured(_REALISTIC_DISTRIBUTION)

    report = EvalReport(
        metric_name="document_minhash_operating_point_jaccard_threshold",
        value=operating_point.threshold,
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path=_FIXTURE,
    )
    print(f"\nMEASURED operating point: {report}")  # noqa: T201
    print(f"  precision={operating_point.precision:.4f}  recall={operating_point.recall:.4f}")  # noqa: T201

    per_tier_recall_at_threshold = {
        tier: sum(1 for s in similarities if s >= operating_point.threshold) / len(similarities)
        for tier, similarities in tier_pairs.items()
    }
    for tier, recall in per_tier_recall_at_threshold.items():
        print(f"  tier={tier}: recall at chosen threshold = {recall:.4f}")  # noqa: T201

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": _FIXTURE,
                "distribution": {
                    "description": _REALISTIC_DISTRIBUTION.description,
                    "is_realistic": _REALISTIC_DISTRIBUTION.is_realistic,
                    "untested_variation_note": _REALISTIC_DISTRIBUTION.untested_variation_note,
                },
                "operating_point": {
                    "jaccard_threshold": operating_point.threshold,
                    "precision": operating_point.precision,
                    "recall": operating_point.recall,
                },
                "per_tier_recall_at_threshold": per_tier_recall_at_threshold,
                "min_recall_floor": _MIN_RECALL_FLOOR,
                "target_precision": _TARGET_PRECISION,
            },
            indent=2,
        )
    )
    print(f"\nFull report: {_REPORT_PATH}")  # noqa: T201


def test_embedding_realistic_recall_at_operating_point() -> None:
    """Sentence-embedding (Stage 2) cosine-similarity check on the SAME realistic
    distribution — confirms the residual-confirmation stage doesn't reject documents Stage 1
    already correctly identified. Not a full independent PR curve (that would mean running
    the (expensive) embedding model over the full negative pool too); this test measures
    recall on the known positives only, which is what Stage 2 needs to preserve."""
    chunks = build_all_chunks(_GUTENBERG_ROOT)
    variants = build_realistic_document_variants(chunks)

    base_embeddings = {
        chunk.chunk_id: compute_document_embedding(Path(chunk.chunk_id), chunk.text)
        for chunk in chunks
    }
    assert all(e is not None for e in base_embeddings.values())

    tier_similarities: dict[str, list[float]] = {"mild": [], "moderate": [], "collab_paste": []}
    for variant in variants:
        variant_embedding = compute_document_embedding(
            Path(f"{variant.chunk_id}__{variant.profile}"), variant.text
        )
        assert variant_embedding is not None
        similarity = cosine_similarity(base_embeddings[variant.chunk_id], variant_embedding)
        tier_similarities[variant.tier].append(similarity)

    print("\n=== Document embedding cosine similarity on known-positive pairs ===")  # noqa: T201
    for tier, similarities in tier_similarities.items():
        avg = sum(similarities) / len(similarities)
        print(f"  tier={tier:14s} min={min(similarities):.4f} avg={avg:.4f}")  # noqa: T201
        # Every realistic-tier positive pair should remain highly similar under embeddings —
        # Stage 2 confirming a Stage-1-approved cluster should almost never reject it outright.
        assert min(similarities) >= 0.5, (
            f"tier={tier}: a realistic positive pair scored below 0.5 cosine similarity — "
            "Stage 2 would incorrectly split a genuine near-dup cluster"
        )
