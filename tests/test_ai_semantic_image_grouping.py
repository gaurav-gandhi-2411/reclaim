from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

from reclaim.ai.image_embeddings import ImageEmbedding, compute_image_embedding
from reclaim.ai.models import AITrack
from reclaim.ai.semantic_image_grouping import (
    build_semantic_image_clusters,
    group_by_semantic_similarity,
)
from reclaim.config import Config
from reclaim.safety import SafetyValidator


def _make_image(
    path: Path, *, color: tuple[int, int, int], shape_color: tuple[int, int, int]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (224, 224), color=color)
    draw = ImageDraw.Draw(img)
    draw.ellipse([40, 40, 180, 180], fill=shape_color)
    img.save(path, format="PNG")


def test_group_by_semantic_similarity_returns_empty_for_fewer_than_two_embeddings() -> None:
    embedding = ImageEmbedding(path=Path("a.jpg"), vector=(1.0, 0.0))
    assert group_by_semantic_similarity([]) == []
    assert group_by_semantic_similarity([embedding]) == []


def test_group_by_semantic_similarity_groups_similar_drops_dissimilar() -> None:
    # Two near-identical vectors (high cosine similarity) + one orthogonal vector.
    close_a = ImageEmbedding(path=Path("a.jpg"), vector=(1.0, 0.01, 0.0))
    close_b = ImageEmbedding(path=Path("b.jpg"), vector=(0.99, 0.02, 0.0))
    far = ImageEmbedding(path=Path("c.jpg"), vector=(0.0, 0.0, 1.0))

    groups = group_by_semantic_similarity([close_a, close_b, far], similarity_threshold=0.9)
    assert len(groups) == 1
    assert set(groups[0].members) == {Path("a.jpg"), Path("b.jpg")}


def test_group_by_semantic_similarity_respects_threshold() -> None:
    a = ImageEmbedding(path=Path("a.jpg"), vector=(1.0, 0.0))
    b = ImageEmbedding(path=Path("b.jpg"), vector=(0.5, 0.866))  # 60 degrees apart, cos=0.5
    assert group_by_semantic_similarity([a, b], similarity_threshold=0.9) == []
    assert len(group_by_semantic_similarity([a, b], similarity_threshold=0.4)) == 1


def test_build_semantic_image_clusters_end_to_end(tmp_path: Path) -> None:
    beach1 = tmp_path / "beach1.png"
    beach2 = tmp_path / "beach2.png"
    forest = tmp_path / "forest.png"
    _make_image(beach1, color=(135, 206, 235), shape_color=(255, 220, 100))
    _make_image(beach2, color=(130, 200, 230), shape_color=(250, 215, 105))
    _make_image(forest, color=(20, 60, 20), shape_color=(80, 40, 10))
    now = 1_700_000_000.0
    for path in (beach1, beach2, forest):
        os.utime(path, (now, now))

    safety = SafetyValidator(Config())
    clusters = build_semantic_image_clusters(
        [beach1, beach2, forest], safety=safety, similarity_threshold=0.85
    )

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.track == AITrack.SEMANTIC_IMAGE
    assert {m.path for m in cluster.members} == {beach1, beach2}
    assert cluster.suggests_deletion is False
    assert all(not m.is_recommended_keep for m in cluster.members)


def test_build_semantic_image_clusters_uses_the_embedding_cache(tmp_path: Path) -> None:
    beach1 = tmp_path / "beach1.png"
    beach2 = tmp_path / "beach2.png"
    _make_image(beach1, color=(135, 206, 235), shape_color=(255, 220, 100))
    _make_image(beach2, color=(130, 200, 230), shape_color=(250, 215, 105))
    now = 1_700_000_000.0
    for path in (beach1, beach2):
        os.utime(path, (now, now))

    safety = SafetyValidator(Config())
    cache_path = tmp_path / "embeddings.sqlite3"

    first = build_semantic_image_clusters(
        [beach1, beach2],
        safety=safety,
        embedding_cache_path=cache_path,
        similarity_threshold=0.85,
    )
    assert cache_path.exists()
    # Second call should hit the cache (same result, no error) -- proves the cache path is
    # actually wired through the orchestration function, not just accepted and ignored.
    second = build_semantic_image_clusters(
        [beach1, beach2],
        safety=safety,
        embedding_cache_path=cache_path,
        similarity_threshold=0.85,
    )
    assert len(first) == len(second) == 1


def test_build_semantic_image_clusters_no_groups_returns_empty(tmp_path: Path) -> None:
    only_one = tmp_path / "solo.png"
    _make_image(only_one, color=(100, 150, 200), shape_color=(255, 100, 50))
    now = 1_700_000_000.0
    os.utime(only_one, (now, now))

    safety = SafetyValidator(Config())
    assert build_semantic_image_clusters([only_one], safety=safety) == []


def test_compute_image_embedding_smoke(tmp_path: Path) -> None:
    """Sanity check the real embedding function this whole test file's fixtures rely on
    (separately from test_ai_image_embeddings.py's own dedicated coverage) actually returns
    something before trusting the grouping tests above."""
    path = tmp_path / "photo.png"
    _make_image(path, color=(1, 2, 3), shape_color=(4, 5, 6))
    embedding = compute_image_embedding(path)
    assert embedding is not None
    assert len(embedding.vector) > 0
