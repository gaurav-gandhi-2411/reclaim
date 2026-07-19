from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim.ai._optional import require
from reclaim.ai.image_embeddings import (
    ImageEmbedding,
    ImageEmbeddingCache,
    compute_embeddings_batch,
)
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.safety import filter_paths_through_safety_validator
from reclaim.safety import SafetyValidator

# Feature 1a Track B (spec §1, ADR-0022): groups the RESIDUAL after Track A's near-identical
# pHash clustering (image_similarity.py) already ran — this module never re-examines images
# Track A already clustered, it only groups what's LEFT by semantic (CLIP embedding cosine)
# similarity into "same scene/event" browse groups. BROWSE-ONLY, structurally: every cluster
# this module produces is tagged `AITrack.SEMANTIC_IMAGE`, which was excluded from
# `_DELETION_SUGGESTION_ELIGIBLE_TRACKS` since ADR-0011 and stays excluded here —
# `AICluster.__post_init__` refuses construction if any member of a SEMANTIC_IMAGE cluster
# ever carries `is_recommended_keep=True` (see evals/test_ai_safety_gate.py).
#
# ANN via FAISS's `IndexHNSWFlat` — the HNSW (Hierarchical Navigable Small World) algorithm
# the spec names, via a library that actually installs on this machine (see ADR-0022:
# hnswlib itself has no prebuilt Windows wheel for this Python version and needs MSVC Build
# Tools to compile from source; FAISS ships its own HNSW index implementation and a prebuilt
# wheel). Vectors are L2-normalized before indexing so inner-product search is equivalent to
# cosine similarity search — the same "raw cosine similarity, never a manufactured
# probability" reporting discipline as every other similarity signal in this layer (spec §0.6).

_DEFAULT_SIMILARITY_THRESHOLD = 0.82  # MEASURED (ADR-0022): BCubed precision 0.7897, recall
# 0.7143 on real INRIA Copydays blocks — the F1-maximizing knee of the swept 0.70-1.00
# tradeoff curve, and the point selected under "maximize recall subject to both floors"
# (precision >= 0.70, recall >= 0.20). Still an explicit caller-supplied parameter (not
# silently hardcoded elsewhere), so a future re-measurement only requires changing this one
# constant, not every call site.
_HNSW_M = 32  # FAISS HNSW's "M" construction parameter (neighbors per node) — a reasonable
# default for a personal photo library's scale (thousands, not millions, of residual images);
# not independently tuned, disclosed as a default rather than a measured value.


@dataclass(frozen=True, slots=True)
class SemanticGroup:
    members: tuple[Path, ...]
    max_pairwise_distance: float  # 1.0 - min_pairwise_cosine_similarity within the group —
    # reported as a distance (lower = tighter group) for consistency with every other
    # AICluster.raw_score in this codebase, which reports a distance/dissimilarity, not a
    # similarity, when the track's `score_kind` says "distance."


class _UnionFind:
    """Same minimal disjoint-set as phash.py's/minhash_lsh.py's/screenshot_burst.py's —
    duplicated rather than shared for the same reason: no coupling between sibling AI
    pipelines beyond living under `reclaim.ai`."""

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


