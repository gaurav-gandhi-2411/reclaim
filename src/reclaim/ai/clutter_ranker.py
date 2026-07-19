from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import blake3

from reclaim.ai._optional import require
from reclaim.ai.feedback_store import FeatureVector
from reclaim.ai.models import AICluster, AIClusterMember, AITrack

# Feature: generic clutter-likelihood ranker (ADR-0021). Recommend-only: PRIORITIZES the
# AIReviewQueue by predicted clutter-likelihood so likely-clutter items surface first,
# reducing review effort — it NEVER auto-deletes and NEVER reaches `apply_batch`
# (evals/test_ai_safety_gate.py's AST scan covers this module automatically, same structural
# guarantee every other reclaim.ai module has).
#
# NAMING DISCIPLINE (GG's explicit instruction): "generic clutter-likelihood ranker,"
# EVERYWHERE — code, UI copy, ADRs, case study. Never "learns your preferences" or "predicts
# what you'll delete." This model is trained on LLM-CONSENSUS labels of a KNOWABLE,
# NON-PERSONAL property (is this file the KIND of thing usually safe to flag), not on any
# real person's actual decisions — see ADR-0021's dataset section. The (separate, currently
# unbuilt) PERSONAL re-ranking layer that learns from real accept/reject/keep decisions
# (Feature 3's feedback_store.py) is a documented FUTURE upgrade gated at >= 500 real
# decisions, never trained on this model's synthetic/LLM-consensus data.
#
# Reuses `feedback_store.FeatureVector` as the canonical feature INPUT type — the same schema
# Feature 3 already established (size, ext, path_class, mtime/ctime, cluster stats, category,
# cloud_sync_flag, sibling_decision_context) — rather than inventing a parallel one. Training
# (from synthetic `ai_fixtures.build_ranker_fixtures.RankerFileRecord`, converted once to a
# `FeatureVector` with zeroed sibling-decision-context — synthetic records have no real
# decision history) and real-runtime scoring (from a `FeatureVector` built the same way
# `feedback_store.record_feedback_decision` already builds one) both go through the EXACT
# SAME `extract_numeric_features` function below — eliminating training-serving skew by
# construction, not by convention.

DEFAULT_MODEL_PATH = Path("data/ai_models/clutter_ranker.txt")

FEATURE_NAMES: tuple[str, ...] = (
    "log_size_bytes",
    "age_days",
    "ctime_age_days",
    "path_class_bucket",
    "category_bucket",
    "ext_bucket",
    "cluster_size",
    "is_recommended_keep",
    "cluster_raw_score",
    "cloud_sync_flag",
    "prior_accepted",
    "prior_rejected",
    "prior_kept",
)
# LightGBM column INDICES (not names) that must be passed as `categorical_feature` — the
# three string fields, stably hash-bucketed (see `_hash_bucket`) rather than encoded against
# a hardcoded vocabulary, so a real extension/category/path-class never seen in the training
# fixture still gets a deterministic, valid bucket instead of an unknown-category failure.
CATEGORICAL_FEATURE_INDICES: tuple[int, ...] = (3, 4, 5)

_HASH_BUCKET_COUNT = 256


