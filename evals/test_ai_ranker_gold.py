from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")

import numpy as np

from reclaim.ai.clutter_ranker import (
    CATEGORICAL_FEATURE_INDICES,
    DEFAULT_MODEL_PATH,
    FEATURE_NAMES,
    ClutterRanker,
    extract_numeric_features,
)
from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    UnsafeMeasuredPromotionError,
    assert_safe_to_promote_to_measured,
    cohens_kappa,
    current_commit_sha,
    fleiss_kappa,
    ndcg_at_k,
    precision_at_k,
)
from reclaim.ai.feedback_store import ClusterStats, FeatureVector, SiblingDecisionContext

# Feature: generic clutter-likelihood ranker (ADR-0021). The FAST, repeatable measurement —
# reads the cached output of evals/ai_fixtures/label_ranker_fixtures.py's real cross-LLM
# labeling pass (a ~1.75-hour one-time run, see that file), never re-calls Ollama itself. Same
# "slow acquisition, fast eval" split as evals/test_ai_document_gold.py reading Gutenberg /
# evals/test_ai_copydays_gold.py reading Copydays.
#
# NAMING DISCIPLINE (GG's explicit instruction, repeated here since this file is where the
# gate lives): "generic clutter-likelihood ranker," never "learns your preferences." Every
# label this eval trains/evaluates against is LLM CONSENSUS on a knowable, non-personal
# property — NOT any real person's actual decisions. This measurement is gated on BOTH a
# ranking-quality floor AND the explicit disclosure that its labels are LLM-consensus, not
# human ground truth (`_DISTRIBUTION.is_synthetic_only=True`, `assert_safe_to_promote_to_
# measured` is asserted to RAISE, never called as if it could pass).

_LABELED_RECORDS_PATH = Path("data/ai_datasets/ranker_labels/labeled_records.jsonl")
_JUDGE_MODELS = ("qwen3:8b", "llama3.1:8b", "gemma2:9b")
_NDCG_AT_K = 5
_NDCG_FLOOR = 0.70
_PRECISION_AT_K = 3
_PRECISION_RELEVANCE_FLOOR = 3  # top-k items must grade >= 3 (probable-or-definite clutter)
_PRECISION_FLOOR = 0.50
_TRAIN_BATCH_FRACTION = 0.75  # grouped split (ADR-0021): whole batches, never split within
_REPORT_PATH = Path("reports/ai/ranker_operating_point.json")
_COMMAND = "uv run pytest evals/test_ai_ranker_gold.py -v -s"

_DISTRIBUTION = DistributionDeclaration(
    description=(
        "120 synthetic-but-realistic file-record metadata records (15 archetype generators "
        "spanning clutter/important/ambiguous kinds), labeled by 3 independent LOCAL LLM "
        "judges (qwen3:8b, llama3.1:8b, gemma2:9b via Ollama, zero paid API) on a fixed 0-4 "
        "generic clutter-likelihood rubric. Trained/evaluated only on records where all 3 "
        "judges unanimously agreed."
    ),
    is_realistic=False,
    is_adversarial_tail_only=False,
    is_synthetic_only=True,  # both the file records AND the labels are synthetic/LLM-
    # generated -- there is no real human or real personal-decision data anywhere in this
    # measurement. Permanently PROVISIONAL, never promoted to MEASURED (see the assertion
    # below that assert_safe_to_promote_to_measured actually raises for this declaration).
    untested_variation_note=(
        "No real user, no real file, no real deletion decision anywhere in this dataset. "
        "Real extensions/paths/categories never seen in the 15 archetype generators are "
        "not covered by training, though the hash-bucketed categorical encoding degrades "
        "gracefully to an unseen-but-valid bucket rather than failing. The LLM judges' own "
        "biases/blind spots are not independently audited beyond the inter-rater agreement "
        "measured here — three LLMs agreeing is evidence of a knowable property, not proof "
        "the rubric itself is correct."
    ),
)

pytestmark = pytest.mark.skipif(
    not _LABELED_RECORDS_PATH.exists(),
    reason=(
        "No cached cross-LLM ranker labels — run "
        "`uv run python evals/ai_fixtures/label_ranker_fixtures.py` first "
        "(~1.75 hours, real local Ollama calls)."
    ),
)


def _load_labeled_records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with _LABELED_RECORDS_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _row_to_feature_vector(row: dict[str, Any]) -> FeatureVector:
    record = row["record"]
    cs = record["cluster_stats"]
    return FeatureVector(
        size_bytes=record["size_bytes"],
        ext=record["ext"],
        path_class=record["path_class"],
        mtime=record["mtime"],
        ctime=record["ctime"],
        cluster_stats=ClusterStats(
            cluster_size=cs["cluster_size"],
            position_in_cluster=cs["position_in_cluster"],
            raw_score=cs["raw_score"],
            score_kind=cs["score_kind"],
            is_recommended_keep=cs["is_recommended_keep"],
        ),
        category=record["category"],
        cloud_sync_flag=record["cloud_sync_flag"],
        sibling_decision_context=SiblingDecisionContext(0, 0, 0),  # synthetic: no real
        # decision history exists yet for any of these records.
    )


