from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1a Track A (spec §1): classical (v1) keep-best quality scorer for near-identical
# clusters. Deliberately NOT a neural net — spec explicitly says NIMA is a v2, add-only-if-
# measured-to-help upgrade, never a default. Every signal here is directly inspectable
# (sharpness, resolution, exposure spread, file size), and `combined` is a documented,
# transparent weighted sum — never presented as a calibrated quality "probability" (spec §0.6).


@dataclass(frozen=True, slots=True)
class QualityScore:
    path: Path
    sharpness: (
        float  # Laplacian variance of the grayscale image — higher means sharper/less blurred
    )
    resolution_pixels: int  # width * height
    exposure_spread: float  # grayscale pixel std-dev — a crude proxy for "not washed out/too dark"
    size_bytes: int
    combined: float  # see `_combine` for the exact, documented formula


def score_image_quality(path: Path) -> QualityScore | None:
    """Returns `None` (not an error) for a file `cv2.imread` can't decode — same "skip, don't
    abort" posture as `phash.compute_image_hashes`."""
    cv2 = require("cv2", feature="sharpness/exposure quality scoring")

    grayscale = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if grayscale is None:
        return None

    sharpness = float(cv2.Laplacian(grayscale, cv2.CV_64F).var())
    height, width = grayscale.shape
    resolution_pixels = width * height
    exposure_spread = float(grayscale.std())
    size_bytes = path.stat().st_size

    return QualityScore(
        path=path,
        sharpness=sharpness,
        resolution_pixels=resolution_pixels,
        exposure_spread=exposure_spread,
        size_bytes=size_bytes,
        combined=_combine(
            sharpness=sharpness,
            resolution_pixels=resolution_pixels,
            exposure_spread=exposure_spread,
        ),
    )


def _combine(*, sharpness: float, resolution_pixels: int, exposure_spread: float) -> float:
    """Transparent, documented weighted combination — every term is explained, nothing is a
    tuned black-box weight presented as measured. PROVISIONAL (see ADR-0012): these weights
    are chosen for directional correctness (sharper and higher-resolution should score
    higher; extreme over/under-exposure should score lower), not fit to labeled data — no
    gold-set label exists yet to fit them against. The eval this feature ships with tests
    top-1 keep-best agreement and the "never picks the worst-quality member" safety metric on
    synthetic fixtures where the ground truth is unambiguous by construction (one member is
    deliberately blurred/downscaled), which does not require precisely-tuned weights to pass
    — only correct direction.

    - `log1p(sharpness)`: Laplacian variance ranges from near-zero (flat/blurred) into the
      thousands (sharp, detailed) — log-compressed so one extremely sharp outlier doesn't
      dominate the sum.
    - `log1p(resolution_pixels)`: same log-compression reasoning for pixel count.
    - `-abs(exposure_spread - MID_EXPOSURE_SPREAD)`: a histogram std-dev far from a
      "reasonable" mid-range value indicates either a washed-out (low spread) or extremely
      high-contrast/noisy (very high spread) image — penalized either direction, not just one.
    """
    mid_exposure_spread = 60.0
    sharpness_component = math.log1p(max(sharpness, 0.0))
    resolution_component = math.log1p(max(resolution_pixels, 0))
    exposure_component = -abs(exposure_spread - mid_exposure_spread)
    return sharpness_component * 2.0 + resolution_component * 1.0 + exposure_component * 0.05


def select_keep(scores: Sequence[QualityScore]) -> QualityScore:
    """Picks the highest-`combined`-score member — the recommended keeper. Never deletes
    anything itself; the caller (feature-1a orchestration, not yet wired to any UI/CLI) is
    responsible for turning this into an `AICluster` with `is_recommended_keep=True` on the
    matching member."""
    if not scores:
        raise ValueError("cannot select a keeper from an empty score list")
    return max(scores, key=lambda score: score.combined)