def group_by_semantic_similarity(
    embeddings: Sequence[ImageEmbedding],
    *,
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    k_neighbors: int = 10,
) -> list[SemanticGroup]:
    """Groups `embeddings` (the Track-A RESIDUAL — the caller's responsibility, same
    two-stage-compute discipline as every other AI pipeline in this codebase, spec §0.4) by
    CLIP cosine similarity via a FAISS HNSW approximate-nearest-neighbor index. A pair is
    unioned iff their cosine similarity clears `similarity_threshold` — `k_neighbors` bounds
    how many nearest neighbors are queried per item (an ANN index approximation parameter,
    not a clustering parameter; raising it costs query time for a chance at finding
    additional true neighbors HNSW's approximate search might otherwise miss).

    Singleton "groups" (nothing else within threshold) are dropped — not a group if there's
    nothing to group with, same convention as `screenshot_burst.cluster_screenshot_bursts`.
    """
    if len(embeddings) < 2:
        return []

    faiss = require("faiss", feature="semantic image grouping (ANN search)")
    numpy = require("numpy", feature="semantic image grouping (ANN search)")

    dimension = len(embeddings[0].vector)
    vectors = numpy.array([e.vector for e in embeddings], dtype="float32")
    faiss.normalize_L2(vectors)  # cosine similarity == inner product on normalized vectors

    index = faiss.IndexHNSWFlat(dimension, _HNSW_M, faiss.METRIC_INNER_PRODUCT)
    index.add(vectors)

    k = min(k_neighbors + 1, len(embeddings))  # +1: a vector's own nearest neighbor is itself
    similarities, neighbor_indices = index.search(vectors, k)

    union_find = _UnionFind(len(embeddings))
    pairwise_similarity: dict[tuple[int, int], float] = {}
    for i in range(len(embeddings)):
        for rank in range(k):
            j = int(neighbor_indices[i][rank])
            if j == i or j < 0:
                continue
            similarity = float(similarities[i][rank])
            if similarity >= similarity_threshold:
                union_find.union(i, j)
                pair_key = (min(i, j), max(i, j))
                pairwise_similarity[pair_key] = similarity

    groups: dict[int, list[int]] = {}
    for index_i in range(len(embeddings)):
        groups.setdefault(union_find.find(index_i), []).append(index_i)

    result: list[SemanticGroup] = []
    for member_indices in groups.values():
        if len(member_indices) < 2:
            continue
        pair_similarities = [
            pairwise_similarity[(min(a, b), max(a, b))]
            for idx_a, a in enumerate(member_indices)
            for b in member_indices[idx_a + 1 :]
            if (min(a, b), max(a, b)) in pairwise_similarity
        ]
        # Some within-group pairs may not have been directly compared (HNSW is approximate —
        # two members could be unioned transitively through a third without ever appearing as
        # each other's k-nearest-neighbor) — the max-distance report only reflects DIRECTLY
        # measured pairs, never fabricates a number for a pair that was never compared.
        max_distance = 1.0 - min(pair_similarities) if pair_similarities else 0.0
        result.append(
            SemanticGroup(
                members=tuple(embeddings[i].path for i in member_indices),
                max_pairwise_distance=max_distance,
            )
        )
    return result


def build_semantic_image_clusters(
    residual_image_paths: Sequence[Path],
    *,
    safety: SafetyValidator,
    embedding_cache_path: Path | None = None,
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> list[AICluster]:
    """End-to-end Track B pipeline: safety-filter -> CLIP embedding (cached) -> FAISS-HNSW
    semantic grouping -> `AICluster` construction on `AITrack.SEMANTIC_IMAGE`. Mirrors
    `image_similarity.build_near_identical_clusters`'s exact shape (same safety-filter ->
    compute -> cluster -> `AICluster` structure), except NO keep-best scoring step and NO
    `is_recommended_keep` ever set on any member — Track B is browse-tidiness only, never a
    deletion suggestion (see `AICluster.__post_init__`'s structural guard, proven in
    `evals/test_ai_safety_gate.py`).

    `residual_image_paths` must ALREADY be the residual after Track A's near-identical pHash
    clustering (`image_similarity.build_near_identical_clusters`) — this function never
    re-examines images Track A already clustered, same "caller's responsibility" two-stage-
    compute discipline as every other AI pipeline in this codebase (spec §0.4).
    """
    eligible_paths = filter_paths_through_safety_validator(residual_image_paths, safety)

    if embedding_cache_path is not None:
        with ImageEmbeddingCache(embedding_cache_path) as cache:
            embeddings = compute_embeddings_batch(eligible_paths, cache=cache)
    else:
        embeddings = compute_embeddings_batch(eligible_paths)

    semantic_groups = group_by_semantic_similarity(
        embeddings, similarity_threshold=similarity_threshold
    )

    clusters: list[AICluster] = []
    for group_index, group in enumerate(semantic_groups):
        members = tuple(
            AIClusterMember(path=path, size_bytes=path.stat().st_size)
            for path in group.members
            if path.exists()
        )
        if len(members) < 2:
            continue  # a member vanished between grouping and cluster construction -- not
            # a group anymore, silently dropped (same "skip, don't abort" posture as the
            # rest of this layer).
        clusters.append(
            AICluster(
                cluster_id=f"semantic-image-{group_index}",
                track=AITrack.SEMANTIC_IMAGE,
                members=members,
                raw_score=group.max_pairwise_distance,
                score_kind="max_pairwise_cosine_distance",
                rationale=(
                    f"{len(members)} images share similar semantic content (CLIP cosine "
                    f"similarity >= {similarity_threshold}) — browse-only grouping, never a "
                    "deletion suggestion."
                ),
            )
        )
    return clusters
