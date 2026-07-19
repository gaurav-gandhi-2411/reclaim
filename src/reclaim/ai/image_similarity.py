from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from reclaim.ai.keep_best import score_image_quality, select_keep
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.phash import (
    ImageHashRecord,
    cluster_by_hamming_distance,
    compute_image_hashes,
    hamming_distance,
)
from reclaim.ai.safety import filter_paths_through_safety_validator
from reclaim.safety import SafetyValidator

# Feature 1a Track A end-to-end orchestration: safety-filter -> pHash/dHash prefilter ->
# Hamming-distance clustering -> classical keep-best scoring -> AICluster construction. Not
# wired to any CLI/dashboard surface yet (see ADR-0011's "no UI wiring" posture) — this is a
# library function a future feature/CLI command will call.


def build_near_identical_clusters(
    image_paths: Sequence[Path],
    *,
    safety: SafetyValidator,
    max_hamming_distance: int,
    hash_kind: str = "phash",
) -> list[AICluster]:
    """End-to-end Track A pipeline over `image_paths` (the caller's responsibility: this
    should already be the RESIDUAL after exact-hash/BLAKE3 dedup — spec §0.4's two-stage
    compute rule). Unreadable image files are skipped, never raised for.

    `max_hamming_distance` is the near-identical operating point — MEASURED at 14 on the real
    public INRIA Copydays dataset (see ADR-0012/ADR-0015), though still a caller-supplied
    parameter, not a hardcoded default here: this function has no opinion on whether its
    caller's chosen value matches the measured one, and the caller is responsible for citing
    the ADR if it presents a value as measured.
    """
    eligible_paths = filter_paths_through_safety_validator(image_paths, safety)

    hash_records: list[ImageHashRecord] = []
    for path in eligible_paths:
        record = compute_image_hashes(path)
        if record is not None:
            hash_records.append(record)

    raw_clusters = cluster_by_hamming_distance(
        hash_records, max_distance=max_hamming_distance, hash_kind=hash_kind
    )

    clusters: list[AICluster] = []
    for cluster_index, hash_group in enumerate(raw_clusters):
        quality_scores = [
            score
            for score in (score_image_quality(record.path) for record in hash_group)
            if score is not None
        ]
        if len(quality_scores) < 2:
            continue  # can't identify a keeper among fewer than 2 scoreable members

        keeper = select_keep(quality_scores)
        hashes_by_path = {record.path: record for record in hash_group}
        members = tuple(
            AIClusterMember(
                path=score.path,
                size_bytes=hashes_by_path[score.path].size_bytes,
                quality_score=score.combined,
                is_recommended_keep=(score.path == keeper.path),
            )
            for score in quality_scores
        )

        member_hashes = [
            record.phash_hex if hash_kind == "phash" else record.dhash_hex for record in hash_group
        ]
        max_pairwise_distance = max(
            hamming_distance(member_hashes[i], member_hashes[j])
            for i in range(len(member_hashes))
            for j in range(i + 1, len(member_hashes))
        )

        clusters.append(
            AICluster(
                cluster_id=f"near-identical-{cluster_index}",
                track=AITrack.NEAR_IDENTICAL_IMAGE,
                members=members,
                raw_score=float(max_pairwise_distance),
                score_kind="max_pairwise_hamming_distance",
                rationale=(
                    f"{len(members)} images within Hamming distance {max_hamming_distance} "
                    f"({hash_kind}) of each other — near-identical; recommending the "
                    "highest classical-quality-score member as the keeper."
                ),
            )
        )
    return clusters
