from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from reclaim.ai.eval_harness import current_commit_sha
from reclaim.ai.models import AICluster, AIClusterMember

# Feature 3 (spec §4): "Feedback-Driven Clutter Prioritization." THIS MODULE IS ONLY THE
# FEEDBACK STORE — logging every accept/reject/keep decision with its feature vector, so a
# future LambdaMART ranker has real, time-stamped, commit-keyed training data once enough
# accumulates. GG's explicit instruction: build ONLY this; the ranker itself is a documented,
# label-gated future step (activates at >= 500 real decisions, time-split eval), not built or
# shipped now — there is no data to train it on yet. See `cold_start_priority.py` for the
# transparent, non-ML heuristic that orders the review queue in the meantime.
#
# Same persistence discipline as `labeling.py`'s `LabelStore` (append-only JSONL, never
# rewritten in place — same event-log pattern as `executor.QuarantineManifestEntry`), same
# metric-provenance discipline (`commit_sha` + `schema_version` on every entry, house rule
# 65b). Lives wherever the caller points it; real callers should use a path under
# `data/ai_feedback/`, which `.gitignore` excludes — these are real, personal filesystem
# decisions from GG's own disk and must never be committed, same posture as `data/ai_labels/`.
#
# NO atime anywhere in the feature vector (spec §4 explicit: "No atime dependence (unreliable
# on NTFS)" — `FILE_ATTRIBUTE_...` access-time tracking is commonly disabled system-wide via
# `NtfsDisableLastAccessUpdate`, making atime an unreliable signal even where present).

FeedbackDecisionKind = Literal["accepted", "rejected", "kept"]
# "accepted": the user approved an AI suggestion (this member is/will be removed).
# "rejected": the user declined a suggestion for this specific member (it should NOT have
#   been proposed — the AI's suggestion, not the whole cluster, was wrong about this member).
# "kept": the user explicitly marked this member a permanent keeper (a stronger, deliberate
#   signal than "rejected" — e.g. clicking "always keep this" rather than just dismissing).


@dataclass(frozen=True, slots=True)
class ClusterStats:
    """The cluster-level context around one member's decision — a member's likelihood of
    being "clutter" depends heavily on what kind of cluster it's in and where it sits within
    it, not just its own file attributes."""

    cluster_size: int
    position_in_cluster: int | None  # VERSION_CHAIN only; None for other tracks
    raw_score: float
    score_kind: str
    is_recommended_keep: bool


@dataclass(frozen=True, slots=True)
class SiblingDecisionContext:
    """How the user has already decided about OTHER members of the SAME cluster, at the
    moment this decision is recorded — a real, informative ranking signal (a cluster where 3
    siblings were already accepted for removal is a strong prior the 4th is clutter too) that
    the feature vector alone (this member's own size/ext/mtime) cannot capture. Computed from
    the store's own history, not fabricated."""

    prior_accepted: int
    prior_rejected: int
    prior_kept: int


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """Spec §4's exact field list: "size, ext, path-class, mtime/ctime, cluster stats,
    category, cloud-sync flag, sibling-decision context." Deliberately no atime field at
    all — not omitted by convention, structurally absent from this type."""

    size_bytes: int
    ext: str
    path_class: str
    mtime: float
    ctime: float
    cluster_stats: ClusterStats
    category: str
    cloud_sync_flag: bool
    sibling_decision_context: SiblingDecisionContext


@dataclass(frozen=True, slots=True)
class FeedbackDecision:
    """One logged decision for one cluster member — the ranker's eventual training row."""

    cluster_id: str
    member_path: str
    decision: FeedbackDecisionKind
    track: str
    feature_vector: FeatureVector
    commit_sha: str
    decided_at: float
    schema_version: int = 1


_PATH_CLASS_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("downloads", "downloads"),
    ("desktop", "desktop"),
    ("documents", "documents"),
    ("temp", "temp"),
    ("tmp", "temp"),
)


def classify_path_class(
    path: Path, *, is_cloud_placeholder: bool, git_repo_root: Path | None
) -> str:
    """A single categorical location label — priority order (most to least specific):
    cloud-sync placeholder, git repo, a recognized special-folder segment (downloads/desktop/
    documents/temp), else "other". A path could technically match more than one signal (a
    Downloads folder that's also cloud-synced); this function picks ONE dominant class
    deliberately, since the feature vector wants a single categorical feature here, not a
    multi-hot vector — `cloud_sync_flag` (a separate feature) still carries that signal
    independently even when it's not the winning path_class.
    """
    if is_cloud_placeholder:
        return "cloud_sync_placeholder"
    if git_repo_root is not None:
        return "git_repo"
    parts_lower = {part.lower() for part in path.parts}
    for segment, label in _PATH_CLASS_SEGMENTS:
        if segment in parts_lower:
            return label
    return "other"


