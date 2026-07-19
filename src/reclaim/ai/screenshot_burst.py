from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai.phash import ImageHashRecord, compute_image_hashes, hamming_distance

# Feature 2's screenshot-burst detection (spec: "dimensions==screen-resolution + capture-time
# proximity + pHash — mostly deterministic"). Three classical, inspectable rules combine to
# decide "these images are one burst" — no model, no learned threshold beyond the pHash
# Hamming distance ADR-0012 already measured for exactly this problem (near-identical images
# taken moments apart IS the near-identical-image-detection problem Feature 1a solved; a
# screenshot burst doesn't need its own separate pHash re-measurement).

MAX_HAMMING_DISTANCE = 14  # MEASURED, ADR-0012/ADR-0015 — reused as-is, not re-derived here.
MAX_CAPTURE_TIME_GAP_SECONDS = 60.0  # policy choice, not data-derived (see ADR-0019):
# consecutive screenshots taken while iterating on the same task (e.g. repeatedly re-taking a
# screenshot of a scrolling page, or a UI that keeps re-rendering) are very rarely more than a
# minute apart; a minute of margin also tolerates filesystem mtime jitter/rounding.


@dataclass(frozen=True, slots=True)
class ScreenshotRecord:
    path: Path
    width: int
    height: int
    mtime: float
    phash_hex: str


def compute_screenshot_record(path: Path) -> ScreenshotRecord | None:
    """Returns `None` (not an error) for a file that fails to decode as an image — same
    "skip, don't abort" posture as every other AI-layer compute function."""
    hash_record: ImageHashRecord | None = compute_image_hashes(path)
    if hash_record is None:
        return None
    return ScreenshotRecord(
        path=path,
        width=hash_record.width,
        height=hash_record.height,
        mtime=path.stat().st_mtime,
        phash_hex=hash_record.phash_hex,
    )


def _same_dimensions(a: ScreenshotRecord, b: ScreenshotRecord) -> bool:
    """Same width AND height — the "dimensions == screen resolution" rule from spec, applied
    pairwise: two screenshots from the same capture session share the exact same screen/window
    resolution, while an unrelated photo or a screenshot from a different device essentially
    never coincidentally matches to the pixel."""
    return a.width == b.width and a.height == b.height


def _within_time_window(
    a: ScreenshotRecord, b: ScreenshotRecord, *, max_gap_seconds: float
) -> bool:
    return abs(a.mtime - b.mtime) <= max_gap_seconds


def _visually_near_identical(
    a: ScreenshotRecord, b: ScreenshotRecord, *, max_hamming_distance: int
) -> bool:
    return hamming_distance(a.phash_hex, b.phash_hex) <= max_hamming_distance


class _UnionFind:
    """Same minimal disjoint-set as phash.py's `_UnionFind` and minhash_lsh.py's — duplicated
    rather than shared for the same reason: no coupling between sibling AI pipelines beyond
    living under `reclaim.ai`."""

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


def cluster_screenshot_bursts(
    records: Sequence[ScreenshotRecord],
    *,
    max_hamming_distance: int = MAX_HAMMING_DISTANCE,
    max_capture_time_gap_seconds: float = MAX_CAPTURE_TIME_GAP_SECONDS,
) -> list[list[ScreenshotRecord]]:
    """Groups `records` into burst clusters: a pair is unioned iff ALL THREE rules agree
    (same dimensions AND within the capture-time window AND visually near-identical by pHash)
    — every rule must pass, not a majority vote, since each rule alone has real false-positive
    risk (two coincidentally-same-resolution screenshots of different content taken close in
    time; or two visually-similar-but-unrelated images). Singleton "clusters" are dropped —
    not a burst if there's nothing to group with. O(n^2) pairwise comparison, same scale
    reasoning as `phash.cluster_by_hamming_distance`: runs on the residual after exact-hash
    dedup, not a raw disk-wide scan.
    """
    union_find = _UnionFind(len(records))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            record_i, record_j = records[i], records[j]
            if (
                _same_dimensions(record_i, record_j)
                and _within_time_window(
                    record_i, record_j, max_gap_seconds=max_capture_time_gap_seconds
                )
                and _visually_near_identical(
                    record_i, record_j, max_hamming_distance=max_hamming_distance
                )
            ):
                union_find.union(i, j)

    groups: dict[int, list[ScreenshotRecord]] = {}
    for index, record in enumerate(records):
        groups.setdefault(union_find.find(index), []).append(record)
    return [group for group in groups.values() if len(group) > 1]


_COMMON_SCREEN_RESOLUTIONS: frozenset[tuple[int, int]] = frozenset(
    {
        (1920, 1080),
        (2560, 1440),
        (3840, 2160),
        (1366, 768),
        (1280, 720),
        (2880, 1800),
        (3024, 1964),
        (1170, 2532),  # common phone portrait resolutions (screenshots, not just desktop)
        (1080, 2400),
        (1284, 2778),
    }
)


def matches_a_common_screen_resolution(width: int, height: int) -> bool:
    """A weaker, single-image signal (no pairing needed) for "this looks like a screenshot at
    all" — used as a pre-filter before the pairwise burst clustering above, not a replacement
    for it (`_same_dimensions` is the real "these two are the same session" signal; this
    function only helps exclude obviously-not-a-screenshot images, like a 4000x3000 camera
    photo, from the candidate pool before the O(n^2) pass). Not exhaustive — `os.environ`
    could report the real screen resolution but that's the CURRENT machine's display, not
    necessarily the device the screenshot was taken on (a photo library synced from a phone,
    for instance) — a hardcoded common-resolutions list is deliberately used instead of trying
    to detect "the" screen resolution.
    """
    return (width, height) in _COMMON_SCREEN_RESOLUTIONS or (
        height,
        width,
    ) in _COMMON_SCREEN_RESOLUTIONS


def _current_display_resolution() -> tuple[int, int] | None:  # pragma: no cover - platform hook
    """Best-effort real screen-resolution lookup for the CURRENT machine, if ever needed by a
    caller wanting to bias toward the local display rather than the hardcoded common-
    resolutions list. Not called by this module's own functions (see
    `matches_a_common_screen_resolution`'s docstring for why) — kept as an available, tested
    building block, not wired into the default pipeline."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    except (AttributeError, OSError):
        return None
