from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Reusable metric-computation library for every AI-feature eval (evals/test_ai_*.py). No
# feature-specific logic lives here — BCubed/PR-curve/provenance are generic across image
# clustering, doc clustering, and (later) the ranker's NDCG/precision@k. Pure functions,
# no I/O except `current_commit_sha`'s read-only `git rev-parse`.


@dataclass(frozen=True, slots=True)
class BCubedResult:
    """Cluster-quality metric (Bagga & Baldwin, 1998) — used instead of pairwise precision/
    recall because pairwise metrics over-reward large clusters; BCubed weights every item
    equally regardless of which cluster it lands in. See spec §7.2."""

    precision: float
    recall: float

    @property
    def f1(self) -> float:
        if self.precision + self.recall == 0:
            return 0.0
        return 2 * self.precision * self.recall / (self.precision + self.recall)


def bcubed_precision_recall(predicted: Mapping[str, str], true: Mapping[str, str]) -> BCubedResult:
    """`predicted`/`true` both map item-id -> cluster-id, covering the SAME item set (a
    singleton "cluster" — an item alone — is a valid cluster-id value, not a missing entry).

    For each item i: precision_i = |items sharing i's predicted cluster AND i's true
    cluster| / |items sharing i's predicted cluster|; recall_i is the same numerator over
    |items sharing i's true cluster|. BCubed precision/recall are the mean of precision_i/
    recall_i over all items.
    """
    items = list(predicted.keys())
    if set(items) != set(true.keys()):
        raise ValueError(
            "predicted and true cluster mappings must cover the exact same item set — "
            f"predicted has {len(predicted)} items, true has {len(true)}, "
            f"symmetric difference: {set(predicted) ^ set(true)}"
        )
    if not items:
        raise ValueError("cannot compute BCubed precision/recall over an empty item set")

    precisions: list[float] = []
    recalls: list[float] = []
    for item in items:
        same_predicted = {other for other in items if predicted[other] == predicted[item]}
        same_true = {other for other in items if true[other] == true[item]}
        correct = same_predicted & same_true
        precisions.append(len(correct) / len(same_predicted))
        recalls.append(len(correct) / len(same_true))

    return BCubedResult(
        precision=sum(precisions) / len(precisions), recall=sum(recalls) / len(recalls)
    )


@dataclass(frozen=True, slots=True)
class PRPoint:
    threshold: float
    precision: float
    recall: float


def precision_recall_curve(
    scored: Sequence[tuple[float, bool]], *, higher_score_is_more_similar: bool = True
) -> list[PRPoint]:
    """`scored`: one `(score, is_true_positive)` pair per candidate item/pair. Sorts by score
    from "most confident positive" to "least" (direction controlled by
    `higher_score_is_more_similar` — pass `False` for a distance metric like Hamming
    distance, where LOWER means more similar) and sweeps the threshold, computing precision/
    recall at each cut point. Used for operating-point selection (spec §7.3) — never to
    manufacture a probability (spec §0.6); the values here are precision/recall at a
    threshold, not a calibrated confidence.
    """
    if not scored:
        raise ValueError("cannot compute a PR curve over zero scored items")
    ordered = sorted(scored, key=lambda pair: pair[0], reverse=higher_score_is_more_similar)
    total_positive = sum(1 for _, is_positive in scored if is_positive)

    points: list[PRPoint] = []
    true_positives = 0
    false_positives = 0
    for score, is_positive in ordered:
        if is_positive:
            true_positives += 1
        else:
            false_positives += 1
        precision = true_positives / (true_positives + false_positives)
        recall = true_positives / total_positive if total_positive else 0.0
        points.append(PRPoint(threshold=score, precision=precision, recall=recall))
    return points


