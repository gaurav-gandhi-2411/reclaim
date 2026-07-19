from __future__ import annotations

import math
from dataclasses import dataclass

# Feature 3's cold-start heuristic (spec §4): "until enough labels exist, surface review
# candidates by a transparent heuristic priority (size x mtime-staleness x location-weight x
# cluster-membership). Clearly labeled 'heuristic,' not 'AI.'"
#
# This is NOT a model. No training, no learned/fit weights, every term directly inspectable —
# same classical-scorer posture as `keep_best.py`'s quality score. It orders the review queue
# ONLY until the label-gated LambdaMART ranker (spec §4, `feedback_store.py`'s decision log
# feeds it) activates at >= 500 real logged decisions — that ranker is a documented FUTURE
# step, not built here (there is no data to train it on yet). `ColdStartPriority.is_heuristic`
# is hardcoded `True` on every result specifically so nothing downstream can mistake this
# priority number for a model's prediction (spec §0.6's "never a manufactured confidence").

_LOCATION_WEIGHTS: dict[str, float] = {
    "downloads": 1.5,  # the classic incidental-clutter location
    "temp": 1.4,
    "desktop": 1.2,
    "documents": 1.0,
    "other": 1.0,
    "cloud_sync_placeholder": 0.8,  # already synced/backed up elsewhere -- lower priority to
    # disturb than a purely-local file, since removing it doesn't reclaim local disk the same
    # way and its "clutter-ness" is less certain (it may be actively used from another device).
    "git_repo": 0.5,  # tracked, versioned content -- least likely to be incidental clutter.
}


def location_weight(path_class: str) -> float:
    """`path_class` is `feedback_store.classify_path_class`'s output. Unrecognized classes
    default to the neutral `1.0` weight rather than raising — this heuristic degrades
    gracefully on an unfamiliar path_class instead of failing the whole priority computation."""
    return _LOCATION_WEIGHTS.get(path_class, 1.0)


def mtime_staleness_days(mtime: float, *, now: float) -> float:
    """Never negative — a file with a future mtime (clock skew, a restored backup) is treated
    as zero staleness, not a bug to propagate into the priority score."""
    return max(0.0, (now - mtime) / 86400.0)


@dataclass(frozen=True, slots=True)
class ColdStartPriority:
    """Every component is exposed, not just `combined` — so a review UI can show *why* an
    item ranked where it did (spec §7.2's diagnosability requirement, same posture as
    `keep_best.QualityScore` exposing sharpness/resolution/exposure_spread alongside
    `combined`), and so this is auditable as a formula, not a black box."""

    size_component: float
    staleness_component: float
    location_component: float
    cluster_membership_component: float
    combined: float
    is_heuristic: bool = True  # ALWAYS True -- see module docstring.


def compute_cold_start_priority(
    *, size_bytes: int, mtime: float, now: float, path_class: str, cluster_size: int
) -> ColdStartPriority:
    """Higher `combined` = higher review priority (surface this first). `size x
    mtime-staleness x location-weight x cluster-membership`, log-compressed on the two
    unbounded terms (size in bytes, staleness in days) for the same reason
    `keep_best._combine` log-compresses sharpness/resolution: one extreme outlier (a single
    50GB file, a file untouched for a decade) shouldn't dominate the ranking of everything
    else. `cluster_size` rewards items with more redundant siblings (a burst/near-dup group of
    8 is more likely genuine clutter than a lone unclustered file) — `cluster_size=1` (no
    real cluster) contributes zero bonus, not a penalty.
    """
    size_component = math.log1p(max(size_bytes, 0))
    staleness_component = math.log1p(mtime_staleness_days(mtime, now=now))
    location_component = location_weight(path_class)
    cluster_membership_component = math.log1p(max(cluster_size - 1, 0))
    combined = (
        size_component
        * (1.0 + staleness_component)
        * location_component
        * (1.0 + cluster_membership_component)
    )
    return ColdStartPriority(
        size_component=size_component,
        staleness_component=staleness_component,
        location_component=location_component,
        cluster_membership_component=cluster_membership_component,
        combined=combined,
    )
