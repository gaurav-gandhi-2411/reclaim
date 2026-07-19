from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    UnsafeMeasuredPromotionError,
    assert_safe_to_promote_to_measured,
    bcubed_precision_recall,
    cohens_kappa,
    current_commit_sha,
    exact_order_accuracy,
    fleiss_kappa,
    kendall_tau,
    ndcg_at_k,
    precision_at_k,
    precision_recall_curve,
    select_joint_operating_point,
    select_joint_operating_point_per_tier,
    select_operating_point,
    select_operating_point_per_tier,
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


def test_select_joint_operating_point_finds_and_gate_when_neither_stage_alone_separates() -> None:
    """The whole reason this function exists (ADR-0017's templated-document finding): stage 1
    alone can't separate these (positives and negatives overlap on stage1_score), stage 2
    alone can't either (same overlap on stage2_score) -- but the AND of BOTH conditions
    cleanly separates every positive from every negative. positives: (0.9, 0.5), (0.5, 0.9).
    negatives: (0.9, 0.3), (0.3, 0.9) -- each negative is high on exactly one stage, mimicking
    a document that shares boilerplate (high stage1) OR reads semantically similar in
    isolation (high stage2) but not both."""
    positives = [(0.9, 0.5), (0.5, 0.9)]
    negatives = [(0.9, 0.3), (0.3, 0.9)]
    result = select_joint_operating_point(
        positives,
        negatives,
        stage1_candidates=[0.3, 0.5, 0.7, 0.9],
        stage2_candidates=[0.3, 0.5, 0.7, 0.9],
        target_precision=1.0,
        min_recall=1.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is not None
    assert result.precision == pytest.approx(1.0)
    assert result.recall == pytest.approx(1.0)
    assert result.stage1_threshold <= 0.5
    assert result.stage2_threshold <= 0.5


def test_select_joint_operating_point_returns_none_when_no_grid_point_qualifies() -> None:
    positives = [(0.5, 0.5)]
    negatives = [(0.9, 0.9)]  # dominates every candidate threshold -> precision never reaches 1.0
    result = select_joint_operating_point(
        positives,
        negatives,
        stage1_candidates=[0.1, 0.5],
        stage2_candidates=[0.1, 0.5],
        target_precision=1.0,
        min_recall=1.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is None


def test_select_joint_operating_point_picks_highest_recall_among_qualifying_combinations() -> None:
    """Two thresholds both clear target_precision=1.0 with zero false positives; the looser
    one (lower thresholds) should win since it has higher recall."""
    positives = [(0.9, 0.9), (0.6, 0.6)]
    negatives = [(0.3, 0.3)]
    result = select_joint_operating_point(
        positives,
        negatives,
        stage1_candidates=[0.5, 0.8],
        stage2_candidates=[0.5, 0.8],
        target_precision=1.0,
        min_recall=0.0,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is not None
    assert result.stage1_threshold == pytest.approx(0.5)
    assert result.stage2_threshold == pytest.approx(0.5)
    assert result.recall == pytest.approx(1.0)


# --- ADR-0018's metrics-integrity invariant: never aggregate across tiers ------------------


def test_select_joint_operating_point_per_tier_rejects_the_real_adr0017_incident() -> None:
    """The EXACT numbers from ADR-0017's follow-up incident, reproduced as a permanent
    regression test. At the real chosen grid point, a large "prose" tier (360 positives,
    7,140 negatives, all cleanly separated) was pooled with a small "templated" tier (162
    positives, 459 negatives, a real precision failure) into one aggregate calculation:
    500 true positives / 25 false positives pooled = 0.9524 precision, which cleared the 0.95
    target -- while the templated tier ALONE was actually at 158/(158+25) = 0.8634 precision,
    a real failure the pooled number completely hid. This test reconstructs those exact counts
    (as placeholder (1.0, 1.0) / (0.0, 0.0) score pairs crossing a trivial 0.5 threshold, not
    real text) and proves two things: (1) the naive pooled approach (`select_joint_
    operating_point`, concatenating both tiers' pairs) WOULD have accepted this operating
    point -- reproducing the bug -- and (2) the per-tier-gated function
    (`select_joint_operating_point_per_tier`) correctly REJECTS it, because the small tier
    fails the target precision on its own."""
    # prose (large) tier: 342/360 positives pass (recall 0.9500), 0/7140 negatives pass (all
    # correctly rejected) -- precision 1.0 alone.
    prose_positive = [(1.0, 1.0)] * 342 + [(0.0, 0.0)] * 18
    prose_negative = [(0.0, 0.0)] * 7_140

    # templated (small) tier: 158/162 positives pass (recall 0.9753), 25/459 negatives
    # INCORRECTLY pass -- precision 158/183 = 0.8634 alone, a real failure.
    templated_positive = [(1.0, 1.0)] * 158 + [(0.0, 0.0)] * 4
    templated_negative = [(1.0, 1.0)] * 25 + [(0.0, 0.0)] * 434

    # Sanity: confirm the hand-picked counts really do reproduce the real incident's numbers
    # before testing the two selection functions against them.
    pooled_tp = 342 + 158
    pooled_fp = 0 + 25
    pooled_precision = pooled_tp / (pooled_tp + pooled_fp)
    pooled_recall = pooled_tp / (360 + 162)
    assert pooled_precision == pytest.approx(0.9524, abs=0.0001)
    assert pooled_recall == pytest.approx(0.9579, abs=0.0001)
    templated_precision_alone = 158 / (158 + 25)
    assert templated_precision_alone == pytest.approx(0.8634, abs=0.0001)

    # (1) The naive pooled approach WOULD have accepted this -- reproducing the bug.
    pooled_result = select_joint_operating_point(
        prose_positive + templated_positive,
        prose_negative + templated_negative,
        stage1_candidates=[0.5],
        stage2_candidates=[0.5],
        target_precision=0.95,
        min_recall=0.5,
        distribution=_distribution(),
        source_description="test — reproducing the pooled bug",
    )
    assert pooled_result is not None, (
        "sanity check: the pooled/aggregate approach must accept this point (0.9524 >= 0.95) "
        "for this test to actually demonstrate the incident, not a strawman"
    )
    assert pooled_result.precision == pytest.approx(0.9524, abs=0.0001)

    # (2) The per-tier-gated function correctly REJECTS it: the templated tier alone is only
    # 0.8634 precision, below the 0.95 target.
    tiers = {
        "prose": (prose_positive, prose_negative),
        "templated": (templated_positive, templated_negative),
    }
    gated_result = select_joint_operating_point_per_tier(
        tiers,
        stage1_candidates=[0.5],
        stage2_candidates=[0.5],
        target_precision=0.95,
        min_recall=0.5,
        distribution=_distribution(),
        source_description="test — per-tier gate",
    )
    assert gated_result is None, (
        "the per-tier-gated selector must REJECT a threshold where any individual tier fails "
        "its precision/recall floor, even though the pooled aggregate across all tiers passes "
        "-- this is the exact ADR-0017 incident, and it must never recur silently"
    )


def test_select_joint_operating_point_per_tier_accepts_when_all_tiers_qualify() -> None:
    """Complementary positive case: when every tier genuinely clears the floors on its own
    (not just in aggregate), the per-tier gate accepts it and reports each tier's real
    precision/recall, with no aggregate/pooled number anywhere on the result."""
    tier_a_positive = [(0.9, 0.9)] * 10
    tier_a_negative = [(0.1, 0.1)] * 100
    tier_b_positive = [(0.9, 0.9)] * 5
    tier_b_negative = [(0.1, 0.1)] * 20

    result = select_joint_operating_point_per_tier(
        {
            "tier_a": (tier_a_positive, tier_a_negative),
            "tier_b": (tier_b_positive, tier_b_negative),
        },
        stage1_candidates=[0.5],
        stage2_candidates=[0.5],
        target_precision=0.95,
        min_recall=0.5,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is not None
    assert result.per_tier["tier_a"].precision == pytest.approx(1.0)
    assert result.per_tier["tier_b"].precision == pytest.approx(1.0)
    assert result.per_tier["tier_a"].recall == pytest.approx(1.0)
    assert result.per_tier["tier_b"].recall == pytest.approx(1.0)


def test_select_operating_point_per_tier_1d_rejects_when_one_tier_fails() -> None:
    """1D (single-stage) analog of the same invariant — a small tier's failure must not be
    hidden by pooling with a large, clean tier, even in the simpler one-threshold case."""
    large_tier_positive = [1.0] * 100
    large_tier_negative = [0.0] * 1000
    small_tier_positive = [1.0] * 10
    small_tier_negative = [1.0] * 3  # 3 false positives in a small pool = real failure

    result = select_operating_point_per_tier(
        {
            "large": (large_tier_positive, large_tier_negative),
            "small": (small_tier_positive, small_tier_negative),
        },
        candidates=[0.5],
        target_precision=0.95,
        min_recall=0.5,
        distribution=_distribution(),
        source_description="test",
    )
    assert result is None, (
        "small tier precision is 10/13 = 0.769, well below the 0.95 target — the per-tier "
        "gate must reject even though the large tier alone is perfect"
    )


def test_select_operating_point_per_tier_rejects_empty_tiers_mapping() -> None:
    """Regression: an empty `tiers` dict used to crash with a bare `min() iterable argument
    is empty` inside the selection loop instead of a clear, actionable error — caught while
    writing this test suite, fixed with an explicit guard."""
    with pytest.raises(ValueError, match="tiers must not be empty"):
        select_operating_point_per_tier(
            {},
            candidates=[0.5],
            target_precision=0.95,
            min_recall=0.5,
            distribution=_distribution(),
            source_description="test",
        )


def test_select_joint_operating_point_per_tier_rejects_empty_tiers_mapping() -> None:
    with pytest.raises(ValueError, match="tiers must not be empty"):
        select_joint_operating_point_per_tier(
            {},
            stage1_candidates=[0.5],
            stage2_candidates=[0.5],
            target_precision=0.95,
            min_recall=0.5,
            distribution=_distribution(),
            source_description="test",
        )


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


# --- fleiss_kappa / cohens_kappa (ADR-0021's cross-LLM labeling agreement measure) ----------


def test_fleiss_kappa_perfect_agreement_is_one() -> None:
    assert fleiss_kappa([[0, 0, 0], [4, 4, 4], [2, 2, 2]]) == pytest.approx(1.0)


def test_fleiss_kappa_hand_computed_partial_agreement() -> None:
    """4 items, 3 raters, categories {0,1}. item1=[0,0,0] item2=[1,1,1] item3=[0,0,1]
    item4=[0,1,1]. Hand-derived: P_bar=2/3, P_bar_e=1/2, kappa=(2/3-1/2)/(1/2)=1/3."""
    ratings = [[0, 0, 0], [1, 1, 1], [0, 0, 1], [0, 1, 1]]
    assert fleiss_kappa(ratings) == pytest.approx(1 / 3)


def test_fleiss_kappa_rejects_unequal_rater_counts() -> None:
    with pytest.raises(ValueError, match="same number of raters"):
        fleiss_kappa([[0, 0, 0], [1, 1]])


def test_fleiss_kappa_rejects_fewer_than_two_raters() -> None:
    with pytest.raises(ValueError, match="at least 2 raters"):
        fleiss_kappa([[0], [1]])


def test_fleiss_kappa_rejects_empty_items() -> None:
    with pytest.raises(ValueError, match="zero items"):
        fleiss_kappa([])


def test_cohens_kappa_hand_computed() -> None:
    """rater_a=[0,0,1,1] rater_b=[0,1,1,1]. p_o=3/4 (agree on items 1,3,4). rater_a
    proportions (0.5,0.5), rater_b proportions (0.25,0.75). p_e=0.5*0.25+0.5*0.75=0.5.
    kappa=(0.75-0.5)/(1-0.5)=0.5."""
    assert cohens_kappa([0, 0, 1, 1], [0, 1, 1, 1]) == pytest.approx(0.5)


def test_cohens_kappa_perfect_agreement_is_one() -> None:
    assert cohens_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)


def test_cohens_kappa_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same items"):
        cohens_kappa([0, 1], [0, 1, 2])


def test_cohens_kappa_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero items"):
        cohens_kappa([], [])


# --- ndcg_at_k / precision_at_k (ADR-0021's ranking-quality metrics) ------------------------


def test_ndcg_at_k_ideal_order_is_one() -> None:
    assert ndcg_at_k([4, 0, 0], 3) == pytest.approx(1.0)


def test_ndcg_at_k_hand_computed_non_ideal_order() -> None:
    """predicted=[0,4,0] (the relevance-4 item ranked 2nd, not 1st). DCG =
    (2^0-1)/log2(2) + (2^4-1)/log2(3) + (2^0-1)/log2(4) = 0 + 15/log2(3) + 0. IDCG (order
    [4,0,0]) = 15. NDCG = (15/log2(3))/15 = 1/log2(3) ~= 0.630930."""
    import math

    assert ndcg_at_k([0, 4, 0], 3) == pytest.approx(1 / math.log2(3))


def test_ndcg_at_k_all_zero_relevance_is_one() -> None:
    assert ndcg_at_k([0, 0, 0], 3) == pytest.approx(1.0)


def test_ndcg_at_k_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        ndcg_at_k([1, 2], 0)


def test_ndcg_at_k_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero items"):
        ndcg_at_k([], 3)


def test_precision_at_k_hand_computed() -> None:
    """predicted=[4,3,1,0,2], k=3, floor=3 -> top3=[4,3,1], 2 of 3 clear the floor."""
    assert precision_at_k([4, 3, 1, 0, 2], 3, relevance_floor=3) == pytest.approx(2 / 3)


def test_precision_at_k_all_clear_floor_is_one() -> None:
    assert precision_at_k([4, 4, 4], 3, relevance_floor=1) == pytest.approx(1.0)


def test_precision_at_k_none_clear_floor_is_zero() -> None:
    assert precision_at_k([0, 0, 0], 3, relevance_floor=4) == pytest.approx(0.0)


def test_precision_at_k_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        precision_at_k([1, 2], 0, relevance_floor=1)


def test_precision_at_k_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="zero items"):
        precision_at_k([], 3, relevance_floor=1)