@dataclass(frozen=True, slots=True)
class DistributionDeclaration:
    """What distribution a PR curve / operating-point selection was actually computed
    against — REQUIRED alongside every `select_operating_point` call (ADR-0016's eval-gate
    hardening, prompted directly by ADR-0012's recall-artifact incident: a precision-only gate
    let Feature 1a's operating point pass CI while catching under 8% of real duplicates,
    because the only real data reachable was Copydays' single adversarial attack tier and
    nothing forced that fact to be stated anywhere machine-checkable). Every field must be set
    truthfully by the caller — there is no default that lets the honest answer go unstated.
    """

    description: str  # e.g. "5 realistic transform profiles on real Copydays originals"
    is_realistic: bool  # does this represent the feature's actual target use case?
    is_adversarial_tail_only: bool  # worst-case/attack-only data, not typical real usage
    is_synthetic_only: bool  # no real-world data involved at all
    untested_variation_note: str  # what real-world variation this measurement does NOT cover

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError(
                "DistributionDeclaration.description must not be empty — every "
                "operating-point selection must state what it was measured against"
            )
        if not self.untested_variation_note.strip():
            raise ValueError(
                "DistributionDeclaration.untested_variation_note must not be empty — even a "
                "realistic measurement has a boundary; state it explicitly rather than "
                "implying the measurement is exhaustive"
            )
        if self.is_realistic and (self.is_adversarial_tail_only or self.is_synthetic_only):
            raise ValueError(
                "a distribution cannot be both 'realistic' and "
                "'adversarial-tail-only'/'synthetic-only' — pick the honest one"
            )


class UnsafeMeasuredPromotionError(Exception):
    """Raised by `assert_safe_to_promote_to_measured` — see that function's docstring."""


def assert_safe_to_promote_to_measured(distribution: DistributionDeclaration) -> None:
    """Call this before an ADR claims an operating point is MEASURED (a real, production-
    basis number) rather than PROVISIONAL. Raises `UnsafeMeasuredPromotionError` if the
    distribution backing that claim is adversarial-tail-only or synthetic-only — both are
    legitimate, honestly-reportable measurements, but ADR-0012's incident proved neither may
    ALONE justify a "this is our real-world number" claim. A test asserting this function does
    NOT raise for a feature's chosen operating point is the structural proof (not just ADR
    prose) that the gate-hardening policy (ADR-0016) was actually followed.
    """
    if distribution.is_adversarial_tail_only:
        raise UnsafeMeasuredPromotionError(
            f"cannot promote to MEASURED from an adversarial-tail-only distribution "
            f"({distribution.description!r}) — this is exactly the mistake ADR-0012 made with "
            f"Copydays' `strong` split alone; report it as a disclosed tier, not the basis for "
            f"the operating point"
        )
    if distribution.is_synthetic_only:
        raise UnsafeMeasuredPromotionError(
            f"cannot promote to MEASURED from a synthetic-only distribution "
            f"({distribution.description!r}) — synthetic fixtures remain PROVISIONAL by "
            f"definition"
        )


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    threshold: float
    precision: float
    recall: float
    is_provisional: bool
    source_description: str
    distribution: DistributionDeclaration


def select_operating_point(
    curve: Sequence[PRPoint],
    *,
    target_precision: float,
    min_recall: float,
    distribution: DistributionDeclaration,
    source_description: str,
) -> OperatingPoint | None:
    """Picks the highest-recall point on `curve` whose precision >= `target_precision`, THEN
    requires that point's recall to also clear `min_recall` — a precision-only gate is exactly
    what let Feature 1a's first operating point pass CI while catching under 8% of real
    duplicates (ADR-0012's incident, ADR-0016's fix). Returns `None` if no point clears the
    precision target, OR if the best precision-qualifying point still fails the recall/
    usefulness floor: callers MUST handle `None` explicitly in both cases — never fall back to
    a lower-precision point, and never ship a precise-but-useless operating point, per spec
    §7.3 and ADR-0016.

    `distribution` is a required `DistributionDeclaration`, not optional — see that class's
    docstring. This function does not itself enforce `assert_safe_to_promote_to_measured`
    (a curve MAY legitimately be computed against an adversarial-tail-only distribution, e.g.
    to honestly report that tier's own numbers) — that check happens at the point an ADR
    claims MEASURED, not at every curve computation.

    `is_provisional` is always `True` from this function alone — a caller selecting on
    synthetic CI fixtures (the only data available before GG's gold-set labeling) must
    treat every returned `OperatingPoint` as provisional; only a gold-set-derived selection,
    explicitly reviewed, may ever be presented as a final operating point. This function has
    no way to know which kind of data it was given, so it never claims non-provisional on
    its own — the caller's ADR is what actually makes that determination and must say so.
    """
    eligible = [point for point in curve if point.precision >= target_precision]
    if not eligible:
        return None
    best = max(eligible, key=lambda point: point.recall)
    if best.recall < min_recall:
        return None
    return OperatingPoint(
        threshold=best.threshold,
        precision=best.precision,
        recall=best.recall,
        is_provisional=True,
        source_description=source_description,
        distribution=distribution,
    )


