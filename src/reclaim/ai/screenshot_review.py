from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from reclaim.ai.content_tagger import ContentTag, tag_content
from reclaim.ai.keep_best import QualityScore, score_image_quality, select_keep
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.phash import hamming_distance
from reclaim.ai.safety import filter_paths_through_safety_validator
from reclaim.ai.screenshot_burst import (
    MAX_CAPTURE_TIME_GAP_SECONDS,
    MAX_HAMMING_DISTANCE,
    ScreenshotRecord,
    cluster_screenshot_bursts,
    compute_screenshot_record,
)
from reclaim.ai.screenshot_ocr import extract_screenshot_text
from reclaim.safety import SafetyValidator

# Feature 2 end-to-end orchestration: safety-filter -> screenshot-record computation
# (dimensions + capture-time + pHash) -> burst clustering -> per-member OCR + content tagging
# -> AICluster construction. Mirrors image_similarity.build_near_identical_clusters'
# structure exactly (same safety-filter -> compute -> cluster -> score -> AICluster shape).
#
# PRIVACY LOCK: `extract_screenshot_text`'s return value (the raw OCR'd text) lives ONLY
# inside this function's local scope, is handed ONLY to `tag_content` (which returns a
# `ContentTag`, never the text itself), and is never assigned to any `AICluster`/
# `AIClusterMember` field, never logged, never returned. This function's own return type,
# `list[AICluster]`, is structurally incapable of carrying OCR text — there is no field on
# either dataclass a caller could even misuse to smuggle it out.
#
# DELETION-ELIGIBILITY GATE (GG's explicit instruction: "bias STRONGLY toward keep for
# receipt/document/code tags... only transient-UI may be deletion-eligible"): a burst cluster
# is only ever given a recommended keeper (and therefore only ever suggests deletion, per
# `AICluster.suggests_deletion`) when EVERY member's content tag is `ContentTag.TRANSIENT_UI`.
# A single member tagged receipt/document/code/chat/unknown downgrades the WHOLE cluster to
# browse-only — the same "any disagreement/ambiguity forces caution" posture as
# `version_chain.py`'s `version_signals_agree`.


def build_screenshot_burst_clusters(
    image_paths: Sequence[Path],
    *,
    safety: SafetyValidator,
    max_hamming_distance: int | None = None,
    max_capture_time_gap_seconds: float | None = None,
) -> list[AICluster]:
    """`max_hamming_distance`/`max_capture_time_gap_seconds` default to
    `screenshot_burst`'s own module constants when omitted — passed through explicitly here
    (not hardcoded) so a caller citing a future re-measurement isn't forced to edit this
    module. See `screenshot_burst.cluster_screenshot_bursts` for the clustering rule itself.
    """
    eligible_paths = filter_paths_through_safety_validator(image_paths, safety)

    records: list[ScreenshotRecord] = []
    for path in eligible_paths:
        record = compute_screenshot_record(path)
        if record is not None:
            records.append(record)

    bursts = cluster_screenshot_bursts(
        records,
        max_hamming_distance=(
            max_hamming_distance if max_hamming_distance is not None else MAX_HAMMING_DISTANCE
        ),
        max_capture_time_gap_seconds=(
            max_capture_time_gap_seconds
            if max_capture_time_gap_seconds is not None
            else MAX_CAPTURE_TIME_GAP_SECONDS
        ),
    )

    clusters: list[AICluster] = []
    for cluster_index, burst in enumerate(bursts):
        clusters.append(_build_one_cluster(cluster_index, burst))
    return clusters


def _build_one_cluster(cluster_index: int, burst: Sequence[ScreenshotRecord]) -> AICluster:
    tags_by_path: dict[Path, ContentTag] = {
        record.path: tag_content(extract_screenshot_text(record.path)).tag for record in burst
    }
    all_transient_ui = all(tag == ContentTag.TRANSIENT_UI for tag in tags_by_path.values())

    quality_scores: list[QualityScore] = [
        score
        for score in (score_image_quality(record.path) for record in burst)
        if score is not None
    ]
    quality_by_path = {score.path: score.combined for score in quality_scores}

    keeper_path: Path | None = None
    if all_transient_ui and len(quality_scores) >= 2:
        keeper_path = select_keep(quality_scores).path

    members = tuple(
        AIClusterMember(
            path=record.path,
            size_bytes=record.path.stat().st_size,
            quality_score=quality_by_path.get(record.path),
            is_recommended_keep=(record.path == keeper_path),
        )
        for record in burst
    )

    phash_hexes = [record.phash_hex for record in burst]
    max_pairwise_distance = max(
        hamming_distance(phash_hexes[i], phash_hexes[j])
        for i in range(len(phash_hexes))
        for j in range(i + 1, len(phash_hexes))
    )

    if keeper_path is not None:
        rationale = (
            f"{len(members)} screenshots taken in a burst (matching resolution, capture time, "
            "and near-identical pHash), all OCR-tagged transient-UI content — recommending the "
            "highest classical-quality-score member as the keeper."
        )
    elif all_transient_ui:
        rationale = (
            f"{len(members)} screenshots form a transient-UI burst, but fewer than 2 members "
            "could be quality-scored — no keeper identified; surfaced for manual review only."
        )
    else:
        rationale = (
            f"{len(members)} screenshots form a burst, but at least one member's OCR content "
            "tag is NOT transient-UI (receipt/document/code/chat/unknown) — no deletion "
            "suggestion made for any member; surfaced for manual review only, since a burst "
            "containing meaningful content must never be treated as uniformly disposable."
        )

    return AICluster(
        cluster_id=f"screenshot-burst-{cluster_index}",
        track=AITrack.SCREENSHOT_BURST,
        members=members,
        raw_score=float(max_pairwise_distance),
        score_kind="max_pairwise_hamming_distance",
        rationale=rationale,
    )
