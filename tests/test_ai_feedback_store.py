from __future__ import annotations

import os
from pathlib import Path

from reclaim.ai.feedback_store import (
    FeatureVector,
    FeedbackStore,
    classify_path_class,
    record_feedback_decision,
)
from reclaim.ai.models import AICluster, AIClusterMember, AITrack


def test_classify_path_class_cloud_placeholder_takes_priority(tmp_path: Path) -> None:
    path = tmp_path / "Downloads" / "file.jpg"
    assert (
        classify_path_class(path, is_cloud_placeholder=True, git_repo_root=tmp_path)
        == "cloud_sync_placeholder"
    )


def test_classify_path_class_git_repo_takes_priority_over_special_folder(tmp_path: Path) -> None:
    path = tmp_path / "Downloads" / "repo" / "file.py"
    assert (
        classify_path_class(path, is_cloud_placeholder=False, git_repo_root=tmp_path / "repo")
        == "git_repo"
    )


def test_classify_path_class_recognizes_special_folders(tmp_path: Path) -> None:
    assert (
        classify_path_class(
            tmp_path / "Downloads" / "f.zip", is_cloud_placeholder=False, git_repo_root=None
        )
        == "downloads"
    )
    assert (
        classify_path_class(
            tmp_path / "Desktop" / "f.txt", is_cloud_placeholder=False, git_repo_root=None
        )
        == "desktop"
    )
    assert (
        classify_path_class(
            tmp_path / "Documents" / "f.docx", is_cloud_placeholder=False, git_repo_root=None
        )
        == "documents"
    )
    assert (
        classify_path_class(
            tmp_path / "Temp" / "f.tmp", is_cloud_placeholder=False, git_repo_root=None
        )
        == "temp"
    )


def test_classify_path_class_defaults_to_other() -> None:
    # Deliberately NOT built from tmp_path -- pytest's tmp_path lives under the OS's own Temp
    # directory, which would (correctly) match the "temp" path_class itself and defeat the
    # point of this "no recognized segment" test.
    path = Path("C:/Users/someone/Projects/widget/notes.txt")
    assert classify_path_class(path, is_cloud_placeholder=False, git_repo_root=None) == "other"


def test_feature_vector_has_no_atime_field() -> None:
    """Structural proof, not just convention: `FeatureVector`'s dataclass fields never
    include atime -- spec §4 explicit: "No atime dependence (unreliable on NTFS)"."""
    field_names = set(FeatureVector.__dataclass_fields__)
    assert not any("atime" in name for name in field_names), field_names


def test_feedback_store_append_and_read_round_trips(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    member_a = tmp_path / "shot_a.png"
    member_b = tmp_path / "shot_b.png"
    member_a.write_bytes(b"x")
    member_b.write_bytes(b"x")
    now = 1_700_000_000.0
    os.utime(member_a, (now, now))
    os.utime(member_b, (now, now))

    cluster = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(
            AIClusterMember(path=member_a, size_bytes=1, is_recommended_keep=True),
            AIClusterMember(path=member_b, size_bytes=1),
        ),
        raw_score=3.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )

    record_feedback_decision(
        store,
        cluster=cluster,
        member=cluster.members[1],
        decision="accepted",
        is_cloud_placeholder=False,
        git_repo_root=None,
        now=now,
        commit_sha="abc123",
    )

    decisions = store.read_all()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.cluster_id == "burst-1"
    assert decision.decision == "accepted"
    assert decision.track == "screenshot_burst"
    assert decision.commit_sha == "abc123"
    assert decision.decided_at == now
    assert decision.schema_version == 1
    assert decision.feature_vector.size_bytes == 1
    assert decision.feature_vector.cluster_stats.cluster_size == 2
    assert decision.feature_vector.cluster_stats.is_recommended_keep is False
    assert decision.feature_vector.cloud_sync_flag is False
    assert store.count() == 1


def test_feedback_store_sibling_decision_context_reflects_prior_decisions(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    paths = [tmp_path / f"shot_{i}.png" for i in range(3)]
    for path in paths:
        path.write_bytes(b"x")
    cluster = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=tuple(AIClusterMember(path=p, size_bytes=1) for p in paths),
        raw_score=3.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )

    record_feedback_decision(
        store,
        cluster=cluster,
        member=cluster.members[0],
        decision="accepted",
        is_cloud_placeholder=False,
        git_repo_root=None,
    )
    record_feedback_decision(
        store,
        cluster=cluster,
        member=cluster.members[1],
        decision="rejected",
        is_cloud_placeholder=False,
        git_repo_root=None,
    )
    record_feedback_decision(
        store,
        cluster=cluster,
        member=cluster.members[2],
        decision="kept",
        is_cloud_placeholder=False,
        git_repo_root=None,
    )

    decisions = store.read_all()
    # The 3rd decision (member index 2) should see both prior siblings' decisions.
    third = decisions[2]
    assert third.feature_vector.sibling_decision_context.prior_accepted == 1
    assert third.feature_vector.sibling_decision_context.prior_rejected == 1
    assert third.feature_vector.sibling_decision_context.prior_kept == 0
    # The 1st decision should see no prior siblings at all.
    first = decisions[0]
    assert first.feature_vector.sibling_decision_context.prior_accepted == 0
    assert first.feature_vector.sibling_decision_context.prior_rejected == 0
    assert first.feature_vector.sibling_decision_context.prior_kept == 0


def test_feedback_store_read_all_returns_empty_list_for_missing_file(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "does_not_exist.jsonl")
    assert store.read_all() == []
    assert store.count() == 0


def test_feedback_store_decisions_from_different_clusters_dont_cross_contaminate(
    tmp_path: Path,
) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    path_a.write_bytes(b"x")
    path_b.write_bytes(b"x")
    cluster_1 = AICluster(
        cluster_id="burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(AIClusterMember(path=path_a, size_bytes=1),),
        raw_score=1.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )
    cluster_2 = AICluster(
        cluster_id="burst-2",
        track=AITrack.SCREENSHOT_BURST,
        members=(AIClusterMember(path=path_b, size_bytes=1),),
        raw_score=1.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test",
    )
    record_feedback_decision(
        store,
        cluster=cluster_1,
        member=cluster_1.members[0],
        decision="accepted",
        is_cloud_placeholder=False,
        git_repo_root=None,
    )
    record_feedback_decision(
        store,
        cluster=cluster_2,
        member=cluster_2.members[0],
        decision="kept",
        is_cloud_placeholder=False,
        git_repo_root=None,
    )
    decisions = store.read_all()
    second = decisions[1]
    assert second.cluster_id == "burst-2"
    assert second.feature_vector.sibling_decision_context.prior_accepted == 0
    assert second.feature_vector.sibling_decision_context.prior_kept == 0
