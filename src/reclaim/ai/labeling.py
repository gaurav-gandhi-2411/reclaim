from __future__ import annotations

import json
import random
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reclaim.ai.eval_harness import current_commit_sha
from reclaim.ai.image_similarity import build_near_identical_clusters
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.phash import ImageHashRecord, compute_image_hashes, hamming_distance
from reclaim.safety import SafetyValidator

# Gold-set labeling tool (spec §7.1 / the explicit autonomy-boundary instruction: "build a
# gold-set labeling tool ... so GG can label a few hundred real image/doc pairs + keep-best
# choices from his own disk. Do NOT fabricate a gold set"). This module is the tool's
# non-UI core: candidate discovery (reusing the real Feature 1a pipeline, not a separate
# reimplementation) and the append-only local label store. `labeling_app.py` wraps this in a
# loopback-only FastAPI review UI; `scripts/ai_label_tool.py` is the CLI launcher.
#
# Nothing here has been run against a real gold set — this delivers the tool, not labels.
#
# STRATIFIED SAMPLING (see ADR-0014): operating-point selection needs labeled examples on
# BOTH sides of the decision boundary — a gold set of only true-duplicates can't locate a
# threshold. `discover_label_candidates` therefore proposes candidates from THREE strata, not
# just the already-clustered near-duplicate pool:
#   - "near_duplicate": the existing Feature 1a clustering pipeline's own output (multi-member
#     groups — needed for keep-best labeling, which requires 2+ genuinely-similar images).
#   - "boundary": pairwise-sampled pairs near the decision threshold (hard cases where the
#     current threshold's correctness is most uncertain and most informative to label).
#   - "negative_control": pairwise-sampled pairs from clearly-distant images (true-negative
#     ground truth — proves the eventual threshold doesn't need to be looser than it is).

LabelDecisionKind = Literal["confirmed_near_duplicates", "rejected_not_duplicates", "skipped"]
SampleStratum = Literal["near_duplicate", "boundary", "negative_control"]

# Fixed, human-selected reason codes for a keep-best choice — never auto-derived from the
# classical scorer's own sub-scores, so the resulting label is an independent signal the
# scorer's eval can be checked against (spec §7.2's diagnosability requirement), not a
# tautology. "other" exists as an escape hatch but deliberately has no free-text companion —
# free-text risks capturing something GG didn't intend to have written to a file.
KEEP_REASON_OPTIONS: tuple[str, ...] = (
    "sharper",
    "higher_resolution",
    "better_exposure",
    "better_framing_or_content",
    "other",
)

# Distance bins for pairwise stratified sampling (phash/dhash are 64-bit, so distance ranges
# 0-64). Deliberately NOT the same thing as `max_hamming_distance` (the cluster-discovery
# threshold, default 15) — "boundary" straddles both sides of that threshold on purpose, and
# "negative_control" is far enough that these pairs should almost certainly be labeled
# non-duplicates, giving clean true-negative ground truth.
_BOUNDARY_MIN_DISTANCE = 11
_BOUNDARY_MAX_DISTANCE = 25
_NEGATIVE_CONTROL_MIN_DISTANCE = 26

# Volume/balance targets (ADR-0014) — not enforced by the tool (GG can stop whenever he
# chooses), but tracked and displayed so a labeling session doesn't silently collapse into
# "400 confirmed positives, 3 rejections" (a gold set that shape cannot locate a threshold).
DEFAULT_TARGET_TOTAL = 300
DEFAULT_TARGET_PER_STRATUM_MINIMUM = 40