def test_cross_llm_agreement_and_ranker_operating_point() -> None:
    lightgbm = pytest.importorskip("lightgbm")

    rows = _load_labeled_records()
    assert len(rows) > 50, f"expected a substantial labeled set, got {len(rows)}"

    complete_rows = [row for row in rows if set(row["grades"].keys()) == set(_JUDGE_MODELS)]
    print(  # noqa: T201
        f"\n{len(complete_rows)}/{len(rows)} records got a grade from all "
        f"{len(_JUDGE_MODELS)} judges."
    )

    # --- Inter-rater agreement (ADR-0021's core measurement) -----------------------------
    item_ratings = [[row["grades"][model] for model in _JUDGE_MODELS] for row in complete_rows]
    kappa = fleiss_kappa(item_ratings)
    print(f"Fleiss' kappa (3 raters, {len(complete_rows)} items): {kappa:.4f}")  # noqa: T201

    pairwise_kappas: dict[str, float] = {}
    for i in range(len(_JUDGE_MODELS)):
        for j in range(i + 1, len(_JUDGE_MODELS)):
            model_a, model_b = _JUDGE_MODELS[i], _JUDGE_MODELS[j]
            ratings_a = [row["grades"][model_a] for row in complete_rows]
            ratings_b = [row["grades"][model_b] for row in complete_rows]
            pair_kappa = cohens_kappa(ratings_a, ratings_b)
            pairwise_kappas[f"{model_a}_vs_{model_b}"] = pair_kappa
            print(f"  Cohen's kappa {model_a} vs {model_b}: {pair_kappa:.4f}")  # noqa: T201

    # --- Exclusion: unanimous agreement only, disagreement EXCLUDED not majority-voted ---
    unanimous_rows = [row for row in complete_rows if len(set(row["grades"].values())) == 1]
    excluded_count = len(complete_rows) - len(unanimous_rows)
    exclusion_rate = excluded_count / len(complete_rows) if complete_rows else 0.0
    print(  # noqa: T201
        f"{len(unanimous_rows)}/{len(complete_rows)} records had UNANIMOUS agreement "
        f"({excluded_count} excluded, {exclusion_rate:.1%} exclusion rate) -- only these "
        "are used for training/eval."
    )
    assert len(unanimous_rows) >= 20, (
        f"only {len(unanimous_rows)} unanimous-agreement records -- too few to train/eval "
        "a meaningful grouped split on"
    )

    # --- Grouped train/eval split: whole batches, never split within one (no leakage) ----
    batch_ids = sorted({row["record"]["batch_id"] for row in unanimous_rows})
    split_index = max(1, int(len(batch_ids) * _TRAIN_BATCH_FRACTION))
    train_batch_ids = set(batch_ids[:split_index])
    eval_batch_ids = set(batch_ids[split_index:])
    assert train_batch_ids and eval_batch_ids, "grouped split produced an empty train or eval side"

    train_rows = [r for r in unanimous_rows if r["record"]["batch_id"] in train_batch_ids]
    eval_rows = [r for r in unanimous_rows if r["record"]["batch_id"] in eval_batch_ids]
    print(  # noqa: T201
        f"Grouped split: {len(train_batch_ids)} train batches ({len(train_rows)} records), "
        f"{len(eval_batch_ids)} eval batches ({len(eval_rows)} records)."
    )

    now = max(row["record"]["batch_generated_at"] for row in rows) + 86400.0

    def _unanimous_grade(row: dict[str, Any]) -> int:
        return int(next(iter(row["grades"].values())))

    # Sort explicitly by batch_id first -- LightGBM's `group` param needs CONTIGUOUS
    # run-lengths matching row order; sorting here makes that a guaranteed property of this
    # function, not an assumption about upstream ordering that a future refactor could
    # silently break.
    train_rows = sorted(train_rows, key=lambda r: r["record"]["batch_id"])

    train_features = np.array(
        [extract_numeric_features(_row_to_feature_vector(row), now=now) for row in train_rows]
    )
    train_labels = [_unanimous_grade(row) for row in train_rows]
    train_group_sizes = _group_sizes_by_batch(train_rows)

    dataset = lightgbm.Dataset(
        train_features,
        label=train_labels,
        group=train_group_sizes,
        categorical_feature=list(CATEGORICAL_FEATURE_INDICES),
        feature_name=list(FEATURE_NAMES),
    )
    booster = lightgbm.train(
        {
            "objective": "lambdarank",
            "metric": "ndcg",
            "verbosity": -1,
            "min_data_in_leaf": 3,
            "num_leaves": 15,
        },
        dataset,
        num_boost_round=100,
    )

    DEFAULT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(DEFAULT_MODEL_PATH))
    ranker = ClutterRanker(model_path=DEFAULT_MODEL_PATH)

    # --- Ranking-quality measurement on the HELD-OUT eval batches -------------------------
    ndcg_scores: list[float] = []
    precision_scores: list[float] = []
    for batch_id in sorted(eval_batch_ids):
        batch_rows = [r for r in eval_rows if r["record"]["batch_id"] == batch_id]
        if len(batch_rows) < 2:
            continue
        feature_vectors = [_row_to_feature_vector(r) for r in batch_rows]
        true_grades = [_unanimous_grade(r) for r in batch_rows]
        ranked = ranker.rank(feature_vectors, now=now)
        predicted_order_grades = [float(true_grades[index]) for index, _score in ranked]
        ndcg_scores.append(ndcg_at_k(predicted_order_grades, min(_NDCG_AT_K, len(batch_rows))))
        precision_scores.append(
            precision_at_k(
                predicted_order_grades,
                min(_PRECISION_AT_K, len(batch_rows)),
                relevance_floor=_PRECISION_RELEVANCE_FLOOR,
            )
        )

    assert ndcg_scores, "no eval batch had >= 2 unanimous-agreement records to rank"
    mean_ndcg = sum(ndcg_scores) / len(ndcg_scores)
    mean_precision = sum(precision_scores) / len(precision_scores)
    print(  # noqa: T201
        f"\nMean NDCG@{_NDCG_AT_K} across {len(ndcg_scores)} eval batches: {mean_ndcg:.4f} "
        f"(floor {_NDCG_FLOOR})"
    )
    print(  # noqa: T201
        f"Mean precision@{_PRECISION_AT_K} (relevance>={_PRECISION_RELEVANCE_FLOOR}) across "
        f"{len(precision_scores)} eval batches: {mean_precision:.4f} (floor {_PRECISION_FLOOR})"
    )

    # --- The honest disclosure gate: this can NEVER be promoted to MEASURED -- LLM-consensus
    # labels on synthetic records are not human ground truth, structurally enforced, not just
    # narrated. ---
    with pytest.raises(UnsafeMeasuredPromotionError, match="synthetic-only"):
        assert_safe_to_promote_to_measured(_DISTRIBUTION)

    assert mean_ndcg >= _NDCG_FLOOR, (
        f"mean NDCG@{_NDCG_AT_K}={mean_ndcg:.4f} is below the ranking-quality floor "
        f"{_NDCG_FLOOR} -- a real, reportable finding about the ranker's quality on this "
        "LLM-consensus-labeled distribution, not a bug to paper over"
    )
    assert mean_precision >= _PRECISION_FLOOR, (
        f"mean precision@{_PRECISION_AT_K}={mean_precision:.4f} is below the floor "
        f"{_PRECISION_FLOOR}"
    )

    report = EvalReport(
        metric_name="ranker_ndcg_at_5",
        value=mean_ndcg,
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path=str(_LABELED_RECORDS_PATH),
    )
    print(f"\n{report}")  # noqa: T201

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(
        json.dumps(
            {
                "commit_sha": current_commit_sha(),
                "command": _COMMAND,
                "fixture_path": str(_LABELED_RECORDS_PATH),
                "judge_models": list(_JUDGE_MODELS),
                "fleiss_kappa_3_raters": kappa,
                "pairwise_cohens_kappa": pairwise_kappas,
                "complete_records": len(complete_rows),
                "unanimous_agreement_records": len(unanimous_rows),
                "excluded_disagreement_records": excluded_count,
                "exclusion_rate": exclusion_rate,
                "train_batches": len(train_batch_ids),
                "eval_batches": len(eval_batch_ids),
                "mean_ndcg_at_5": mean_ndcg,
                "mean_precision_at_3": mean_precision,
                "note": (
                    "GENERIC clutter-likelihood ranker -- labels are LLM CONSENSUS on a "
                    "knowable, non-personal property, NOT human ground truth and NOT any "
                    "real person's actual decisions. Permanently PROVISIONAL "
                    "(is_synthetic_only=True) -- assert_safe_to_promote_to_measured raises "
                    "for this distribution, verified by this eval, not just narrated."
                ),
            },
            indent=2,
        )
    )
    print(f"\nFull report: {_REPORT_PATH}")  # noqa: T201


def _group_sizes_by_batch(rows: list[dict[str, Any]]) -> list[int]:
    """LightGBM's `group` param needs CONTIGUOUS run-lengths matching the row order — the
    caller is responsible for having already sorted `rows` by batch_id (the one call site
    above does this explicitly, not relying on upstream ordering)."""
    sizes: list[int] = []
    current_batch: str | None = None
    for row in rows:
        batch_id = row["record"]["batch_id"]
        if batch_id != current_batch:
            sizes.append(0)
            current_batch = batch_id
        sizes[-1] += 1
    return sizes
