from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

# Synthetic, deterministic (SEED-driven, per house rule 40), checked-in fixture GENERATOR for
# Feature 1a Track A's eval (spec §7.1: "CI fixtures (checked in, synthetic, deterministic)").
# Unlike evals/fixtures/build_golden_tree.py (which materializes a tree from a hand-authored
# JSON manifest), there's no meaningful way to hand-author "which pixels" for an image fixture
# — the checked-in artifact here IS this generator: deterministic given SEED, so re-running it
# always reproduces the exact same tree + ground truth without needing to commit any binary
# image files to the repo.
#
# This module needs Pillow directly (no reclaim.ai._optional guard) — unlike src/reclaim/ai/*
# runtime code, an eval fixture builder is test-only and already requires the `ai` extras
# installed to run at all (same posture as build_golden_tree.py requiring git on PATH).

_SEED = 42
_BASE_SIZE = (512, 512)


@dataclass(frozen=True, slots=True)
class ImageFixtureCase:
    id: str
    relative_path: str
    # Shared by every member of a near-dup cluster; unique per distractor (so a distractor is
    # its own singleton "true cluster" for BCubed purposes).
    true_cluster_id: str
    # Ground truth for the keep-best scorer — True for exactly one member per real cluster,
    # meaningless (always False) for distractors.
    is_best_quality: bool


def _draw_base_image(seed: int) -> Image.Image:
    """A distinct, seeded synthetic image — different seeds produce visually and
    perceptually distinguishable images (won't spuriously pHash-cluster with each other)."""
    rng = random.Random(seed)  # noqa: S311 -- deterministic synthetic fixture generation, not
    # a security context; the whole point of a fixed seed is reproducible test data.
    background = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    img = Image.new("RGB", _BASE_SIZE, color=background)
    draw = ImageDraw.Draw(img)
    for _ in range(8):
        shape = rng.choice(["ellipse", "rectangle"])
        x0, y0 = rng.randint(0, _BASE_SIZE[0] - 50), rng.randint(0, _BASE_SIZE[1] - 50)
        x1, y1 = x0 + rng.randint(40, 200), y0 + rng.randint(40, 200)
        color = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        getattr(draw, shape)([x0, y0, x1, y1], fill=color)
    return img


def _save_jpeg(img: Image.Image, path: Path, *, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, format="JPEG", quality=quality)


def build_image_similarity_fixtures(
    root: Path, *, n_clusters: int = 6, n_distractors: int = 8
) -> list[ImageFixtureCase]:
    """Materializes a synthetic image tree under `root` with known ground truth (also
    returned). Each cluster has one unambiguous best-quality member (sharp, full detail, high
    JPEG quality), one unambiguous worst-quality member (heavily downscaled+upscaled,
    gaussian-blurred, low JPEG quality), and two middle-quality variants (moderate
    resize+recompress; a mild brightness shift simulating a re-export) — ground truth doesn't
    depend on the keep-best scorer's exact weights being finely tuned, only on the DIRECTION
    of sharpness/resolution/exposure scoring being right. Distractor images are each a
    distinct pattern with no near-dup variants, testing that clustering doesn't over-merge.
    """
    cases: list[ImageFixtureCase] = []

    for cluster_index in range(n_clusters):
        seed = _SEED + cluster_index
        base = _draw_base_image(seed)
        cluster_id = f"cluster_{cluster_index:02d}"
        cluster_dir = root / cluster_id

        best_path = cluster_dir / "best_original.jpg"
        _save_jpeg(base, best_path, quality=95)
        cases.append(
            ImageFixtureCase(
                f"{cluster_id}_best", str(best_path.relative_to(root)), cluster_id, True
            )
        )

        # Every non-best variant is genuinely downscaled (not resized back up to the original
        # dimensions) — real near-dup copies (a resave, a resize-to-share, a thumbnail-grade
        # re-export) usually do carry fewer actual pixels, and this gives the keep-best
        # scorer's resolution signal a real, unambiguous difference to measure instead of
        # every member having identical pixel dimensions (an earlier version of this fixture
        # resized every variant back up to _BASE_SIZE, which silently zeroed out the
        # resolution term's discriminating power and made "best" vs. a same-resolution,
        # equally-sharp variant an arbitrary tossup — this is that fix). pHash/dHash are
        # themselves scale-invariant (both resize their input to a small canonical size
        # before hashing), so this doesn't hurt clustering at all.
        moderate = base.resize((int(_BASE_SIZE[0] * 0.7), int(_BASE_SIZE[1] * 0.7)), Image.BILINEAR)
        moderate_path = cluster_dir / "middle_resized.jpg"
        _save_jpeg(moderate, moderate_path, quality=70)
        cases.append(
            ImageFixtureCase(
                f"{cluster_id}_mid_resize", str(moderate_path.relative_to(root)), cluster_id, False
            )
        )

        # Mild brightness shift (0.92-1.08x) simulating a re-edited/re-exported copy, ALSO
        # mildly downscaled — kept narrow so the near-dup remains realistically recognizable,
        # not an unrealistically hard synthetic case that would force an unreasonably loose
        # Hamming threshold.
        brightness_rng = random.Random(seed + 1000)  # noqa: S311 -- deterministic fixture data
        brightness_factor = 0.92 + brightness_rng.random() * 0.16
        shifted_full = ImageEnhance.Brightness(base).enhance(brightness_factor)
        shifted = shifted_full.resize(
            (int(_BASE_SIZE[0] * 0.85), int(_BASE_SIZE[1] * 0.85)), Image.BILINEAR
        )
        shifted_path = cluster_dir / "middle_brightness.jpg"
        _save_jpeg(shifted, shifted_path, quality=80)
        cases.append(
            ImageFixtureCase(
                f"{cluster_id}_mid_brightness",
                str(shifted_path.relative_to(root)),
                cluster_id,
                False,
            )
        )

        heavily_small = base.resize(
            (int(_BASE_SIZE[0] * 0.25), int(_BASE_SIZE[1] * 0.25)), Image.BILINEAR
        )
        worst = heavily_small.filter(ImageFilter.GaussianBlur(radius=2))
        worst_path = cluster_dir / "worst_blurred.jpg"
        _save_jpeg(worst, worst_path, quality=20)
        cases.append(
            ImageFixtureCase(
                f"{cluster_id}_worst", str(worst_path.relative_to(root)), cluster_id, False
            )
        )

    distractor_dir = root / "distractors"
    for distractor_index in range(n_distractors):
        seed = _SEED + 10_000 + distractor_index
        img = _draw_base_image(seed)
        distractor_id = f"distractor_{distractor_index:02d}"
        path = distractor_dir / f"{distractor_id}.jpg"
        _save_jpeg(img, path, quality=90)
        cases.append(
            ImageFixtureCase(distractor_id, str(path.relative_to(root)), distractor_id, False)
        )

    return cases
