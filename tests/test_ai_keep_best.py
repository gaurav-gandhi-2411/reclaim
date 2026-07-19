from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageFilter

from reclaim.ai.keep_best import QualityScore, score_image_quality, select_keep


def _make_image(path: Path, *, size: tuple[int, int] = (128, 128), blur: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A checkerboard-ish pattern (not a flat color) so Laplacian variance is meaningfully
    # nonzero and blur has something real to smooth out.
    img = Image.new("L", size)
    pixels = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            pixels[x, y] = 255 if (x // 8 + y // 8) % 2 == 0 else 0
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=3))
    img.convert("RGB").save(path, format="PNG")


def test_score_image_quality_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.jpg"
    not_an_image.write_bytes(b"not image data")
    assert score_image_quality(not_an_image) is None


def test_score_image_quality_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert score_image_quality(tmp_path / "gone.jpg") is None


def test_sharp_image_scores_higher_sharpness_than_blurred(tmp_path: Path) -> None:
    sharp_path = tmp_path / "sharp.png"
    blurred_path = tmp_path / "blurred.png"
    _make_image(sharp_path, blur=False)
    _make_image(blurred_path, blur=True)

    sharp = score_image_quality(sharp_path)
    blurred = score_image_quality(blurred_path)
    assert sharp is not None
    assert blurred is not None
    assert sharp.sharpness > blurred.sharpness
    assert sharp.combined > blurred.combined


def test_higher_resolution_scores_higher_resolution_component(tmp_path: Path) -> None:
    small_path = tmp_path / "small.png"
    large_path = tmp_path / "large.png"
    _make_image(small_path, size=(32, 32))
    _make_image(large_path, size=(256, 256))

    small = score_image_quality(small_path)
    large = score_image_quality(large_path)
    assert small is not None
    assert large is not None
    assert large.resolution_pixels > small.resolution_pixels


def test_select_keep_picks_highest_combined_score() -> None:
    low = QualityScore(
        path=Path("low.jpg"),
        sharpness=1.0,
        resolution_pixels=100,
        exposure_spread=60,
        size_bytes=10,
        combined=0.5,
    )
    high = QualityScore(
        path=Path("high.jpg"),
        sharpness=5.0,
        resolution_pixels=100,
        exposure_spread=60,
        size_bytes=10,
        combined=5.0,
    )
    assert select_keep([low, high]).path == Path("high.jpg")
    assert select_keep([high, low]).path == Path("high.jpg")  # order-independent


def test_select_keep_raises_on_empty_sequence() -> None:
    with pytest.raises(ValueError, match="empty"):
        select_keep([])
