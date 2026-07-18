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
class OperatingPoint:
    threshold: float
    precision: float
    recall: float
    is_provisional: bool
    source_description: str


def select_operating_point(
    curve: Sequence[PRPoint], *, target_precision: float, source_description: str
) -> OperatingPoint | None:
    """Picks the highest-recall point on `curve` whose precision >= `target_precision`, or
    `None` if no point clears the target (callers MUST handle `None` explicitly — never fall
    back to a lower-precision point silently, per spec §7.3's "chosen from the PR curve at a
    target precision — NOT hand-set").

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
    return OperatingPoint(
        threshold=best.threshold,
        precision=best.precision,
        recall=best.recall,
        is_provisional=True,
        source_description=source_description,
    )


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
