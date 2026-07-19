from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

from reclaim.ai.screenshot_burst import (
    ScreenshotRecord,
    cluster_screenshot_bursts,
    compute_screenshot_record,
    matches_a_common_screen_resolution,
)


def _make_screenshot(
    path: Path,
    *,
    size: tuple[int, int] = (1920, 1080),
    seed_color: tuple[int, int, int] = (10, 10, 10),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=seed_color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, size[0] - 20, size[1] - 20], outline=(200, 200, 200), width=4)
    img.save(path, format="PNG")


def _record_at(
    path: Path, *, mtime: float, size: tuple[int, int] = (1920, 1080), seed_color=(10, 10, 10)
) -> ScreenshotRecord:
    _make_screenshot(path, size=size, seed_color=seed_color)
    os.utime(path, (mtime, mtime))
    record = compute_screenshot_record(path)
    assert record is not None
    return record


def test_compute_screenshot_record_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    not_an_image = tmp_path / "fake.png"
    not_an_image.write_bytes(b"not image data")
    assert compute_screenshot_record(not_an_image) is None


def test_cluster_screenshot_bursts_groups_same_size_close_in_time_and_similar(
    tmp_path: Path,
) -> None:
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now)
    b = _record_at(tmp_path / "shot2.png", mtime=now + 5)
    c = _record_at(tmp_path / "shot3.png", mtime=now + 10)

    bursts = cluster_screenshot_bursts([a, b, c])
    assert len(bursts) == 1
    assert {r.path for r in bursts[0]} == {a.path, b.path, c.path}


def test_cluster_screenshot_bursts_singleton_is_dropped(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now)
    assert cluster_screenshot_bursts([a]) == []


def test_cluster_screenshot_bursts_rejects_different_dimensions(tmp_path: Path) -> None:
    """Same content, same time, but different dimensions (a different device/window) must
    never be unioned -- all three rules must agree, not a majority vote."""
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now, size=(1920, 1080))
    b = _record_at(tmp_path / "shot2.png", mtime=now + 1, size=(1366, 768))
    assert cluster_screenshot_bursts([a, b]) == []


def test_cluster_screenshot_bursts_rejects_time_gap_beyond_window(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now)
    b = _record_at(tmp_path / "shot2.png", mtime=now + 3600)  # an hour apart
    assert cluster_screenshot_bursts([a, b]) == []


def test_cluster_screenshot_bursts_rejects_visually_different_content(tmp_path: Path) -> None:
    """Same dimensions, same time window, but visually distinct (very different pHash) --
    coincidentally-same-resolution screenshots of different content must not be grouped."""
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now, seed_color=(10, 10, 10))
    b = _record_at(tmp_path / "shot2.png", mtime=now + 1, seed_color=(245, 245, 245))
    assert cluster_screenshot_bursts([a, b]) == []


def test_cluster_screenshot_bursts_respects_custom_thresholds(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    a = _record_at(tmp_path / "shot1.png", mtime=now)
    b = _record_at(tmp_path / "shot2.png", mtime=now + 30)
    assert cluster_screenshot_bursts([a, b], max_capture_time_gap_seconds=10.0) == []
    assert len(cluster_screenshot_bursts([a, b], max_capture_time_gap_seconds=60.0)) == 1


def test_matches_a_common_screen_resolution() -> None:
    assert matches_a_common_screen_resolution(1920, 1080) is True
    assert matches_a_common_screen_resolution(1080, 1920) is True  # portrait orientation
    assert matches_a_common_screen_resolution(4000, 3000) is False  # a camera photo