@dataclass(frozen=True, slots=True)
class LabelDecision:
    """One human labeling decision for one candidate — the ground truth Feature 1a's operating
    point will eventually be selected from (ADR-0012's PROVISIONAL threshold stops being
    provisional once enough of these exist and a new ADR records the real PR curve).

    `commit_sha`/`schema_version` give every decision the same metric-provenance discipline
    (house rule 65b) the rest of this project holds itself to — a gold-set file is only a
    "stable artifact keyed to a commit" if each entry actually records which commit's
    candidate-generation code proposed it.
    """

    cluster_id: str
    decision: LabelDecisionKind
    # Which member GG identifies as the one to keep — only meaningful for
    # "confirmed_near_duplicates"; None for a rejected or skipped candidate.
    keep_path: str | None
    # Why — only meaningful alongside a non-None keep_path. Never auto-derived; see
    # KEEP_REASON_OPTIONS.
    keep_reasons: tuple[str, ...]
    member_paths: tuple[str, ...]
    stratum: SampleStratum
    raw_score: float
    score_kind: str
    commit_sha: str
    labeled_at: float
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class LabelCandidate:
    """One candidate the labeling UI presents: an `AICluster` (the shared, safety-audited core
    AI-layer type — Feature 1a's shipped pipeline uses the same type) plus the sampling
    stratum it was drawn from. The stratum is deliberately kept OUTSIDE `AICluster` itself —
    that type gains no labeling-tool-specific fields."""

    cluster: AICluster
    stratum: SampleStratum


def _bin_for_distance(distance: int) -> SampleStratum:
    if distance <= _BOUNDARY_MAX_DISTANCE and distance >= _BOUNDARY_MIN_DISTANCE:
        return "boundary"
    if distance >= _NEGATIVE_CONTROL_MIN_DISTANCE:
        return "negative_control"
    # distance < _BOUNDARY_MIN_DISTANCE: already the clustered pool's territory
    return "near_duplicate"


def _cluster_pair_keys(clusters: Sequence[AICluster]) -> set[frozenset[Path]]:
    """Every within-cluster member pair, as an unordered key — used to avoid re-presenting a
    pair as a "boundary"/"negative_control" candidate when it's already covered by a
    near_duplicate cluster candidate."""
    keys: set[frozenset[Path]] = set()
    for cluster in clusters:
        members = cluster.members
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                keys.add(frozenset({members[i].path, members[j].path}))
    return keys


def _stratified_pair_candidates(
    records: Sequence[ImageHashRecord],
    *,
    exclude_pairs: set[frozenset[Path]],
    hash_kind: str,
    per_stratum: int,
    seed: int,
) -> list[LabelCandidate]:
    """Independent pairwise sampling for the "boundary"/"negative_control" strata — deliberately
    NOT built from the transitive near-dup clustering (which only ever proposes pairs already
    within one loose threshold and would structurally never surface a hard negative or a
    just-outside-the-boundary case). O(n^2) pairwise comparison over `records`; the caller
    (`discover_label_candidates`) is responsible for bounding `records`' size for large photo
    collections — see `max_images_for_boundary_sampling`.
    """
    hashes = [r.phash_hex if hash_kind == "phash" else r.dhash_hex for r in records]
    by_stratum: dict[SampleStratum, list[tuple[int, ImageHashRecord, ImageHashRecord]]] = (
        defaultdict(list)
    )
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            pair_key = frozenset({records[i].path, records[j].path})
            if pair_key in exclude_pairs:
                continue
            distance = hamming_distance(hashes[i], hashes[j])
            stratum = _bin_for_distance(distance)
            if stratum == "near_duplicate":
                continue  # already the cluster-based discovery path's territory
            by_stratum[stratum].append((distance, records[i], records[j]))

    rng = random.Random(seed)  # noqa: S311 -- deterministic candidate sampling, not a security context
    candidates: list[LabelCandidate] = []
    for stratum in ("boundary", "negative_control"):
        pool = list(by_stratum.get(stratum, ()))
        rng.shuffle(pool)
        for index, (distance, record_a, record_b) in enumerate(pool[:per_stratum]):
            cluster = AICluster(
                cluster_id=f"{stratum}-{index}",
                track=AITrack.NEAR_IDENTICAL_IMAGE,
                members=(
                    AIClusterMember(path=record_a.path, size_bytes=record_a.size_bytes),
                    AIClusterMember(path=record_b.path, size_bytes=record_b.size_bytes),
                ),
                raw_score=float(distance),
                score_kind="hamming_distance",
                rationale=_stratum_rationale(stratum, distance),
            )
            candidates.append(LabelCandidate(cluster=cluster, stratum=stratum))
    return candidates


