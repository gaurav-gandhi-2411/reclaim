from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    UnsafeMeasuredPromotionError,
    assert_safe_to_promote_to_measured,
    bcubed_precision_recall,
    current_commit_sha,
    exact_order_accuracy,
    kendall_tau,
    precision_recall_curve,
    select_operating_point,
)


def _distribution(**overrides: object) -> DistributionDeclaration:
    """Test convenience: a valid, realistic-by-default declaration with any field overridden."""
    defaults: dict[str, object] = {
        "description": "test distribution",
        "is_realistic": True,
        "is_adversarial_tail_only": False,
        "is_synthetic_only": False,
        "untested_variation_note": "n/a — test fixture",
    }
    defaults.update(overrides)
    return DistributionDeclaration(**defaults)  # type: ignore[arg-type]


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

    strict = select_operating_point(
        curve,
        target_precision=0.9,
        min_recall=0.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert strict is not None
    assert strict.threshold == pytest.approx(0.9)
    assert strict.recall == pytest.approx(1 / 3)
    assert strict.is_provisional is True
    assert strict.distribution.is_realistic is True

    looser = select_operating_point(
        curve,
        target_precision=0.6,
        min_recall=0.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert looser is not None
    assert looser.threshold == pytest.approx(0.6)
    assert looser.recall == pytest.approx(1.0)


def test_select_operating_point_returns_none_when_no_point_meets_target() -> None:
    """target_precision above 1.0 -- the maximum any point can score -- guarantees zero
    eligible points regardless of curve shape."""
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored)
    result = select_operating_point(
        curve,
        target_precision=1.01,
        min_recall=0.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is None


def test_select_operating_point_returns_none_when_recall_floor_not_met() -> None:
    """ADR-0016's gate hardening: a point can clear the precision target and still fail the
    gate if its recall is below the usefulness floor. threshold=0.9 clears target_precision=0.9
    with recall=1/3 (per the hand-computed docstring above) -- setting min_recall=0.5 must
    reject it even though it would have passed the old precision-only gate. This is the
    regression test for the exact class of mistake ADR-0012 made."""
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored)
    result = select_operating_point(
        curve,
        target_precision=0.9,
        min_recall=0.5,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is None


def test_select_operating_point_accepts_recall_exactly_at_the_floor() -> None:
    """Boundary case: `<` not `<=` in the floor comparison -- a point whose recall exactly
    equals min_recall must PASS, not be rejected. threshold=0.6 (target_precision=0.6) has
    recall=1.0 per the hand-computed docstring above; using that exact value as the floor
    proves the comparison is inclusive at the boundary."""
    scored = [(0.9, True), (0.8, False), (0.7, True), (0.6, True)]
    curve = precision_recall_curve(scored)
    result = select_operating_point(
        curve,
        target_precision=0.6,
        min_recall=1.0,  # exactly the achievable recall at this precision target
        distribution=_distribution(),
        source_description="test",
    )
    assert result is not None
    assert result.recall == pytest.approx(1.0)


def test_distribution_declaration_rejects_empty_description() -> None:
    with pytest.raises(ValueError, match="description must not be empty"):
        _distribution(description="")


def test_distribution_declaration_rejects_empty_untested_variation_note() -> None:
    with pytest.raises(ValueError, match="untested_variation_note must not be empty"):
        _distribution(untested_variation_note="")


def test_distribution_declaration_rejects_realistic_and_adversarial_together() -> None:
    with pytest.raises(ValueError, match="pick the honest one"):
        _distribution(is_realistic=True, is_adversarial_tail_only=True)


def test_distribution_declaration_rejects_realistic_and_synthetic_together() -> None:
    with pytest.raises(ValueError, match="pick the honest one"):
        _distribution(is_realistic=True, is_synthetic_only=True)


def test_assert_safe_to_promote_to_measured_passes_for_realistic_distribution() -> None:
    assert_safe_to_promote_to_measured(_distribution())  # must not raise


def test_assert_safe_to_promote_to_measured_rejects_adversarial_tail_only() -> None:
    """This is the structural proof that ADR-0012's original mistake -- promoting
    max_hamming_distance=14 to MEASURED off Copydays' `strong` split alone -- would now be
    caught mechanically, not just by GG reading the ADR closely."""
    adversarial = _distribution(
        is_realistic=False,
        is_adversarial_tail_only=True,
        description="Copydays strong-attack split",
    )
    with pytest.raises(UnsafeMeasuredPromotionError, match="adversarial-tail-only"):
        assert_safe_to_promote_to_measured(adversarial)


def test_assert_safe_to_promote_to_measured_rejects_synthetic_only() -> None:
    synthetic = _distribution(
        is_realistic=False, is_synthetic_only=True, description="synthetic CI fixtures"
    )
    with pytest.raises(UnsafeMeasuredPromotionError, match="synthetic-only"):
        assert_safe_to_promote_to_measured(synthetic)


def test_current_commit_sha_returns_a_real_hash_in_this_repo() -> None:
    sha = current_commit_sha()
    assert sha != "unknown"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_commit_sha_returns_unknown_outside_a_repo(tmp_path: Path) -> None:
    sha = current_commit_sha(repo_root=tmp_path)
    assert sha == "unknown"


def test_exact_order_accuracy_identical_order_is_one() -> None:
    assert exact_order_accuracy(["a", "b", "c"], ["a", "b", "c"]) == 1.0


def test_exact_order_accuracy_any_mismatch_is_zero() -> None:
    assert exact_order_accuracy(["a", "c", "b"], ["a", "b", "c"]) == 0.0
    assert exact_order_accuracy(["c", "b", "a"], ["a", "b", "c"]) == 0.0


def test_exact_order_accuracy_rejects_mismatched_item_sets() -> None:
    with pytest.raises(ValueError, match="same items"):
        exact_order_accuracy(["a", "b"], ["a", "c"])


def test_kendall_tau_identical_order_is_one() -> None:
    assert kendall_tau(["a", "b", "c", "d"], ["a", "b", "c", "d"]) == pytest.approx(1.0)


def test_kendall_tau_fully_reversed_order_is_negative_one() -> None:
    assert kendall_tau(["d", "c", "b", "a"], ["a", "b", "c", "d"]) == pytest.approx(-1.0)


def test_kendall_tau_hand_computed_partial_agreement() -> None:
    """true=[a,b,c,d]. predicted=[a,c,b,d]: pairs (a,b) (a,c) (a,d) (c,d) (b,d) concordant,
    (b,c) discordant (true has b before c, predicted has c before b). 5 concordant, 1
    discordant, 6 total pairs -> tau = (5-1)/6 = 0.6667."""
    assert kendall_tau(["a", "c", "b", "d"], ["a", "b", "c", "d"]) == pytest.approx(4 / 6)


def test_kendall_tau_rejects_mismatched_item_sets() -> None:
    with pytest.raises(ValueError, match="same items"):
        kendall_tau(["a", "b"], ["a", "c"])


def test_kendall_tau_single_item_is_trivially_one() -> None:
    assert kendall_tau(["a"], ["a"]) == 1.0
