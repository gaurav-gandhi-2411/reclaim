from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Reserved category-group namespace prefix for anything the AI layer ever surfaces —
# never emitted by the deterministic engine's own detectors (reclaim.detectors). This is
# a structural, testable boundary (evals/test_ai_safety_gate.py) between "the deterministic
# engine's candidate namespace" and "the AI layer's namespace," independent of the type-level
# separation below.
AI_CATEGORY_GROUP_PREFIX = "ai_"


class AITrack(StrEnum):
    """Which AI pipeline produced a review-queue entry.

    Only tracks in `_DELETION_SUGGESTION_ELIGIBLE_TRACKS` may ever carry a deletion
    suggestion (spec §0.7/§2: "near-identical ... may produce a deletion suggestion; semantic
    similarity is browse-grouping only" for images, and "deletion suggestions only for
    high-similarity near-dups; version-chains are ordered recommendations" for documents) —
    see `AICluster.suggests_deletion`. `SEMANTIC_IMAGE`/`RANKED_CLUTTER` remain browse/
    ranking-only placeholders for future features (1a-Track-B/3). `SCREENSHOT_BURST` is
    deletion-eligible at the TRACK level, but Feature 2's orchestration
    (`screenshot_review.build_screenshot_burst_clusters`) only ever sets a keeper when EVERY
    member's OCR content tag is `content_tagger.ContentTag.TRANSIENT_UI` — a burst containing
    any receipt/document/code/chat/unknown content downgrades that specific cluster to
    browse-only, same conditional-keeper posture as `VERSION_CHAIN`'s `version_signals_agree`.
    """

    NEAR_IDENTICAL_IMAGE = "near_identical_image"  # Feature 1a Track A — deletion-eligible
    SEMANTIC_IMAGE = "semantic_image"  # Feature 1a Track B — browse-only, future
    NEAR_DUP_DOCUMENT = "near_dup_document"  # Feature 1b — deletion-eligible (high-similarity)
    VERSION_CHAIN = "version_chain"  # Feature 1b — deletion-eligible (keep-latest recommendation)
    SCREENSHOT_BURST = "screenshot_burst"  # Feature 2 — deletion-eligible, gated on
    # transient-UI content tag only (see class docstring)
    RANKED_CLUTTER = "ranked_clutter"  # Feature 3 — ranking-only, future


_DELETION_SUGGESTION_ELIGIBLE_TRACKS = frozenset(
    {
        AITrack.NEAR_IDENTICAL_IMAGE,
        AITrack.NEAR_DUP_DOCUMENT,
        AITrack.VERSION_CHAIN,
        AITrack.SCREENSHOT_BURST,
    }
)


@dataclass(frozen=True, slots=True)
class AIClusterMember:
    """One file inside an AI-produced cluster.

    Deliberately shares NO field names or base class with `reclaim.models.Candidate` — no
    `safety_verdict`, no `retention_days`, no `tier`. `reclaim.executor.apply_batch` accesses
    those attributes unconditionally on every item in its input list; handing it a list of
    these instead raises `AttributeError` immediately, before any filesystem call, rather
    than silently proceeding (see evals/test_ai_safety_gate.py for the executed proof).
    """

    path: Path
    size_bytes: int
    quality_score: float | None = None  # keep-best scorer output, if computed for this member
    is_recommended_keep: bool = False
    position: int | None = None  # VERSION_CHAIN only: 0-indexed chain order, oldest first


@dataclass(frozen=True, slots=True)
class AICluster:
    """One AI-produced group: a near-dup cluster, a semantic group, a version chain, a
    screenshot burst, or (Feature 3) a ranked-clutter entry.

    `raw_score`/`score_kind` are always a real measured distance/similarity/Jaccard index,
    never a manufactured probability (spec §0.6) — `score_kind` exists precisely so nothing
    downstream can misrepresent "0.87 cosine similarity" as "87% confidence."
    """

    cluster_id: str
    track: AITrack
    members: tuple[AIClusterMember, ...]
    raw_score: float
    score_kind: str
    rationale: str

    def __post_init__(self) -> None:
        if self.track not in _DELETION_SUGGESTION_ELIGIBLE_TRACKS and any(
            member.is_recommended_keep for member in self.members
        ):
            raise ValueError(
                f"track {self.track!r} is browse/ranking-only (not in "
                f"_DELETION_SUGGESTION_ELIGIBLE_TRACKS) — it must never carry an "
                "is_recommended_keep flag on any member, since that flag only has meaning "
                "alongside a deletion suggestion for the cluster's other members."
            )

    @property
    def suggests_deletion(self) -> bool:
        """True only for a deletion-eligible track AND only once a keep-best member has
        actually been identified — a near-identical cluster with no scored keeper yet is
        just a browse group, not a suggestion."""
        return self.track in _DELETION_SUGGESTION_ELIGIBLE_TRACKS and any(
            member.is_recommended_keep for member in self.members
        )