@dataclass(frozen=True, slots=True)
class JointOperatingPoint:
    """The 2D analog of `OperatingPoint` for a two-stage AND-gated pipeline (spec's own
    architecture: a cheap Stage-1 prefilter, then a Stage-2 check on the residual — a pair is
    only accepted if it clears BOTH). A single 1D PR curve can't represent this joint decision
    boundary; `select_joint_operating_point` grid-searches it directly instead."""

    stage1_threshold: float
    stage2_threshold: float
    precision: float
    recall: float
    is_provisional: bool
    source_description: str
    distribution: DistributionDeclaration


def select_joint_operating_point(
    positive_pairs: Sequence[tuple[float, float]],
    negative_pairs: Sequence[tuple[float, float]],
    *,
    stage1_candidates: Sequence[float],
    stage2_candidates: Sequence[float],
    target_precision: float,
    min_recall: float,
    distribution: DistributionDeclaration,
    source_description: str,
) -> JointOperatingPoint | None:
    """Grid-searches `stage1_candidates x stage2_candidates` for the AND-gated threshold pair
    (a pair is "flagged" iff `stage1_score >= t1 AND stage2_score >= t2`) with the highest
    recall among combinations that clear both `target_precision` and `min_recall` — same
    selection philosophy as `select_operating_point` (highest recall meeting both floors),
    extended to two jointly-applied thresholds instead of one. `positive_pairs`/
    `negative_pairs` are each a sequence of `(stage1_score, stage2_score)` per real pair —
    both scores must already be "higher = more similar" (callers negate a distance metric
    before calling this, same convention as `precision_recall_curve`).

    Returns `None` if no grid combination clears both floors — callers MUST handle this
    explicitly, never silently falling back to an unqualified combination.
    """
    best: JointOperatingPoint | None = None
    for stage1_threshold in stage1_candidates:
        for stage2_threshold in stage2_candidates:
            true_positives = sum(
                1 for s1, s2 in positive_pairs if s1 >= stage1_threshold and s2 >= stage2_threshold
            )
            false_positives = sum(
                1 for s1, s2 in negative_pairs if s1 >= stage1_threshold and s2 >= stage2_threshold
            )
            flagged = true_positives + false_positives
            if flagged == 0:
                continue
            precision = true_positives / flagged
            recall = true_positives / len(positive_pairs) if positive_pairs else 0.0
            if precision < target_precision or recall < min_recall:
                continue
            if best is None or recall > best.recall:
                best = JointOperatingPoint(
                    stage1_threshold=stage1_threshold,
                    stage2_threshold=stage2_threshold,
                    precision=precision,
                    recall=recall,
                    is_provisional=True,
                    source_description=source_description,
                    distribution=distribution,
                )
    return best


# --- Metrics-integrity invariant (ADR-0018): NEVER aggregate precision/recall across
# distinct declared tiers. A real incident (ADR-0017's templated-document follow-up) pooled a
# large, cleanly-separated tier (7,140 prose negatives, 0 false positives) with a small tier
# that had a real precision failure (459 templated negatives, 25 false positives) into one
# combined calculation — 500 true positives / 25 false positives across the pool reads as
# 0.9524 precision, which cleared the 0.95 target, while the small tier ALONE was actually at
# 0.8634 precision (158 TP / 25 FP within that tier), a real failure the pooled number hid
# completely. The large tier's clean negatives mathematically diluted the small tier's real
# problem. The functions below have NO code path that computes a number across pooled tiers —
# every tier's precision/recall is computed from ONLY that tier's own pairs, always, and a
# candidate threshold only qualifies if EVERY declared tier clears both floors independently.
# This is why `select_operating_point`/`select_joint_operating_point` above remain valid: they
# are correct and honest for a SINGLE declared distribution. The moment a caller has more than
# one named tier, it must use the `_per_tier` variants below — there is no correct way to pool.


@dataclass(frozen=True, slots=True)
class TierMetrics:
    precision: float
    recall: float


@dataclass(frozen=True, slots=True)
class TierGatedOperatingPoint:
    """The 1D analog of `OperatingPoint`, gated across multiple named tiers independently —
    see the metrics-integrity invariant above. `per_tier` records every tier's own precision/
    recall at the chosen threshold; there is no aggregate/pooled number anywhere on this type."""

    threshold: float
    per_tier: Mapping[str, TierMetrics]
    is_provisional: bool
    source_description: str
    distribution: DistributionDeclaration


