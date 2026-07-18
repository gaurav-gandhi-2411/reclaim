from __future__ import annotations

from dataclasses import dataclass, field

from reclaim.ai.models import AICluster

# This module — deliberately — never imports `reclaim.executor`, never constructs a
# `reclaim.models.Candidate`, and exposes no method that returns or accepts one. It is the
# ONLY place AI-layer output lands (spec §0.1); see evals/test_ai_safety_gate.py for the
# static and runtime proofs that this boundary holds.


@dataclass(slots=True)
class AIReviewQueue:
    """In-memory holding area for AI-produced clusters, split into the two spec-mandated
    views: deletion suggestions (near-identical track only) and everything else
    (browse/ranking-only). No method here writes to disk, calls `apply_batch`, or otherwise
    touches the deterministic engine — this class only accumulates and partitions data the
    caller already computed.
    """

    clusters: list[AICluster] = field(default_factory=list)

    def add(self, cluster: AICluster) -> None:
        self.clusters.append(cluster)

    def deletion_suggestions(self) -> list[AICluster]:
        """Near-identical clusters with an identified keep-best member — the only AI output
        ever framed as a deletion suggestion, and even then, recommend-only: nothing in this
        class (or anywhere in `reclaim.ai`) can act on it."""
        return [cluster for cluster in self.clusters if cluster.suggests_deletion]

    def browse_only(self) -> list[AICluster]:
        return [cluster for cluster in self.clusters if not cluster.suggests_deletion]