def _stratum_rationale(stratum: SampleStratum, distance: int) -> str:
    if stratum == "boundary":
        return (
            f"BOUNDARY sample: Hamming distance {distance} — near the decision threshold. "
            "This pair is deliberately uncertain; your label here is the most informative "
            "kind for locating the real operating point."
        )
    return (
        f"NEGATIVE CONTROL sample: Hamming distance {distance} — these should almost "
        "certainly NOT be duplicates. Confirming that expectation (by rejecting) gives clean "
        "true-negative ground truth."
    )


def discover_label_candidates(
    root: Path,
    *,
    safety: SafetyValidator,
    max_hamming_distance: int = 15,
    per_stratum: int = 60,
    max_images_for_boundary_sampling: int = 800,
    seed: int = 42,
) -> list[LabelCandidate]:
    """Proposes candidates for GG to review, across all three strata (see module docstring).

    The "near_duplicate" stratum reuses the exact Feature 1a pipeline
    (`image_similarity.build_near_identical_clusters`), not a separate implementation, so what
    GG labels there is genuinely what the shipped feature would propose. `max_hamming_distance`
    defaults looser than ADR-0012's CI gate (10) — deliberately over-inclusive so GG can reject
    borderline cases inside that pool too.

    The "boundary"/"negative_control" strata are independent pairwise samples (up to
    `per_stratum` each, deterministic given `seed`) — the labeled examples on both sides of the
    decision boundary that PR-curve-based operating-point selection actually needs. Pairwise
    sampling is O(n^2); `max_images_for_boundary_sampling` deterministically subsamples the
    scanned image set before that step only (the near_duplicate cluster discovery still runs
    against the FULL scanned set) so a large photo collection doesn't hang the tool.
    """
    image_paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    ]

    near_dup_clusters = build_near_identical_clusters(
        image_paths, safety=safety, max_hamming_distance=max_hamming_distance
    )
    candidates: list[LabelCandidate] = [
        LabelCandidate(cluster=cluster, stratum="near_duplicate") for cluster in near_dup_clusters
    ]

    hash_records: list[ImageHashRecord] = []
    for path in image_paths:
        record = compute_image_hashes(path)
        if record is not None:
            hash_records.append(record)
    if len(hash_records) > max_images_for_boundary_sampling:
        rng = random.Random(seed)  # noqa: S311 -- deterministic candidate sampling
        hash_records = rng.sample(hash_records, max_images_for_boundary_sampling)

    exclude_pairs = _cluster_pair_keys(near_dup_clusters)
    candidates.extend(
        _stratified_pair_candidates(
            hash_records,
            exclude_pairs=exclude_pairs,
            hash_kind="phash",
            per_stratum=per_stratum,
            seed=seed,
        )
    )
    return candidates


