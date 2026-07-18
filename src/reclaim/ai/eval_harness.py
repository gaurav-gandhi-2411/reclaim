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