@dataclass(frozen=True, slots=True)
class TierGatedJointOperatingPoint:
    """The 2D analog of `TierGatedOperatingPoint` — see `select_joint_operating_point_per_tier`."""

    stage1_threshold: float
    stage2_threshold: float
    per_tier: Mapping[str, TierMetrics]
    is_provisional: bool
    source_description: str
    distribution: DistributionDeclaration


def select_operating_point_per_tier(
    tiers: Mapping[str, tuple[Sequence[float], Sequence[float]]],
    *,
    candidates: Sequence[float],
    higher_score_is_more_similar: bool = True,
    target_precision: float,
    min_recall: float,
    distribution: DistributionDeclaration,
    source_description: str,
) -> TierGatedOperatingPoint | None:
    """`tiers` maps a tier name to `(positive_scores, negative_scores)` for that tier ONLY —
    never pool scores from different tiers into one sequence before calling this (that's
    exactly the mistake this function exists to make structurally impossible here). Sweeps
    `candidates`; a threshold qualifies only if EVERY tier's own precision/recall clears both
    `target_precision` and `min_recall` independently. Among qualifying thresholds, picks the
    one maximizing the MINIMUM recall across tiers (not the mean — a threshold that's
    excellent for one tier and merely adequate for another should not be preferred over one
    that's good for both, since the weaker tier is the one actually at risk).

    Returns `None` if no threshold clears every tier — callers MUST handle this explicitly.
    """
    if not tiers:
        raise ValueError(
            "tiers must not be empty — gating on zero declared tiers is meaningless, and "
            "silently returning None here would be indistinguishable from 'no threshold "
            "qualified', a different and more useful signal"
        )
    best: TierGatedOperatingPoint | None = None
    for threshold in candidates:
        per_tier: dict[str, TierMetrics] = {}
        qualifies = True
        for tier_name, (positive_scores, negative_scores) in tiers.items():
            if higher_score_is_more_similar:
                true_positives = sum(1 for s in positive_scores if s >= threshold)
                false_positives = sum(1 for s in negative_scores if s >= threshold)
            else:
                true_positives = sum(1 for s in positive_scores if s <= threshold)
                false_positives = sum(1 for s in negative_scores if s <= threshold)
            flagged = true_positives + false_positives
            precision = true_positives / flagged if flagged else 0.0
            recall = true_positives / len(positive_scores) if positive_scores else 0.0
            per_tier[tier_name] = TierMetrics(precision=precision, recall=recall)
            if precision < target_precision or recall < min_recall:
                qualifies = False
        if not qualifies:
            continue
        min_recall_across_tiers = min(metrics.recall for metrics in per_tier.values())
        if best is None or min_recall_across_tiers > min(
            metrics.recall for metrics in best.per_tier.values()
        ):
            best = TierGatedOperatingPoint(
                threshold=threshold,
                per_tier=per_tier,
                is_provisional=True,
                source_description=source_description,
                distribution=distribution,
            )
    return best


def select_joint_operating_point_per_tier(
    tiers: Mapping[str, tuple[Sequence[tuple[float, float]], Sequence[tuple[float, float]]]],
    *,
    stage1_candidates: Sequence[float],
    stage2_candidates: Sequence[float],
    target_precision: float,
    min_recall: float,
    distribution: DistributionDeclaration,
    source_description: str,
) -> TierGatedJointOperatingPoint | None:
    """The 2D (joint, AND-gated two-stage) analog of `select_operating_point_per_tier` — see
    the metrics-integrity invariant above `TierMetrics`. `tiers` maps a tier name to
    `(positive_pairs, negative_pairs)` for that tier ONLY, each pair being
    `(stage1_score, stage2_score)`. A `(t1, t2)` grid point only qualifies if EVERY tier's own
    precision/recall clears both floors independently; among qualifying points, picks the one
    maximizing the minimum recall across tiers.

    This is the function `evals/test_ai_document_templated_gold.py` uses — ADR-0017's own
    incident (a hand-rolled version of exactly this grid search, first written without
    per-tier gating) is why this exists as a reusable, tested harness primitive instead of
    something every eval re-implements ad hoc.
    """
    if not tiers:
        raise ValueError(
            "tiers must not be empty — gating on zero declared tiers is meaningless, and "
            "silently returning None here would be indistinguishable from 'no threshold "
            "qualified', a different and more useful signal"
        )
    best: TierGatedJointOperatingPoint | None = None
    for stage1_threshold in stage1_candidates:
        for stage2_threshold in stage2_candidates:
            per_tier: dict[str, TierMetrics] = {}
            qualifies = True
            for tier_name, (positive_pairs, negative_pairs) in tiers.items():
                true_positives = sum(
                    1
                    for s1, s2 in positive_pairs
                    if s1 >= stage1_threshold and s2 >= stage2_threshold
                )
                false_positives = sum(
                    1
                    for s1, s2 in negative_pairs
                    if s1 >= stage1_threshold and s2 >= stage2_threshold
                )
                flagged = true_positives + false_positives
                precision = true_positives / flagged if flagged else 0.0
                recall = true_positives / len(positive_pairs) if positive_pairs else 0.0
                per_tier[tier_name] = TierMetrics(precision=precision, recall=recall)
                if precision < target_precision or recall < min_recall:
                    qualifies = False
            if not qualifies:
                continue
            min_recall_across_tiers = min(metrics.recall for metrics in per_tier.values())
            if best is None or min_recall_across_tiers > min(
                metrics.recall for metrics in best.per_tier.values()
            ):
                best = TierGatedJointOperatingPoint(
                    stage1_threshold=stage1_threshold,
                    stage2_threshold=stage2_threshold,
                    per_tier=per_tier,
                    is_provisional=True,
                    source_description=source_description,
                    distribution=distribution,
                )
    return best


