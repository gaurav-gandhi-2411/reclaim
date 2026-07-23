from __future__ import annotations

from dataclasses import dataclass

from reclaim.ai.models import AICluster, AIClusterMember, AITrack

# Plain-language presentation layer for AI-produced clusters (reclaim-ai-features-spec.md
# §0.6/§0.7). Translates an already-computed `AICluster`'s real, measured `raw_score`/
# `score_kind`/`rationale` into copy safe to show a non-technical user — this module never
# invents a judgment the cluster itself doesn't already carry, and never converts a distance/
# similarity into a fabricated confidence percentage (house rule: no invented confidence
# anywhere in UI copy). The raw number stays available, unconverted and clearly labeled, in
# `ClusterPresentation.technical_detail` for a collapsed "details" affordance aimed at
# technical users — never folded into `headline`/`detail_lines`, which stay pure prose.
#
# SCOPE NOTE: nothing in `reclaim.api` currently computes real `AICluster` instances for a live
# scan session — there is no orchestration path yet from image_similarity/document_similarity/
# screenshot_review/clutter_ranker/semantic_image_grouping into api/service.py (confirmed by
# grep during Stage 2 launch-UX work; the only existing consumer of `AICluster` output today is
# `reclaim.ai.labeling_app`, a separate gold-set-labeling tool, not the main dashboard). This
# module exists so that wiring, whenever it lands, has a ready-made, independently-tested
# translation layer to call rather than inventing UI copy ad hoc at the call site — it is not
# itself wired into any API route or the dashboard yet.

_PHASH_BITS = 64  # imagehash.phash's default hash_size=8 -> an 8x8 = 64-bit hash (phash.py's
# compute_image_hashes never overrides hash_size) — the width every NEAR_IDENTICAL_IMAGE/
# SCREENSHOT_BURST raw_score (a Hamming distance, score_kind="max_pairwise_hamming_distance")
# is measured against.

_HEADLINES: dict[AITrack, str] = {
    AITrack.NEAR_IDENTICAL_IMAGE: "These look like the same photo saved more than once",
    AITrack.SEMANTIC_IMAGE: "Photos from what looks like the same scene",
    AITrack.NEAR_DUP_DOCUMENT: "Looks like earlier drafts of the same document",
    AITrack.VERSION_CHAIN: "Looks like earlier drafts of the same document",
    AITrack.SCREENSHOT_BURST: "A run of near-identical screenshots",
    AITrack.RANKED_CLUTTER: "Possible clutter",
}

BROWSE_ONLY_NOTE = "Browse only — we won't suggest deleting any of these."
VERSION_DISAGREEMENT_NOTE = (
    "We can't tell which is newest, so we won't suggest deleting any of these."
)
RANKED_CLUTTER_LIST_LABEL = (
    "Sorted by what's most likely to be clutter — a suggestion for where to look first, not a "
    "verdict."
)

# `AICluster.suggests_deletion` already asserts these two are the only tracks a keep-best
# quality-score-driven comparison ever applies to (screenshot bursts only ever get a keeper
# when EVERY member is OCR-tagged transient-UI — see screenshot_review.py's PRIVACY LOCK, which
# is exactly why the specific content tag itself is deliberately unavailable here, see
# `_screenshot_content_note` below).
_QUALITY_SCORED_TRACKS = frozenset({AITrack.NEAR_IDENTICAL_IMAGE, AITrack.SCREENSHOT_BURST})


@dataclass(frozen=True, slots=True)
class ClusterPresentation:
    """Plain-language rendering of one `AICluster`, ready for a dashboard to display without
    interpolating any raw distance/similarity number into headline copy.

    `keep_path`/every path referenced anywhere in this module is a real filesystem path
    (attacker-controllable, same reasoning as `app.js::renderClusterTable`'s XSS-history
    comment) — a caller rendering this into HTML MUST use `textContent`/`dataset`, never
    innerHTML, exactly like the existing duplicate-cluster review table.
    """

    cluster_id: str
    track: AITrack
    headline: str
    detail_lines: tuple[str, ...]
    is_suggestion: bool
    browse_only_note: str | None
    keep_path: str | None
    technical_detail: str


def _technical_detail(cluster: AICluster) -> str:
    """The one place a raw measured number appears — always labeled with its unit/scale/
    direction, never converted into a percentage (house rule: no invented confidence)."""
    raw = cluster.raw_score
    kind = cluster.score_kind
    if kind == "max_pairwise_hamming_distance":
        return f"Hamming distance {int(raw)} of {_PHASH_BITS} bits (lower = more similar)"
    if kind == "max_pairwise_cosine_distance":
        return f"CLIP cosine distance {raw:.3f} (lower = more similar)"
    if kind in (
        "min_pairwise_cosine_similarity_within_cluster",
        "min_pairwise_content_similarity_within_chain",
    ):
        return f"Cosine similarity {raw:.3f} (higher = more similar)"
    if kind == "clutter_likelihood_lambdamart":
        return (
            f"Clutter-likelihood ranker score {raw:.3f} (higher sorts first — a ranking "
            "signal, not a probability)"
        )
    # Defensive fallback for a future score_kind this module hasn't been taught about yet.
    return f"{kind}: {raw:.3f} (a raw measured value, not a percentage)"  # pragma: no cover


def _keeper(cluster: AICluster) -> AIClusterMember | None:
    for member in cluster.members:
        if member.is_recommended_keep:
            return member
    return None


