from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.eval_harness import (
    bcubed_precision_recall,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)

# Correctness of these functions is foundational — every future feature's eval gate reports
# a BCubed/PR-curve number computed by this module. A bug here would silently corrupt every
# precision/recall figure this layer ever reports, so every expected value below is
# hand-computed in the docstring/comment next to its test, not just asserted against
# whatever the code currently returns.


def test_bcubed_perfect_clustering_scores_one() -> None:
    true = {"a": "T1", "b": "T1", "c": "T2"}
    result = bcubed_precision_recall(true, true)
    assert result.precision == pytest.approx(1.0)
    assert result.recall == pytest.approx(1.0)
    assert result.f1 == pytest.approx(1.0)


def test_bcubed_hand_computed_mixed_clustering() -> None:
    """True: {a,b,c}=T1, {d}=T2. Predicted: {a,b}=P1, {c,d}=P2.
    a: same_pred={a,b}, same_true={a,b,c}, correct={a,b} -> P=2/2=1, R=2/3
    b: same as a -> P=1, R=2/3
    c: same_pred={c,d}, same_true={a,b,c}, correct={c} -> P=1/2, R=1/3
    d: same_pred={c,d}, same_true={d}, correct={d} -> P=1/2, R=1
    mean precision = (1+1+0.5+0.5)/4 = 0.75
    mean recall = (2/3+2/3+1/3+1)/4 = 0.666666...
    """
    true = {"a": "T1", "b": "T1", "c": "T1", "d": "T2"}
    predicted = {"a": "P1", "b": "P1", "c": "P2", "d": "P2"}
    result = bcubed_precision_recall(predicted, true)
    assert result.precision == pytest.approx(0.75)
    assert result.recall == pytest.approx(2 / 3)


def test_bcubed_every_item_its_own_predicted_cluster_is_perfect_precision_low_recall() -> None:
    true = {"a": "T1", "b": "T1", "c": "T1"}
    predicted = {"a": "P1", "b": "P2", "c": "P3"}
    result = bcubed_precision_recall(predicted, true)
    assert result.precision == pytest.approx(1.0)  # every item alone with itself: 1/1
    assert result.recall == pytest.approx(1 / 3)  # each only recalls itself out of 3


def test_bcubed_one_giant_predicted_cluster_is_perfect_recall_low_precision() -> None:
    true = {"a": "T1", "b": "T2", "c": "T3"}
    predicted = {"a": "P1", "b": "P1", "c": "P1"}
    result = bcubed_precision_recall(predicted, true)
    assert result.recall == pytest.approx(1.0)
    assert result.precision == pytest.approx(1 / 3)


def test_bcubed_rejects_mismatched_item_sets() -> None:
    with pytest.raises(ValueError, match="same item set"):
        bcubed_precision_recall({"a": "P1"}, {"a": "T1", "b": "T1"})


def test_bcubed_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        bcubed_precision_recall({}, {})


def test_pr_curve_hand_computed() -> None:
    """scored (desc by score): (0.9,T),(0.8,F),(0.7,T),(0.6,T); total_positive=3.
    step1: tp=1,fp=0 -> P=1.0,   R=1/3
    step2: tp=1,fp=1 -> P=0.5,   R=1/3
    step3: tp=2,fp=1 -> P=2/3,   R=2/3
    step4: tp=3,fp=1 -> P=0.75,  R=1.0
    """
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)
    assert [p.precision for p in curve] == pytest.approx([1.0, 0.5, 2 / 3, 0.75])
    assert [p.recall for p in curve] == pytest.approx([1 / 3, 1 / 3, 2 / 3, 1.0])


def test_pr_curve_lower_score_more_similar_direction() -> None:
    """Same data, but scores are a distance (lower = more similar) — reversing the sort
    direction should reproduce the exact same curve shape as the cosine-similarity case
    above, just fed in descending-distance order."""
    scored = [(0.1, True), (0.2, False), (0.3, True), (0.4, True)]
    curve = precision_recall_curve(scored, higher_score_is_more_similar=False)
    assert [p.precision for p in curve] == pytest.approx([1.0, 0.5, 2 / 3, 0.75])
    assert [p.recall for p in curve] == pytest.approx([1 / 3, 1 / 3, 2 / 3, 1.0])


def test_pr_curve_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero scored items"):
        precision_recall_curve([])


def test_select_operating_point_picks_highest_recall_meeting_target_precision() -> None:
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored)

    strict = select_operating_point(curve, target_precision=0.9, source_description="test")
    assert strict is not None
    assert strict.threshold == pytest.approx(0.9)
    assert strict.recall == pytest.approx(1 / 3)
    assert strict.is_provisional is True

    looser = select_operating_point(curve, target_precision=0.6, source_description="test")
    assert looser is not None
    assert looser.threshold == pytest.approx(0.6)
    assert looser.recall == pytest.approx(1.0)


def test_select_operating_point_returns_none_when_no_point_meets_target() -> None:
    """target_precision above 1.0 -- the maximum any point can score -- guarantees zero
    eligible points regardless of curve shape."""
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored)
    result = select_operating_point(curve, target_precision=1.01, source_description="test")
    assert result is None


def test_current_commit_sha_returns_a_real_hash_in_this_repo() -> None:
    sha = current_commit_sha()
    assert sha != "unknown"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_commit_sha_returns_unknown_outside_a_repo(tmp_path: Path) -> None:
    sha = current_commit_sha(repo_root=tmp_path)
    assert sha == "unknown"