def exact_order_accuracy(predicted_order: Sequence[str], true_order: Sequence[str]) -> float:
    """1.0 if `predicted_order` matches `true_order` exactly (same items, same sequence),
    else 0.0 — the strict version-chain-ordering metric (spec §7.2), complementary to
    `kendall_tau`'s partial-credit measure."""
    if set(predicted_order) != set(true_order):
        raise ValueError(
            "predicted_order and true_order must contain the same items — "
            f"symmetric difference: {set(predicted_order) ^ set(true_order)}"
        )
    return 1.0 if list(predicted_order) == list(true_order) else 0.0


def kendall_tau(predicted_order: Sequence[str], true_order: Sequence[str]) -> float:
    """Kendall's tau-a between two total orderings of the same item set (spec §7.2's
    version-chain ordering metric): (concordant_pairs - discordant_pairs) / total_pairs,
    ranging -1.0 (fully reversed) to 1.0 (identical order). Ties in the compared rankings
    can't occur here since both inputs are total orders (permutations) of the same items, so
    tau-a (no tie correction) is the right variant, not tau-b.
    """
    if set(predicted_order) != set(true_order):
        raise ValueError(
            "predicted_order and true_order must contain the same items — "
            f"symmetric difference: {set(predicted_order) ^ set(true_order)}"
        )
    items = list(true_order)
    true_rank = {item: index for index, item in enumerate(true_order)}
    predicted_rank = {item: index for index, item in enumerate(predicted_order)}

    total_pairs = len(items) * (len(items) - 1) // 2
    if total_pairs == 0:
        return 1.0  # 0 or 1 items: trivially "in order"

    concordant = 0
    discordant = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            true_direction = true_rank[items[i]] - true_rank[items[j]]
            predicted_direction = predicted_rank[items[i]] - predicted_rank[items[j]]
            agreement = true_direction * predicted_direction
            if agreement > 0:
                concordant += 1
            elif agreement < 0:
                discordant += 1
    return (concordant - discordant) / total_pairs


def current_commit_sha(repo_root: Path | None = None) -> str:
    """Real `git rev-parse HEAD` — never hardcoded. Returns `"unknown"` (never a fabricated
    hash) if git isn't available or this isn't a repo, so a caller can detect that and refuse
    to report a metric without provenance rather than silently accepting a placeholder."""
    git_exe = shutil.which("git")
    if git_exe is None:
        return "unknown"
    try:
        result = subprocess.run(  # noqa: S603 -- fixed args, not untrusted input
            [git_exe, "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Every metric this layer reports carries its provenance (spec §7.7: "commit + command
    + fixture/gold-set that produced it") — a bare number with no `EvalReport` around it must
    never appear in an ADR, PLAN.md, or CASE_STUDY."""

    metric_name: str
    value: float
    commit_sha: str
    command: str
    fixture_path: str

    def __str__(self) -> str:
        return (
            f"{self.metric_name}={self.value:.4f} "
            f"(commit={self.commit_sha} command={self.command!r} fixture={self.fixture_path})"
        )
