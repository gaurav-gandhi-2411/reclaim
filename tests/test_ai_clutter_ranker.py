from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.clutter_ranker import (
    CATEGORICAL_FEATURE_INDICES,
    FEATURE_NAMES,
    ClutterRanker,
    _hash_bucket,
    build_ranked_clutter_entries,
    extract_numeric_features,
)
from reclaim.ai.feedback_store import ClusterStats, FeatureVector, SiblingDecisionContext
from reclaim.ai.models import AITrack

_NO_CLUSTER = ClusterStats(
    cluster_size=1,
    position_in_cluster=None,
    raw_score=0.0,
    score_kind="none",
    is_recommended_keep=False,
)
_NO_SIBLINGS = SiblingDecisionContext(prior_accepted=0, prior_rejected=0, prior_kept=0)


def _feature_vector(**overrides: object) -> FeatureVector:
    defaults: dict[str, object] = {
        "size_bytes": 1_000_000,
        "ext": ".tmp",
        "path_class": "temp",
        "mtime": 1_700_000_000.0,
        "ctime": 1_699_000_000.0,
        "cluster_stats": _NO_CLUSTER,
        "category": "temp_and_browser_caches",
        "cloud_sync_flag": False,
        "sibling_decision_context": _NO_SIBLINGS,
    }
    defaults.update(overrides)
    return FeatureVector(**defaults)  # type: ignore[arg-type]


def test_hash_bucket_is_deterministic_across_calls() -> None:
    assert _hash_bucket("temp") == _hash_bucket("temp")
    assert _hash_bucket(".pdf") == _hash_bucket(".pdf")


def test_hash_bucket_is_within_range() -> None:
    for value in ("temp", "documents", ".pdf", ".exe", "some_unseen_extension_xyz", ""):
        bucket = _hash_bucket(value)
        assert 0 <= bucket < 256


def test_hash_bucket_different_strings_usually_differ() -> None:
    """Not a formal proof (hash collisions are possible) — a sanity check that this isn't
    accidentally a constant function."""
    buckets = {_hash_bucket(v) for v in ("temp", "documents", "downloads", "other", ".pdf")}
    assert len(buckets) > 1


def test_extract_numeric_features_returns_one_value_per_feature_name() -> None:
    fv = _feature_vector()
    features = extract_numeric_features(fv, now=1_700_100_000.0)
    assert len(features) == len(FEATURE_NAMES)
    assert all(isinstance(f, float) for f in features)


def test_extract_numeric_features_age_never_negative_for_future_mtime() -> None:
    fv = _feature_vector(mtime=2_000_000_000.0, ctime=2_000_000_000.0)
    features = extract_numeric_features(fv, now=1_700_000_000.0)
    age_days_index = FEATURE_NAMES.index("age_days")
    ctime_age_days_index = FEATURE_NAMES.index("ctime_age_days")
    assert features[age_days_index] == 0.0
    assert features[ctime_age_days_index] == 0.0


def test_extract_numeric_features_larger_file_has_larger_log_size() -> None:
    small = extract_numeric_features(_feature_vector(size_bytes=1_000), now=1_700_100_000.0)
    large = extract_numeric_features(_feature_vector(size_bytes=1_000_000_000), now=1_700_100_000.0)
    log_size_index = FEATURE_NAMES.index("log_size_bytes")
    assert large[log_size_index] > small[log_size_index]


def test_extract_numeric_features_reflects_cluster_stats() -> None:
    in_cluster = ClusterStats(
        cluster_size=5,
        position_in_cluster=2,
        raw_score=0.92,
        score_kind="hamming_distance",
        is_recommended_keep=True,
    )
    fv = _feature_vector(cluster_stats=in_cluster)
    features = extract_numeric_features(fv, now=1_700_100_000.0)
    assert features[FEATURE_NAMES.index("cluster_size")] == 5.0
    assert features[FEATURE_NAMES.index("is_recommended_keep")] == 1.0
    assert features[FEATURE_NAMES.index("cluster_raw_score")] == pytest.approx(0.92)


def test_extract_numeric_features_reflects_sibling_context() -> None:
    siblings = SiblingDecisionContext(prior_accepted=3, prior_rejected=1, prior_kept=2)
    fv = _feature_vector(sibling_decision_context=siblings)
    features = extract_numeric_features(fv, now=1_700_100_000.0)
    assert features[FEATURE_NAMES.index("prior_accepted")] == 3.0
    assert features[FEATURE_NAMES.index("prior_rejected")] == 1.0
    assert features[FEATURE_NAMES.index("prior_kept")] == 2.0


