from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Maps the INRIA Copydays dataset's real, human-construction-verified ground truth (ADR-0015)
# into pair-level labels for reclaim.ai.eval_harness's PR-curve machinery. Copydays filenames
# follow a `BBBBSS.jpg` convention (verified against the real extracted files, not assumed from
# documentation): the first 4 digits are a "block" ID shared by one original photo and every
# attacked derivative of it; the last 2 digits are 00 for the unmodified original and 01, 02,
# ... for each attacked variant. Every within-block pair is a true near-duplicate (same source
# photo, however heavily attacked); every cross-block pair is a true negative (unrelated
# photos) — this labeling requires no human judgment call of our own, it falls directly out of
# how the dataset's creators constructed it.


@dataclass(frozen=True, slots=True)
class CopydaysImage:
    path: Path
    block_id: str
    is_original: bool  # True for the SS == "00" file in its block


@dataclass(frozen=True, slots=True)
class CopydaysPair:
    image_a: CopydaysImage
    image_b: CopydaysImage
    is_same_photo: bool  # ground truth: same block (True) or different block (False)


def discover_copydays_images(extracted_root: Path) -> list[CopydaysImage]:
    images = []
    for path in sorted(extracted_root.glob("*.jpg")):
        stem = path.stem
        if len(stem) != 6 or not stem.isdigit():
            continue  # not a Copydays-convention filename — skip rather than guess
        images.append(CopydaysImage(path=path, block_id=stem[:4], is_original=(stem[4:] == "00")))
    return images


def all_pairs(images: list[CopydaysImage]) -> list[CopydaysPair]:
    """Every unordered pair among `images` — O(n^2), same posture as
    phash.cluster_by_hamming_distance and the existing synthetic-fixture eval's
    `_all_pairwise_hamming_scored`: at Copydays' real scale (386 images -> ~74k pairs) this is
    fast and doesn't need bucketing."""
    pairs = []
    for i in range(len(images)):
        for j in range(i + 1, len(images)):
            pairs.append(
                CopydaysPair(
                    image_a=images[i],
                    image_b=images[j],
                    is_same_photo=(images[i].block_id == images[j].block_id),
                )
            )
    return pairs


def blocks_with_original_and_variants(
    images: list[CopydaysImage],
) -> dict[str, tuple[CopydaysImage, list[CopydaysImage]]]:
    """Groups images by block, returning (original, [attacked variants]) per block — the shape
    keep-best evaluation needs (ADR-0015 §4: is the classical scorer's recommended keeper the
    real, unmodified original, not a print-and-scanned/blurred/painted derivative?). Omits any
    block that has no discovered original (shouldn't happen on the real dataset, but a loader
    must not silently assume)."""
    by_block: dict[str, list[CopydaysImage]] = {}
    for image in images:
        by_block.setdefault(image.block_id, []).append(image)

    result: dict[str, tuple[CopydaysImage, list[CopydaysImage]]] = {}
    for block_id, members in by_block.items():
        originals = [m for m in members if m.is_original]
        if len(originals) != 1:
            continue
        variants = [m for m in members if not m.is_original]
        if variants:
            result[block_id] = (originals[0], variants)
    return result
