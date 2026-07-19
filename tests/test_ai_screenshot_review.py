from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from reclaim.ai.models import AITrack
from reclaim.ai.screenshot_review import build_screenshot_burst_clusters
from reclaim.config import Config
from reclaim.safety import SafetyValidator

# Orchestration tests for Feature 2's end-to-end pipeline: safety-filter -> burst clustering
# -> OCR -> content tagging -> AICluster. The central property under test (GG's explicit
# instruction: "bias STRONGLY toward keep for receipt/document/code tags... only transient-UI
# may be deletion-eligible") is the conditional-keeper gate in `_build_one_cluster`: a burst
# only ever gets a recommended keeper when EVERY member's OCR content tag is TRANSIENT_UI.


def _make_burst_image(path: Path, text: str, *, mtime: float, size=(640, 480)) -> None:
    """A shared, visually-dominant checkerboard background (so pHash groups these as one
    burst regardless of the small text overlay) with distinct overlaid text per image, so OCR
    content genuinely differs across "burst members" the way real screenshots would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(240, 240, 240))
    pixels_draw = ImageDraw.Draw(img)
    for x in range(0, size[0], 16):
        for y in range(0, size[1], 16):
            if (x // 16 + y // 16) % 2 == 0:
                pixels_draw.rectangle([x, y, x + 16, y + 16], fill=(210, 210, 210))
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except OSError:
        font = ImageFont.load_default()
    y_pos = size[1] // 2 - 40
    for line in text.splitlines():
        pixels_draw.text((30, y_pos), line, fill=(0, 0, 0), font=font)
        y_pos += 34
    img.save(path, format="PNG")
    os.utime(path, (mtime, mtime))


def test_build_screenshot_burst_clusters_recommends_a_keeper_when_all_members_are_transient_ui(
    tmp_path: Path,
) -> None:
    now = 1_700_000_000.0
    p1 = tmp_path / "shot1.png"
    p2 = tmp_path / "shot2.png"
    p3 = tmp_path / "shot3.png"
    _make_burst_image(p1, "Loading...", mtime=now)
    _make_burst_image(p2, "Please wait", mtime=now + 3)
    _make_burst_image(p3, "Retry", mtime=now + 6)

    safety = SafetyValidator(Config())
    clusters = build_screenshot_burst_clusters([p1, p2, p3], safety=safety)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.track == AITrack.SCREENSHOT_BURST
    assert cluster.suggests_deletion is True
    keepers = [m for m in cluster.members if m.is_recommended_keep]
    assert len(keepers) == 1


def test_build_screenshot_burst_clusters_is_browse_only_when_one_member_has_real_content(
    tmp_path: Path,
) -> None:
    """The safety-critical case: a burst where most members look like transient UI but ONE
    member's OCR text tags as something else (here, receipt-like content) -- the WHOLE
    cluster must downgrade to browse-only, never suggest deletion for any member."""
    now = 1_700_000_000.0
    p1 = tmp_path / "shot1.png"
    p2 = tmp_path / "shot2.png"
    _make_burst_image(p1, "Loading...", mtime=now)
    _make_burst_image(
        p2,
        "Total: $19.43\nTax: $1.44\nThank you for your purchase",
        mtime=now + 3,
    )

    # A generous max_hamming_distance isolates the variable under test (content-tag
    # downgrade logic) from pHash's real sensitivity to the receipt text's extra visual ink
    # (already covered by test_ai_screenshot_burst.py's own dedicated pHash-rejection tests).
    safety = SafetyValidator(Config())
    clusters = build_screenshot_burst_clusters([p1, p2], safety=safety, max_hamming_distance=64)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.track == AITrack.SCREENSHOT_BURST
    assert cluster.suggests_deletion is False
    assert all(not m.is_recommended_keep for m in cluster.members)
    assert "NOT transient-UI" in cluster.rationale


def test_build_screenshot_burst_clusters_returns_empty_for_no_burst(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    p1 = tmp_path / "shot1.png"
    _make_burst_image(p1, "Loading...", mtime=now)

    safety = SafetyValidator(Config())
    assert build_screenshot_burst_clusters([p1], safety=safety) == []


def test_ocr_secret_text_never_appears_anywhere_in_the_returned_clusters(tmp_path: Path) -> None:
    """PRIVACY LOCK, end-to-end: a burst built from images containing a unique secret token
    must never surface that token anywhere in the AICluster/AIClusterMember objects returned
    to a caller -- checked via repr() over the whole cluster list as a structural catch-all,
    on top of the dataclasses' own fields having no text-carrying field to begin with."""
    canary_text = "XKQZ-SECRET-4471"
    now = 1_700_000_000.0
    p1 = tmp_path / "shot1.png"
    p2 = tmp_path / "shot2.png"
    _make_burst_image(p1, f"CONFIDENTIAL\n{canary_text}", mtime=now)
    _make_burst_image(p2, f"CONFIDENTIAL\n{canary_text}", mtime=now + 3)

    safety = SafetyValidator(Config())
    clusters = build_screenshot_burst_clusters([p1, p2], safety=safety)

    assert len(clusters) == 1
    assert canary_text not in repr(clusters)
    for cluster in clusters:
        assert canary_text not in cluster.rationale
        assert canary_text not in cluster.cluster_id
        for member in cluster.members:
            assert canary_text not in str(member.path)
