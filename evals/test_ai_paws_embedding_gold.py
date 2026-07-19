from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("sentence_transformers")

from ai_fixtures.paws_loader import load_paws_labeled_final

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)
from reclaim.ai.text_embeddings import compute_document_embedding, cosine_similarity

# Secondary, DISCLOSED boundary-tier check for Feature 1b's sentence-embedding (Stage 2)
# cosine-similarity behavior — real, human/machine-labeled Wikipedia sentence pairs (PAWS-Wiki
# "Labeled (Final)" split, ADR-0017). PAWS is explicitly adversarial BY CONSTRUCTION (its
# negatives are word-order-scrambled/back-translated specifically to have high lexical overlap
# with low semantic similarity) — same posture as Copydays' `strong` split in ADR-0012: real,
# honestly reported, but NEVER the sole basis for a MEASURED claim (ADR-0016). The primary
# Stage-1/Stage-2 measurement is evals/test_ai_document_gold.py's realistic Gutenberg
# distribution; this file exists to honestly show how the embedding stage behaves on a genuinely
# hard, real dataset, not to select Feature 1b's shipped threshold.
#
# Not in the default CI sweep (network download + ~16k sentence embeddings). Reproduce with:
#   (parquet files must already be at data/ai_datasets/paws_wiki/ — see ADR-0017)
#   uv run pytest evals/test_ai_paws_embedding_gold.py -v -s

_PAWS_ROOT = Path("data/ai_datasets/paws_wiki")
_COMMAND = "uv run pytest evals/test_ai_paws_embedding_gold.py -v -s"
_FIXTURE = "data/ai_datasets/paws_wiki/test.parquet (PAWS-Wiki Labeled Final, ADR-0017)"
_REPORT_PATH = Path("reports/ai/paws_embedding_pr_curve.json")
_ADVERSARIAL_DISTRIBUTION = DistributionDeclaration(
    description=(
        "PAWS-Wiki Labeled (Final) test split — real Wikipedia sentences, "
        "word-order-scrambled/back-translated negatives constructed to be hard"
    ),
    is_realistic=False,
    is_adversarial_tail_only=True,
    is_synthetic_only=False,
    untested_variation_note=(
        "covers only PAWS's specific adversarial construction (high lexical overlap, low "
        "semantic similarity); says nothing about ordinary document-length near-dup behavior "
        "— see evals/test_ai_document_gold.py for that"
    ),
)

pytestmark = pytest.mark.skipif(
    not _PAWS_ROOT.exists() or not (_PAWS_ROOT / "test.parquet").exists(),
    reason=(
        "PAWS-Wiki parquet files not present locally — see ADR-0017 for the download URLs "
        "(huggingface.co/datasets/google-research-datasets/paws, labeled_final/test split). "
        "Not auto-downloaded by this test or by CI."
    ),
)


def test_embedding_pr_curve_on_paws_adversarial_boundary_tier() -> None:
    pairs = load_paws_labeled_final(_PAWS_ROOT / "test.parquet")
    assert len(pairs) == 8000

    scored: list[tuple[float, bool]] = []
    for pair in pairs:
        embedding_1 = compute_document_embedding(Path(f"paws-{pair.id}-a"), pair.sentence1)
        embedding_2 = compute_document_embedding(Path(f"paws-{pair.id}-b"), pair.sentence2)
        assert embedding_1 is not None
        assert embedding_2 is not None
        similarity = cosine_similarity(embedding_1, embedding_2)
        scored.append((similarity, pair.is_paraphrase))

    n_positive = sum(1 for _, is_paraphrase in scored if is_paraphrase)
    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)

    print(f"\n=== PAWS-Wiki embedding PR curve ({len(scored)} pairs, {n_positive} paraphrase) ===")  # noqa: T201
    results_by_target: dict[str, dict[str, float]] = {}
    for target_precision in (0.95, 0.90, 0.85):
        operating_point = select_operating_point(
            curve,
            target_precision=target_precision,
            min_recall=0.0,  # exploratory/disclosure check, not a shipped gate — see docstring
            distribution=_ADVERSARIAL_DISTRIBUTION,
            source_description=f"PAWS-Wiki (ADR-0017), commit {current_commit_sha()}",
        )
        if operating_point is None:
            print(f"  target precision {target_precision}: NOT achievable on PAWS-Wiki")  # noqa: T201
            results_by_target[str(target_precision)] = {"achievable": False}
            continue
        print(  # noqa: T201
            f"  target precision {target_precision}: cosine>={operating_point.threshold:.4f}  "
            f"recall={operating_point.recall:.4f}"
        )
        results_by_target[str(target_precision)] = {
            "achievable": True,
            "cosine_threshold": operating_point.threshold,
            "recall": operating_point.recall,
        }

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": _FIXTURE,
                "distribution": {
                    "description": _ADVERSARIAL_DISTRIBUTION.description,
                    "is_adversarial_tail_only": True,
                },
                "pr_tradeoff": results_by_target,
                "note": (
                    "Disclosed boundary-tier check only — NEVER the basis for Feature 1b's "
                    "shipped embedding threshold (ADR-0016). See evals/test_ai_document_gold.py "
                    "for the realistic-distribution measurement that actually governs it."
                ),
            },
            indent=2,
        )
    )
    print(f"\nFull report: {_REPORT_PATH}")  # noqa: T201
    print(  # noqa: T201
        "\nThis measurement is reported for disclosure only and intentionally asserts nothing "
        "— PAWS is an adversarial boundary tier, not Feature 1b's operating distribution."
    )