def _quality_reason(cluster: AICluster, keeper: AIClusterMember) -> str:
    """Honest keep-best explanation: `AIClusterMember.quality_score` is a single combined
    float (`keep_best._combine`'s weighted sum of sharpness + resolution + exposure) — the
    per-signal breakdown does not survive onto the member, so this states the MECHANISM (which
    signals the check considers) rather than asserting a specific one — e.g. "sharper" —
    dominated, which the data available here cannot actually verify."""
    others = [m for m in cluster.members if m is not keeper]
    comparison = ""
    if (
        keeper.quality_score is not None
        and others
        and all(m.quality_score is not None for m in others)
    ):
        best_other = max(m.quality_score for m in others if m.quality_score is not None)
        comparison = f" (quality check score {keeper.quality_score:.2f} vs {best_other:.2f})"
    return (
        "We recommend keeping this copy — it scored higher on our sharpness/resolution/"
        f"exposure quality check than the other cop{'y' if len(others) == 1 else 'ies'}."
        f"{comparison}"
    )


def _screenshot_content_note(cluster: AICluster) -> str:
    """`ContentTag` (receipt/document/code/chat/transient-UI) is deliberately never assigned to
    any `AICluster`/`AIClusterMember` field — see screenshot_review.py's PRIVACY LOCK comment,
    the raw OCR'd text (and the tag derived from it) lives only in that function's local scope.
    This module respects that boundary: it never reconstructs or guesses the specific tag, and
    only ever states the one fact the cluster's `suggests_deletion`/membership genuinely
    carries — whether every member cleared the transient-UI-only deletion-eligibility gate."""
    if cluster.suggests_deletion:
        return (
            "Every screenshot in this burst looks like disposable UI capture (not a receipt, "
            "document, or other saved content) — we recommend keeping the clearest copy."
        )
    return (
        "At least one screenshot in this burst may hold meaningful content (a receipt, "
        "document, code, or chat) — we won't suggest deleting any of these; review them "
        "yourself."
    )


def _version_chain_detail(cluster: AICluster) -> tuple[tuple[str, ...], str | None, str | None]:
    """Returns `(detail_lines, browse_only_note, keep_path)` for VERSION_CHAIN/NEAR_DUP_DOCUMENT.
    Only VERSION_CHAIN members ever carry a `position` (0-indexed, oldest first) — when present,
    this orders the chain explicitly; NEAR_DUP_DOCUMENT (no chain-ordering data) just names the
    recommended keeper without claiming an order it was never given."""
    has_positions = any(member.position is not None for member in cluster.members)
    keeper = _keeper(cluster)

    if cluster.track is AITrack.VERSION_CHAIN and not cluster.suggests_deletion:
        # `version_chain.build_version_chain_cluster` sets no keeper at all when the filename-
        # version and mtime signals disagree — exactly the case GG's instruction calls out.
        return ((), VERSION_DISAGREEMENT_NOTE, None)

    if has_positions:
        ordered = sorted(cluster.members, key=lambda m: m.position if m.position is not None else 0)
        lines = tuple(
            f"{index + 1} of {len(ordered)}"
            + (" — newest, recommended to keep" if member.is_recommended_keep else "")
            for index, member in enumerate(ordered)
        )
        keep_path = keeper.path.as_posix() if keeper is not None else None
        return (lines, None, keep_path)

    if keeper is not None:
        line = "We recommend keeping the newest/most complete copy."
        return ((line,), None, keeper.path.as_posix())
    # Defensive: NEAR_DUP_DOCUMENT always sets a keeper today (document_similarity.py's
    # select_document_keep is unconditional, unlike version_chain's signals-agree gate above).
    return ((), None, None)  # pragma: no cover


def present_cluster(cluster: AICluster) -> ClusterPresentation:
    """Builds the plain-language rendering for one `AICluster`. Every returned string is safe
    prose (no HTML); every path is a raw filesystem path a caller must render via `textContent`,
    never innerHTML (see `ClusterPresentation`'s docstring)."""
    headline = _HEADLINES[cluster.track]
    keeper = _keeper(cluster)
    detail_lines: tuple[str, ...] = ()
    browse_only_note: str | None = None
    keep_path: str | None = None

    if cluster.track is AITrack.SEMANTIC_IMAGE:
        headline = f"{len(cluster.members)} photos from what looks like the same scene"
        browse_only_note = BROWSE_ONLY_NOTE

    elif cluster.track in _QUALITY_SCORED_TRACKS:
        if cluster.track is AITrack.SCREENSHOT_BURST:
            detail_lines = (_screenshot_content_note(cluster),)
        if keeper is not None:
            detail_lines = (*detail_lines, _quality_reason(cluster, keeper))
            keep_path = keeper.path.as_posix()
        elif not cluster.suggests_deletion and cluster.track is AITrack.NEAR_IDENTICAL_IMAGE:
            browse_only_note = (
                "Fewer than two members could be quality-scored — no keeper identified; "
                "surfaced for review only."
            )

    elif cluster.track in (AITrack.NEAR_DUP_DOCUMENT, AITrack.VERSION_CHAIN):
        detail_lines, browse_only_note, keep_path = _version_chain_detail(cluster)

    elif cluster.track is AITrack.RANKED_CLUTTER:
        detail_lines = (RANKED_CLUTTER_LIST_LABEL,)
        browse_only_note = RANKED_CLUTTER_LIST_LABEL

    return ClusterPresentation(
        cluster_id=cluster.cluster_id,
        track=cluster.track,
        headline=headline,
        detail_lines=detail_lines,
        is_suggestion=cluster.suggests_deletion,
        browse_only_note=browse_only_note,
        keep_path=keep_path,
        technical_detail=_technical_detail(cluster),
    )