def _hash_bucket(value: str, *, num_buckets: int = _HASH_BUCKET_COUNT) -> int:
    """Deterministic across processes/runs (unlike Python's built-in `hash()`, which is
    salted per-process for strings) — reuses `blake3`, already a core dependency, rather than
    adding a new one just for this."""
    digest = blake3.blake3(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % num_buckets


def extract_numeric_features(feature_vector: FeatureVector, *, now: float) -> list[float]:
    """The single canonical encoding function — see module docstring for why training and
    real-runtime scoring must both go through exactly this, never a reimplementation."""
    cluster = feature_vector.cluster_stats
    sibling = feature_vector.sibling_decision_context
    age_days = max(0.0, (now - feature_vector.mtime) / 86400.0)
    ctime_age_days = max(0.0, (now - feature_vector.ctime) / 86400.0)

    return [
        math.log1p(max(feature_vector.size_bytes, 0)),
        age_days,
        ctime_age_days,
        float(_hash_bucket(feature_vector.path_class)),
        float(_hash_bucket(feature_vector.category)),
        float(_hash_bucket(feature_vector.ext)),
        float(cluster.cluster_size),
        1.0 if cluster.is_recommended_keep else 0.0,
        cluster.raw_score,
        1.0 if feature_vector.cloud_sync_flag else 0.0,
        float(sibling.prior_accepted),
        float(sibling.prior_rejected),
        float(sibling.prior_kept),
    ]


@dataclass(frozen=True, slots=True)
class ClutterLikelihoodScore:
    """Every result carries `is_generic` hardcoded `True` — structurally documenting that
    this score reflects the generic (knowable-from-metadata) clutter-likelihood property,
    never a personal prediction, mirroring `cold_start_priority.ColdStartPriority.
    is_heuristic`'s "make the honesty claim checkable, not just narrated" pattern."""

    raw_score: float  # the LightGBM model's raw predicted relevance -- higher = more
    # likely to be generic clutter. NOT a probability (spec §0.6): never presented as a
    # calibrated confidence percentage anywhere this is surfaced.
    is_generic: bool = True


class ClutterRanker:
    """Loads a trained LightGBM LambdaMART model (native text-format booster) and scores
    `FeatureVector`s by generic clutter-likelihood. Recommend-only: this class has no method
    that deletes, moves, or mutates anything — `score`/`rank` are pure functions over
    in-memory data."""

    def __init__(self, model_path: Path = DEFAULT_MODEL_PATH) -> None:
        lightgbm = require("lightgbm", feature="generic clutter-likelihood ranking")
        if not model_path.exists():
            raise FileNotFoundError(
                f"no trained clutter-ranker model at {model_path} -- run "
                "evals/ai_fixtures/label_ranker_fixtures.py then the training eval "
                "(ADR-0021) to produce one, or point ClutterRanker at an existing artifact"
            )
        self._np = require("numpy", feature="generic clutter-likelihood ranking")
        self._booster = lightgbm.Booster(model_file=str(model_path))

    def score(self, feature_vector: FeatureVector, *, now: float) -> ClutterLikelihoodScore:
        features = self._np.array([extract_numeric_features(feature_vector, now=now)])
        raw = self._booster.predict(features)[0]
        return ClutterLikelihoodScore(raw_score=float(raw))

    def rank(
        self, feature_vectors: list[FeatureVector], *, now: float
    ) -> list[tuple[int, ClutterLikelihoodScore]]:
        """Returns `(original_index, score)` pairs sorted by DESCENDING clutter-likelihood
        (most-likely-clutter first) — the actual "prioritize the review queue" operation.
        Batches the whole list through one `predict` call rather than one per item."""
        if not feature_vectors:
            return []
        features = self._np.array([extract_numeric_features(fv, now=now) for fv in feature_vectors])
        raw_scores = self._booster.predict(features)
        scored = [
            (index, ClutterLikelihoodScore(raw_score=float(raw)))
            for index, raw in enumerate(raw_scores)
        ]
        return sorted(scored, key=lambda pair: pair[1].raw_score, reverse=True)


def build_ranked_clutter_entries(
    paths: list[Path], feature_vectors: list[FeatureVector], ranker: ClutterRanker, *, now: float
) -> list[AICluster]:
    """Wraps each `(path, feature_vector)` pair into its own singleton `AICluster` on
    `AITrack.RANKED_CLUTTER`, `raw_score` = the ranker's predicted clutter-likelihood —
    `AIReviewQueue.browse_only()` already returns these; a caller sorts by `raw_score`
    descending to get the actual "likely clutter first" ordering.

    SAFETY: `is_recommended_keep` is never set on any member here, and never could be even by
    a future bug — `AITrack.RANKED_CLUTTER` is not in `_DELETION_SUGGESTION_ELIGIBLE_TRACKS`
    (models.py), so `AICluster.__post_init__` raises `ValueError` on construction if any
    member ever carried that flag. This function relies on that existing structural guard
    rather than re-implementing the check — see `evals/test_ai_safety_gate.py` for the proof.
    """
    if len(paths) != len(feature_vectors):
        raise ValueError(
            f"paths and feature_vectors must be the same length — got {len(paths)} vs "
            f"{len(feature_vectors)}"
        )
    entries: list[AICluster] = []
    for index, (path, feature_vector) in enumerate(zip(paths, feature_vectors, strict=True)):
        score = ranker.score(feature_vector, now=now)
        entries.append(
            AICluster(
                cluster_id=f"ranked-clutter-{index}",
                track=AITrack.RANKED_CLUTTER,
                members=(AIClusterMember(path=path, size_bytes=feature_vector.size_bytes),),
                raw_score=score.raw_score,
                score_kind="clutter_likelihood_lambdamart",
                rationale=(
                    "Generic clutter-likelihood ranker score (LLM-consensus-trained, NOT a "
                    "personal preference prediction) — prioritizes review order only, never "
                    "a deletion suggestion on its own."
                ),
            )
        )
    return entries
