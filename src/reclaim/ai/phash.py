from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require

# Feature 1a Track A (spec §1): the pHash/dHash prefilter for near-identical image clustering.
# This is a HASH, not ML — the cheap Stage-0/Stage-1 prefilter the spec calls for (§0.4:
# "cheap deterministic/hash prefilter ... never embed a file a hash could have excluded").
# Callers are responsible for handing this module the RESIDUAL after exact-hash (BLAKE3)
# dedup — this module does not re-check for byte-identical files itself.


@dataclass(frozen=True, slots=True)
class ImageHashRecord:
    """One image's computed hashes + the cheap metadata needed for keep-best scoring later —
    stored as hex strings (not `imagehash.ImageHash` objects) so records are trivially
    serializable into the future embedding/hash SQLite cache (spec §0.5) without needing a
    custom codec."""

    path: Path
    size_bytes: int
    width: int
    height: int
    phash_hex: str
    dhash_hex: str


def compute_image_hashes(path: Path) -> ImageHashRecord | None:
    """Computes pHash + dHash for one image file. Returns `None` (not an error) for a file
    that can't be opened as an image — corrupt file, non-image with an image extension, or a
    format Pillow doesn't support — a common, expected case, not worth raising for; the
    caller simply excludes it from clustering."""
    imagehash = require("imagehash", feature="perceptual-hash near-duplicate clustering")
    pil_image = require("PIL.Image", feature="image loading")

    try:
        with pil_image.open(path) as img:
            img.load()
            width, height = img.size
            phash = imagehash.phash(img)
            dhash = imagehash.dhash(img)
    except Exception:
        # decode exceptions (UnidentifiedImageError, OSError, ...) means "not a usable image
        # here," never a reason to abort the whole scan.
        return None

    return ImageHashRecord(
        path=path,
        size_bytes=path.stat().st_size,
        width=width,
        height=height,
        phash_hex=str(phash),
        dhash_hex=str(dhash),
    )


def hamming_distance(hash_hex_a: str, hash_hex_b: str) -> int:
    imagehash = require("imagehash", feature="hamming-distance comparison")
    return imagehash.hex_to_hash(hash_hex_a) - imagehash.hex_to_hash(hash_hex_b)  # type: ignore[no-any-return]


class _UnionFind:
    """Minimal disjoint-set for clustering by pairwise threshold — path-compressed `find`,
    unranked `union` (fine at the scale this runs at; see the module-level scale note)."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        root_i, root_j = self.find(i), self.find(j)
        if root_i != root_j:
            self._parent[root_i] = root_j


def cluster_by_hamming_distance(
    records: Sequence[ImageHashRecord], *, max_distance: int, hash_kind: str = "phash"
) -> list[list[ImageHashRecord]]:
    """Groups `records` into near-identical clusters: any pair within `max_distance` Hamming
    distance is unioned into the same cluster (transitively — a chain of pairwise-close
    images all end up in one cluster even if the first and last are individually farther
    apart than `max_distance`; this is the standard, intentional behavior of threshold-based
    hash clustering, not a bug). Singleton "clusters" (no near-dup partner found) are dropped
    — they're not near-dup candidates at all.

    `max_distance` is the near-identical operating point — MEASURED at 14 on the real public
    INRIA Copydays dataset via the real PR curve (spec §7.3) — see ADR-0012/ADR-0015. Still a
    caller-supplied parameter, not a default hardcoded here.

    O(n²) pairwise comparison — deliberately not optimized further yet. This module operates
    on the RESIDUAL after exact-hash dedup (typically a small fraction of a real disk's image
    count), and the deterministic engine's own history (ADR-0002 etc.) is explicit that
    scale-optimizing before a real bottleneck is measured is premature; if a real-disk run
    ever shows this pairwise pass dominating runtime, that's the trigger to bucket by hash
    prefix or similar, not a guess made now.
    """
    hashes = [r.phash_hex if hash_kind == "phash" else r.dhash_hex for r in records]
    union_find = _UnionFind(len(records))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if hamming_distance(hashes[i], hashes[j]) <= max_distance:
                union_find.union(i, j)

    groups: dict[int, list[ImageHashRecord]] = {}
    for index, record in enumerate(records):
        groups.setdefault(union_find.find(index), []).append(record)
    return [group for group in groups.values() if len(group) > 1]
