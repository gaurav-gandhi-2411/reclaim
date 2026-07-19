from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from reclaim.ai.phash import cluster_by_hamming_distance, compute_image_hashes, hamming_distance


def _make_image(
    path: Path, *, color: tuple[int, int, int], size: tuple[int, int] = (64, 64)
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path, format="PNG")


def _make_patterned_image(
    path: Path, *, seed_color: tuple[int, int, int], size: tuple[int, int] = (64, 64)
) -> None:
    """A solid color alone gives pHash almost no structural/frequency content to distinguish
    on (pHash is a DCT of luminance gradients, not an absolute-color comparator — two
    different flat colors can hash near-identically). A shape breaks that degeneracy, same as
    the real eval fixture builder (evals/ai_fixtures/build_image_similarity_fixtures.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=seed_color)
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [8, 8, size[0] - 8, size[1] - 8],
        fill=(255 - seed_color[0], 255 - seed_color[1], 255 - seed_color[2]),
    )
    img.save(path, format="PNG")


def test_compute_image_hashes_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.jpg"
    not_an_image.write_bytes(b"this is definitely not image data")
    assert compute_image_hashes(not_an_image) is None


def test_compute_image_hashes_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert compute_image_hashes(tmp_path / "does_not_exist.jpg") is None


def test_compute_image_hashes_records_real_dimensions_and_size(tmp_path: Path) -> None:
    path = tmp_path / "solid.png"
    _make_image(path, color=(200, 50, 50), size=(128, 96))
    record = compute_image_hashes(path)
    assert record is not None
    assert record.width == 128
    assert record.height == 96
    assert record.size_bytes == path.stat().st_size
    assert len(record.phash_hex) > 0
    assert len(record.dhash_hex) > 0


def test_hamming_distance_zero_for_identical_hash() -> None:
    assert hamming_distance("ffff0000ffff0000", "ffff0000ffff0000") == 0


def test_hamming_distance_is_symmetric(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _make_image(a, color=(10, 20, 30))
    _make_image(b, color=(200, 180, 90))
    record_a = compute_image_hashes(a)
    record_b = compute_image_hashes(b)
    assert record_a is not None
    assert record_b is not None
    assert hamming_distance(record_a.phash_hex, record_b.phash_hex) == hamming_distance(
        record_b.phash_hex, record_a.phash_hex
    )


def test_cluster_by_hamming_distance_groups_near_identical_and_drops_singletons(
    tmp_path: Path,
) -> None:
    # Two patterned images differing by only a tiny color shift should hash near-identically;
    # a structurally different pattern should not cluster with them at a tight threshold.
    near_a = tmp_path / "near_a.png"
    near_b = tmp_path / "near_b.png"
    distant = tmp_path / "distant.png"
    _make_patterned_image(near_a, seed_color=(100, 100, 100))
    _make_patterned_image(near_b, seed_color=(102, 100, 100))
    _make_image(distant, color=(0, 255, 0))  # flat color: structurally distinct from the pattern

    records = [compute_image_hashes(p) for p in (near_a, near_b, distant)]
    assert all(r is not None for r in records)

    clusters = cluster_by_hamming_distance(records, max_distance=4)  # type: ignore[arg-type]

    assert len(clusters) == 1
    clustered_paths = {member.path for member in clusters[0]}
    assert clustered_paths == {near_a, near_b}


def test_cluster_by_hamming_distance_empty_input_returns_empty_list() -> None:
    assert cluster_by_hamming_distance([], max_distance=10) == []
