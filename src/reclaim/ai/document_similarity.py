from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from reclaim.ai.document_keep_best import select_document_keep
from reclaim.ai.document_text import extract_text
from reclaim.ai.minhash_lsh import (
    DocumentMinHash,
    cluster_by_jaccard_similarity,
    compute_document_minhash,
)
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.safety import filter_paths_through_safety_validator
from reclaim.ai.text_embeddings import compute_document_embedding, cosine_similarity
from reclaim.safety import SafetyValidator

# Feature 1b end-to-end orchestration: safety-filter -> local text extraction -> MinHash/LSH
# Stage-1 prefilter -> sentence-embedding Stage-2 confirmation on the RESIDUAL ONLY (spec
# §0.4's two-stage compute rule: Stage 2 only ever runs on documents that already appeared in
# a Stage-1 candidate cluster, never on the full corpus) -> classical keep-best. Mirrors
# image_similarity.py's role for Feature 1a Track A. Not wired to any CLI/dashboard surface
# yet (ADR-0011's "no UI wiring" posture, still true for this feature).


class _UnionFind:
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


def build_near_dup_document_clusters(
    document_paths: Sequence[Path],
    *,
    safety: SafetyValidator,
    minhash_threshold: float,
    embedding_threshold: float,
    num_perm: int = 128,
) -> list[AICluster]:
    """`minhash_threshold`/`embedding_threshold` are the near-dup-document operating points —
    MEASURED at 0.2 / 0.6 respectively on a real, realistic public-domain document distribution
    (see ADR-0017). Still caller-supplied parameters, not defaults hardcoded here. Unreadable/
    unsupported files are skipped, never raised for.
    """
    eligible_paths = filter_paths_through_safety_validator(document_paths, safety)

    text_by_path: dict[Path, str] = {}
    minhash_records: list[DocumentMinHash] = []
    for path in eligible_paths:
        text = extract_text(path)
        if text is None:
            continue
        text_by_path[path] = text
        record = compute_document_minhash(path, text, num_perm=num_perm)
        if record is not None:
            minhash_records.append(record)

    stage1_groups = cluster_by_jaccard_similarity(minhash_records, min_similarity=minhash_threshold)

    # Stage 2 embeddings are computed ONLY for documents that appear in at least one Stage-1
    # candidate cluster — the residual, never the full corpus (spec §0.4).
    residual_paths = {record.path for group in stage1_groups for record in group}
    embeddings_by_path = {
        path: embedding
        for path in residual_paths
        if (embedding := compute_document_embedding(path, text_by_path[path])) is not None
    }

    clusters: list[AICluster] = []
    for group_index, group in enumerate(stage1_groups):
        confirmable = [record for record in group if record.path in embeddings_by_path]
        if len(confirmable) < 2:
            continue

        sub_union = _UnionFind(len(confirmable))
        for i in range(len(confirmable)):
            for j in range(i + 1, len(confirmable)):
                similarity = cosine_similarity(
                    embeddings_by_path[confirmable[i].path], embeddings_by_path[confirmable[j].path]
                )
                if similarity >= embedding_threshold:
                    sub_union.union(i, j)

        sub_groups: dict[int, list[DocumentMinHash]] = {}
        for index, record in enumerate(confirmable):
            sub_groups.setdefault(sub_union.find(index), []).append(record)

        for sub_group in sub_groups.values():
            if len(sub_group) < 2:
                continue
            paths = [record.path for record in sub_group]
            keeper_path = select_document_keep(paths)
            members = tuple(
                AIClusterMember(
                    path=record.path,
                    size_bytes=record.path.stat().st_size,
                    is_recommended_keep=(record.path == keeper_path),
                )
                for record in sub_group
            )
            # The WEAKEST link, not the strongest — same "worst case within the cluster"
            # reasoning as image_similarity.py reporting max_pairwise_distance (its loosest
            # pair); here, similarity is the score, so the loosest pair is the MINIMUM.
            min_pairwise_similarity = min(
                cosine_similarity(embeddings_by_path[a.path], embeddings_by_path[b.path])
                for idx_a, a in enumerate(sub_group)
                for b in sub_group[idx_a + 1 :]
            )
            clusters.append(
                AICluster(
                    cluster_id=f"near-dup-document-{group_index}-{len(clusters)}",
                    track=AITrack.NEAR_DUP_DOCUMENT,
                    members=members,
                    raw_score=min_pairwise_similarity,
                    score_kind="min_pairwise_cosine_similarity_within_cluster",
                    rationale=(
                        f"{len(members)} documents passed MinHash/LSH prefilter "
                        f"(Jaccard >= {minhash_threshold}) and sentence-embedding confirmation "
                        f"(cosine >= {embedding_threshold}) — recommending the largest, most "
                        "recently modified member as the keeper."
                    ),
                )
            )
    return clusters