def test_categorical_feature_indices_point_at_the_bucketed_string_fields() -> None:
    expected = {"path_class_bucket", "category_bucket", "ext_bucket"}
    actual = {FEATURE_NAMES[i] for i in CATEGORICAL_FEATURE_INDICES}
    assert actual == expected


def test_cluster_ranker_raises_actionable_error_when_no_model_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"label_ranker_fixtures\.py"):
        ClutterRanker(model_path=tmp_path / "does_not_exist.txt")


def _high_clutter_feature_vector() -> FeatureVector:
    return _feature_vector(
        ext=".tmp", path_class="temp", category="temp_and_browser_caches", size_bytes=1_000
    )


def _low_clutter_feature_vector() -> FeatureVector:
    return _feature_vector(
        ext=".pdf", path_class="documents", category="uncategorized", size_bytes=2_000_000
    )


def _train_toy_ranker(tmp_path: Path) -> ClutterRanker:
    """A minimal real LightGBM LambdaMART booster trained on a handful of synthetic feature
    vectors — enough to test `ClutterRanker.score`/`rank`'s real prediction path without
    depending on the full ~1.75-hour cross-LLM labeling pipeline (ADR-0021's own eval does
    that against the real trained artifact). Every downstream test in this file scores/ranks
    the EXACT SAME `_high_clutter_feature_vector`/`_low_clutter_feature_vector` this trains
    on — a toy model trained on 2 distinct feature combinations has no reason to generalize
    to a differently-shaped feature vector it never saw."""
    lightgbm = pytest.importorskip("lightgbm")
    np = pytest.importorskip("numpy")

    now = 1_700_100_000.0
    features = np.array(
        [
            extract_numeric_features(_high_clutter_feature_vector(), now=now),
            extract_numeric_features(_low_clutter_feature_vector(), now=now),
        ]
        * 10
    )
    labels = [4, 0] * 10
    dataset = lightgbm.Dataset(
        features,
        label=labels,
        group=[len(labels)],
        categorical_feature=list(CATEGORICAL_FEATURE_INDICES),
        feature_name=list(FEATURE_NAMES),
    )
    booster = lightgbm.train(
        {"objective": "lambdarank", "verbosity": -1, "min_data_in_leaf": 1, "num_leaves": 4},
        dataset,
        num_boost_round=30,
    )
    model_path = tmp_path / "toy_ranker.txt"
    booster.save_model(str(model_path))
    return ClutterRanker(model_path=model_path)


def test_cluster_ranker_scores_higher_clutter_likelihood_higher(tmp_path: Path) -> None:
    ranker = _train_toy_ranker(tmp_path)
    now = 1_700_100_000.0
    high = ranker.score(_high_clutter_feature_vector(), now=now)
    low = ranker.score(_low_clutter_feature_vector(), now=now)
    assert high.raw_score > low.raw_score
    assert high.is_generic is True


def test_cluster_ranker_rank_sorts_descending(tmp_path: Path) -> None:
    ranker = _train_toy_ranker(tmp_path)
    now = 1_700_100_000.0
    low = _low_clutter_feature_vector()
    high = _high_clutter_feature_vector()
    ranked = ranker.rank([low, high], now=now)
    assert [index for index, _score in ranked] == [1, 0]


def test_cluster_ranker_rank_empty_input_returns_empty(tmp_path: Path) -> None:
    ranker = _train_toy_ranker(tmp_path)
    assert ranker.rank([], now=1_700_100_000.0) == []


def test_build_ranked_clutter_entries_produces_valid_singleton_clusters(tmp_path: Path) -> None:
    ranker = _train_toy_ranker(tmp_path)
    paths = [Path("a.tmp"), Path("b.pdf")]
    feature_vectors = [
        _feature_vector(ext=".tmp", path_class="temp", category="temp_and_browser_caches"),
        _feature_vector(ext=".pdf", path_class="documents", category="uncategorized"),
    ]
    entries = build_ranked_clutter_entries(paths, feature_vectors, ranker, now=1_700_100_000.0)
    assert len(entries) == 2
    for entry in entries:
        assert entry.track == AITrack.RANKED_CLUTTER
        assert len(entry.members) == 1
        assert entry.members[0].is_recommended_keep is False
        assert entry.suggests_deletion is False


def test_build_ranked_clutter_entries_rejects_mismatched_lengths(tmp_path: Path) -> None:
    ranker = _train_toy_ranker(tmp_path)
    with pytest.raises(ValueError, match="same length"):
        build_ranked_clutter_entries(
            [Path("a.tmp")], [_feature_vector(), _feature_vector()], ranker, now=1_700_100_000.0
        )