def _decision_to_dict(decision: FeedbackDecision) -> dict[str, object]:
    fv = decision.feature_vector
    return {
        "cluster_id": decision.cluster_id,
        "member_path": decision.member_path,
        "decision": decision.decision,
        "track": decision.track,
        "feature_vector": {
            "size_bytes": fv.size_bytes,
            "ext": fv.ext,
            "path_class": fv.path_class,
            "mtime": fv.mtime,
            "ctime": fv.ctime,
            "cluster_stats": {
                "cluster_size": fv.cluster_stats.cluster_size,
                "position_in_cluster": fv.cluster_stats.position_in_cluster,
                "raw_score": fv.cluster_stats.raw_score,
                "score_kind": fv.cluster_stats.score_kind,
                "is_recommended_keep": fv.cluster_stats.is_recommended_keep,
            },
            "category": fv.category,
            "cloud_sync_flag": fv.cloud_sync_flag,
            "sibling_decision_context": {
                "prior_accepted": fv.sibling_decision_context.prior_accepted,
                "prior_rejected": fv.sibling_decision_context.prior_rejected,
                "prior_kept": fv.sibling_decision_context.prior_kept,
            },
        },
        "commit_sha": decision.commit_sha,
        "decided_at": decision.decided_at,
        "schema_version": decision.schema_version,
    }


def _decision_from_dict(data: Any) -> FeedbackDecision:
    fv_data = data["feature_vector"]
    cluster_stats_data = fv_data["cluster_stats"]
    sibling_data = fv_data["sibling_decision_context"]
    return FeedbackDecision(
        cluster_id=data["cluster_id"],
        member_path=data["member_path"],
        decision=data["decision"],
        track=data["track"],
        feature_vector=FeatureVector(
            size_bytes=fv_data["size_bytes"],
            ext=fv_data["ext"],
            path_class=fv_data["path_class"],
            mtime=fv_data["mtime"],
            ctime=fv_data["ctime"],
            cluster_stats=ClusterStats(
                cluster_size=cluster_stats_data["cluster_size"],
                position_in_cluster=cluster_stats_data["position_in_cluster"],
                raw_score=cluster_stats_data["raw_score"],
                score_kind=cluster_stats_data["score_kind"],
                is_recommended_keep=cluster_stats_data["is_recommended_keep"],
            ),
            category=fv_data["category"],
            cloud_sync_flag=fv_data["cloud_sync_flag"],
            sibling_decision_context=SiblingDecisionContext(
                prior_accepted=sibling_data["prior_accepted"],
                prior_rejected=sibling_data["prior_rejected"],
                prior_kept=sibling_data["prior_kept"],
            ),
        ),
        commit_sha=data["commit_sha"],
        decided_at=data["decided_at"],
        schema_version=data.get("schema_version", 1),
    )


class FeedbackStore:
    """Append-only JSONL decision log — see module docstring for the persistence discipline
    this mirrors from `labeling.LabelStore`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, decision: FeedbackDecision) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_decision_to_dict(decision)))
            fh.write("\n")

    def read_all(self) -> list[FeedbackDecision]:
        if not self._path.exists():
            return []
        decisions: list[FeedbackDecision] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                decisions.append(_decision_from_dict(json.loads(stripped)))
        return decisions

    def count(self) -> int:
        """Total logged decisions — the number the label-gated ranker's `>= 500` activation
        threshold (spec §4) is checked against, once that ranker exists."""
        return len(self.read_all())


def _compute_sibling_decision_context(
    store: FeedbackStore, cluster_id: str
) -> SiblingDecisionContext:
    prior_accepted = prior_rejected = prior_kept = 0
    for existing in store.read_all():
        if existing.cluster_id != cluster_id:
            continue
        if existing.decision == "accepted":
            prior_accepted += 1
        elif existing.decision == "rejected":
            prior_rejected += 1
        else:
            prior_kept += 1
    return SiblingDecisionContext(
        prior_accepted=prior_accepted, prior_rejected=prior_rejected, prior_kept=prior_kept
    )


def record_feedback_decision(
    store: FeedbackStore,
    *,
    cluster: AICluster,
    member: AIClusterMember,
    decision: FeedbackDecisionKind,
    is_cloud_placeholder: bool,
    git_repo_root: Path | None,
    now: float | None = None,
    commit_sha: str | None = None,
) -> None:
    """Builds the feature vector from `cluster`/`member` plus caller-supplied filesystem
    context (`is_cloud_placeholder`/`git_repo_root` — the same signals `SafetyValidator`
    already computes per file, not re-derived here) and appends one `FeedbackDecision`.
    `member` must be one of `cluster.members`; the caller is responsible for that invariant
    (mirroring `labeling.record_decision`'s equivalent trust boundary)."""
    sibling_context = _compute_sibling_decision_context(store, cluster.cluster_id)
    feature_vector = FeatureVector(
        size_bytes=member.size_bytes,
        ext=member.path.suffix.lower(),
        path_class=classify_path_class(
            member.path, is_cloud_placeholder=is_cloud_placeholder, git_repo_root=git_repo_root
        ),
        mtime=member.path.stat().st_mtime if member.path.exists() else 0.0,
        ctime=member.path.stat().st_ctime if member.path.exists() else 0.0,
        cluster_stats=ClusterStats(
            cluster_size=len(cluster.members),
            position_in_cluster=member.position,
            raw_score=cluster.raw_score,
            score_kind=cluster.score_kind,
            is_recommended_keep=member.is_recommended_keep,
        ),
        category=cluster.track.value,
        cloud_sync_flag=is_cloud_placeholder,
        sibling_decision_context=sibling_context,
    )
    store.append(
        FeedbackDecision(
            cluster_id=cluster.cluster_id,
            member_path=member.path.as_posix(),
            decision=decision,
            track=cluster.track.value,
            feature_vector=feature_vector,
            commit_sha=commit_sha if commit_sha is not None else current_commit_sha(),
            decided_at=now if now is not None else time.time(),
        )
    )
