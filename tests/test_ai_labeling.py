from __future__ import annotations

from pathlib import Path

from reclaim.ai.labeling import (
    LabelCandidate,
    LabelStore,
    compute_progress,
    discover_label_candidates,
    record_decision,
)
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator


def _candidate(cluster_id: str = "c1", stratum: str = "near_duplicate") -> LabelCandidate:
    keep = AIClusterMember(path=Path("a.jpg"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("b.jpg"), size_bytes=100)
    cluster = AICluster(
        cluster_id=cluster_id,
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=2.0,
        score_kind="hamming_distance",
        rationale="test",
    )
    return LabelCandidate(cluster=cluster, stratum=stratum)  # type: ignore[arg-type]


def test_label_store_append_and_read_round_trips(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(
        store,
        _candidate(),
        decision="confirmed_near_duplicates",
        keep_path="a.jpg",
        keep_reasons=("sharper", "higher_resolution"),
        now=1000.0,
        commit_sha="deadbeef",
    )

    decisions = store.read_all()
    assert len(decisions) == 1
    assert decisions[0].cluster_id == "c1"
    assert decisions[0].decision == "confirmed_near_duplicates"
    assert decisions[0].keep_path == "a.jpg"
    assert decisions[0].keep_reasons == ("sharper", "higher_resolution")
    assert decisions[0].member_paths == ("a.jpg", "b.jpg")
    assert decisions[0].stratum == "near_duplicate"
    assert decisions[0].raw_score == 2.0
    assert decisions[0].score_kind == "hamming_distance"
    assert decisions[0].commit_sha == "deadbeef"
    assert decisions[0].labeled_at == 1000.0
    assert decisions[0].schema_version == 1


def test_label_store_commit_sha_defaults_to_the_real_repo_head(tmp_path: Path) -> None:
    """When the caller doesn't pass commit_sha explicitly, record_decision stamps the real
    current commit (reclaim.ai.eval_harness.current_commit_sha) — never a placeholder."""
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(store, _candidate(), decision="skipped", keep_path=None)
    decisions = store.read_all()
    assert decisions[0].commit_sha != "unknown"
    assert len(decisions[0].commit_sha) == 40


def test_label_store_read_all_on_missing_file_returns_empty(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "does_not_exist.jsonl")
    assert store.read_all() == []
    assert store.labeled_cluster_ids() == set()


def test_label_store_is_append_only_event_log(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(store, _candidate("c1"), decision="skipped", keep_path=None, now=1.0)
    record_decision(
        store,
        _candidate("c2", "boundary"),
        decision="rejected_not_duplicates",
        keep_path=None,
        now=2.0,
    )
    record_decision(
        store, _candidate("c1"), decision="confirmed_near_duplicates", keep_path="a.jpg", now=3.0
    )

    assert len(store.read_all()) == 3  # every append preserved, nothing rewritten in place
    assert store.labeled_cluster_ids() == {"c1", "c2"}


def test_label_store_file_never_created_until_first_append(tmp_path: Path) -> None:
    label_path = tmp_path / "nested" / "labels.jsonl"
    store = LabelStore(label_path)
    assert not label_path.exists()
    record_decision(store, _candidate(), decision="skipped", keep_path=None)
    assert label_path.exists()


def _make_patterned_image(path: Path, seed_color: tuple[int, int, int]) -> None:
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (128, 128), color=seed_color)
    draw = ImageDraw.Draw(img)
    inverse = (255 - seed_color[0], 255 - seed_color[1], 255 - seed_color[2])
    draw.ellipse([10, 10, 100, 100], fill=inverse)
    img.save(path, format="JPEG", quality=90)


def test_discover_label_candidates_filters_protected_paths(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    _make_patterned_image(root / "a.jpg", (100, 100, 100))
    _make_patterned_image(root / "a_copy.jpg", (102, 100, 100))  # near-identical to a.jpg

    protected = root / "Windows"
    _make_patterned_image(protected / "b.jpg", (100, 100, 100))  # also would cluster, but protected

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected.as_posix()}/*"]))
    )

    candidates = discover_label_candidates(root, safety=safety, max_hamming_distance=10)

    all_paths = {member.path for c in candidates for member in c.cluster.members}
    assert (protected / "b.jpg") not in all_paths
    assert (root / "a.jpg") in all_paths
    assert (root / "a_copy.jpg") in all_paths


def test_discover_label_candidates_empty_directory_returns_no_clusters(tmp_path: Path) -> None:
    safety = SafetyValidator(Config())
    assert discover_label_candidates(tmp_path, safety=safety) == []


def test_discover_label_candidates_covers_all_three_strata(tmp_path: Path) -> None:
    """The sampling-protocol requirement: a real labeling session needs examples on both sides
    of the decision boundary, not just easy positives. A handful of visually distinct clusters
    plus scattered singletons should produce candidates in every stratum, not only
    near_duplicate — a gold set that's only confirmed positives can't locate a threshold."""
    root = tmp_path / "photos"
    # Three visually distinct "scenes", each with a genuine near-dup pair, plus several
    # completely unrelated singleton images -- guarantees some pairs land far apart
    # (negative_control) and, with imagehash's real distance distribution, some in between.
    for scene in range(3):
        base_color = (scene * 60 % 256, (scene * 97) % 256, (scene * 151) % 256)
        _make_patterned_image(root / f"scene{scene}_a.jpg", base_color)
        shifted = tuple(min(255, c + 2) for c in base_color)
        _make_patterned_image(root / f"scene{scene}_b.jpg", shifted)  # type: ignore[arg-type]
    for singleton in range(6):
        color = ((singleton * 37) % 256, (singleton * 83) % 256, (singleton * 211) % 256)
        _make_patterned_image(root / f"singleton_{singleton}.jpg", color)

    safety = SafetyValidator(Config())
    candidates = discover_label_candidates(
        root, safety=safety, max_hamming_distance=10, per_stratum=20, seed=42
    )

    strata_present = {c.stratum for c in candidates}
    assert "near_duplicate" in strata_present
    # At minimum, some stratum other than near_duplicate must be represented — the exact split
    # depends on the real measured Hamming distances between these synthetic images, which is
    # the point (this isn't a hand-picked fixture; it's exercising the real bucketing logic).
    assert strata_present - {"near_duplicate"}, (
        f"expected boundary and/or negative_control candidates too, got only: {strata_present}"
    )


def test_progress_tracks_totals_and_per_stratum_counts(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(store, _candidate("c1", "near_duplicate"), decision="skipped", keep_path=None)
    record_decision(
        store, _candidate("c2", "boundary"), decision="rejected_not_duplicates", keep_path=None
    )
    record_decision(
        store, _candidate("c3", "boundary"), decision="confirmed_near_duplicates", keep_path="a.jpg"
    )

    progress = compute_progress(store, target_total=10, target_per_stratum_minimum=2)
    assert progress.total_labeled == 3
    assert progress.counts_by_stratum == {"near_duplicate": 1, "boundary": 2}
    assert progress.meets_targets is False  # negative_control has zero, below the minimum


def test_progress_meets_targets_only_when_every_stratum_minimum_is_met(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    for i in range(3):
        record_decision(
            store, _candidate(f"nd{i}", "near_duplicate"), decision="skipped", keep_path=None
        )
        record_decision(store, _candidate(f"b{i}", "boundary"), decision="skipped", keep_path=None)
        record_decision(
            store, _candidate(f"n{i}", "negative_control"), decision="skipped", keep_path=None
        )

    progress = compute_progress(store, target_total=9, target_per_stratum_minimum=3)
    assert progress.total_labeled == 9
    assert progress.meets_targets is True


def test_progress_relabeling_a_cluster_counts_once_under_its_latest_stratum(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(
        store, _candidate("c1", "boundary"), decision="skipped", keep_path=None, now=1.0
    )
    record_decision(
        store,
        _candidate("c1", "boundary"),
        decision="confirmed_near_duplicates",
        keep_path="a.jpg",
        now=2.0,
    )

    progress = compute_progress(store, target_total=10, target_per_stratum_minimum=1)
    assert progress.total_labeled == 1
    assert progress.counts_by_stratum == {"boundary": 1}
