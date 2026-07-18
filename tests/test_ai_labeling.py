from __future__ import annotations

from pathlib import Path

from reclaim.ai.labeling import LabelStore, discover_label_candidates, record_decision
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator


def _cluster(cluster_id: str = "c1") -> AICluster:
    keep = AIClusterMember(path=Path("a.jpg"), size_bytes=100, is_recommended_keep=True)
    drop = AIClusterMember(path=Path("b.jpg"), size_bytes=100)
    return AICluster(
        cluster_id=cluster_id,
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=2.0,
        score_kind="hamming_distance",
        rationale="test",
    )


def test_label_store_append_and_read_round_trips(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(
        store, _cluster(), decision="confirmed_near_duplicates", keep_path="a.jpg", now=1000.0
    )

    decisions = store.read_all()
    assert len(decisions) == 1
    assert decisions[0].cluster_id == "c1"
    assert decisions[0].decision == "confirmed_near_duplicates"
    assert decisions[0].keep_path == "a.jpg"
    assert decisions[0].member_paths == ("a.jpg", "b.jpg")
    assert decisions[0].labeled_at == 1000.0


def test_label_store_read_all_on_missing_file_returns_empty(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "does_not_exist.jsonl")
    assert store.read_all() == []
    assert store.labeled_cluster_ids() == set()


def test_label_store_is_append_only_event_log(tmp_path: Path) -> None:
    store = LabelStore(tmp_path / "labels.jsonl")
    record_decision(store, _cluster("c1"), decision="skipped", keep_path=None, now=1.0)
    record_decision(
        store, _cluster("c2"), decision="rejected_not_duplicates", keep_path=None, now=2.0
    )
    record_decision(
        store, _cluster("c1"), decision="confirmed_near_duplicates", keep_path="a.jpg", now=3.0
    )

    assert len(store.read_all()) == 3  # every append preserved, nothing rewritten in place
    assert store.labeled_cluster_ids() == {"c1", "c2"}


def test_label_store_file_never_created_until_first_append(tmp_path: Path) -> None:
    label_path = tmp_path / "nested" / "labels.jsonl"
    store = LabelStore(label_path)
    assert not label_path.exists()
    record_decision(store, _cluster(), decision="skipped", keep_path=None)
    assert label_path.exists()


def test_discover_label_candidates_filters_protected_paths(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    def _make(path: Path, seed_color: tuple[int, int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (128, 128), color=seed_color)
        draw = ImageDraw.Draw(img)
        draw.ellipse(
            [10, 10, 100, 100], fill=(255 - seed_color[0], 255 - seed_color[1], 255 - seed_color[2])
        )
        img.save(path, format="JPEG", quality=90)

    root = tmp_path / "photos"
    _make(root / "a.jpg", (100, 100, 100))
    _make(root / "a_copy.jpg", (102, 100, 100))  # near-identical to a.jpg

    protected = root / "Windows"
    _make(protected / "b.jpg", (100, 100, 100))  # would also cluster with a.jpg, but protected

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected.as_posix()}/*"]))
    )

    clusters = discover_label_candidates(root, safety=safety, max_hamming_distance=10)

    all_paths = {member.path for cluster in clusters for member in cluster.members}
    assert (protected / "b.jpg") not in all_paths
    assert (root / "a.jpg") in all_paths
    assert (root / "a_copy.jpg") in all_paths


def test_discover_label_candidates_empty_directory_returns_no_clusters(tmp_path: Path) -> None:
    safety = SafetyValidator(Config())
    assert discover_label_candidates(tmp_path, safety=safety) == []