class LabelStore:
    """Append-only JSONL label log — same event-log pattern as
    `executor.QuarantineManifestEntry` (append, never rewrite in place). Lives wherever the
    caller points it; `scripts/ai_label_tool.py` defaults to `data/ai_labels/gold_labels.jsonl`,
    which `.gitignore` excludes — these paths are real, personal filesystem paths from GG's
    own disk and must never be committed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, decision: LabelDecision) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "cluster_id": decision.cluster_id,
                        "decision": decision.decision,
                        "keep_path": decision.keep_path,
                        "keep_reasons": list(decision.keep_reasons),
                        "member_paths": list(decision.member_paths),
                        "stratum": decision.stratum,
                        "raw_score": decision.raw_score,
                        "score_kind": decision.score_kind,
                        "commit_sha": decision.commit_sha,
                        "labeled_at": decision.labeled_at,
                        "schema_version": decision.schema_version,
                    }
                )
            )
            fh.write("\n")

    def read_all(self) -> list[LabelDecision]:
        if not self._path.exists():
            return []
        decisions: list[LabelDecision] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                data = json.loads(stripped)
                decisions.append(
                    LabelDecision(
                        cluster_id=data["cluster_id"],
                        decision=data["decision"],
                        keep_path=data["keep_path"],
                        keep_reasons=tuple(data.get("keep_reasons", ())),
                        member_paths=tuple(data["member_paths"]),
                        stratum=data.get("stratum", "near_duplicate"),
                        raw_score=data.get("raw_score", 0.0),
                        score_kind=data.get("score_kind", "hamming_distance"),
                        commit_sha=data.get("commit_sha", "unknown"),
                        labeled_at=data["labeled_at"],
                        schema_version=data.get("schema_version", 1),
                    )
                )
        return decisions

    def labeled_cluster_ids(self) -> set[str]:
        """Folds to the LATEST decision per cluster_id (same "last line wins" event-log fold
        the deterministic engine's manifest reader uses) — re-launching the tool skips
        already-labeled clusters rather than re-asking, but a cluster can still be
        re-labeled by deliberately labeling it again (the new decision simply appends)."""
        return {decision.cluster_id for decision in self.read_all()}


@dataclass(frozen=True, slots=True)
class LabelingProgress:
    """Live tally shown in the UI — the balance check the build brief asked for: a gold set of
    400 confirmed positives and 3 rejections cannot locate a threshold, and this makes that
    imbalance visible WHILE labeling, not discoverable only after the fact."""

    total_labeled: int
    target_total: int
    counts_by_stratum: dict[SampleStratum, int]
    target_per_stratum_minimum: int

    @property
    def meets_targets(self) -> bool:
        return self.total_labeled >= self.target_total and all(
            self.counts_by_stratum.get(stratum, 0) >= self.target_per_stratum_minimum
            for stratum in ("boundary", "negative_control")
        )


def compute_progress(
    store: LabelStore,
    *,
    target_total: int = DEFAULT_TARGET_TOTAL,
    target_per_stratum_minimum: int = DEFAULT_TARGET_PER_STRATUM_MINIMUM,
) -> LabelingProgress:
    # Folded (latest-per-cluster_id), matching labeled_cluster_ids' semantics — a cluster
    # re-labeled twice counts once, under its most recent stratum/decision.
    latest_by_cluster: dict[str, LabelDecision] = {}
    for decision in store.read_all():
        latest_by_cluster[decision.cluster_id] = decision

    counts_by_stratum: dict[SampleStratum, int] = defaultdict(int)
    for decision in latest_by_cluster.values():
        counts_by_stratum[decision.stratum] += 1

    return LabelingProgress(
        total_labeled=len(latest_by_cluster),
        target_total=target_total,
        counts_by_stratum=dict(counts_by_stratum),
        target_per_stratum_minimum=target_per_stratum_minimum,
    )


def record_decision(
    store: LabelStore,
    candidate: LabelCandidate,
    *,
    decision: LabelDecisionKind,
    keep_path: str | None,
    keep_reasons: tuple[str, ...] = (),
    now: float | None = None,
    commit_sha: str | None = None,
) -> None:
    cluster = candidate.cluster
    store.append(
        LabelDecision(
            cluster_id=cluster.cluster_id,
            decision=decision,
            keep_path=keep_path,
            keep_reasons=keep_reasons,
            member_paths=tuple(member.path.as_posix() for member in cluster.members),
            stratum=candidate.stratum,
            raw_score=cluster.raw_score,
            score_kind=cluster.score_kind,
            commit_sha=commit_sha if commit_sha is not None else current_commit_sha(),
            labeled_at=now if now is not None else time.time(),
        )
    )
