from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.presentation import (
    BROWSE_ONLY_NOTE,
    VERSION_DISAGREEMENT_NOTE,
    ClusterPresentation,
    present_cluster,
)

# No AICluster constructed here touches the filesystem (`present_cluster` never calls
# `Path.stat`/`.exists` — only `.as_posix()`), so fake, non-existent paths are safe fixtures.


def _member(
    path: str,
    *,
    quality_score: float | None = None,
    is_recommended_keep: bool = False,
    position: int | None = None,
) -> AIClusterMember:
    return AIClusterMember(
        path=Path(path),
        size_bytes=1024,
        quality_score=quality_score,
        is_recommended_keep=is_recommended_keep,
        position=position,
    )


# --- NEAR_IDENTICAL_IMAGE ------------------------------------------------------------------


def test_near_identical_image_names_keeper_and_states_mechanism_not_specific_claim() -> None:
    cluster = AICluster(
        cluster_id="near-identical-0",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(
            _member("C:/photos/img1.jpg", quality_score=8.5, is_recommended_keep=True),
            _member("C:/photos/img1_copy.jpg", quality_score=6.1, is_recommended_keep=False),
        ),
        raw_score=3.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="2 images within Hamming distance 14 (phash) of each other.",
    )
    presentation = present_cluster(cluster)

    assert presentation.headline == "These look like the same photo saved more than once"
    assert presentation.is_suggestion is True
    assert presentation.keep_path == "C:/photos/img1.jpg"
    assert presentation.technical_detail == "Hamming distance 3 of 64 bits (lower = more similar)"
    # No fabricated percentage or overclaimed "it IS sharper" — the mechanism (what the check
    # measures), not a specific sub-signal the data doesn't preserve.
    joined = " ".join(presentation.detail_lines)
    assert "sharpness/resolution/exposure" in joined
    assert "%" not in joined
    assert "%" not in presentation.technical_detail


def test_near_identical_image_technical_detail_never_a_percentage_across_the_score_range() -> None:
    for raw in (0.0, 1.0, 14.0, 63.0):
        cluster = AICluster(
            cluster_id="c",
            track=AITrack.NEAR_IDENTICAL_IMAGE,
            members=(
                _member("a.jpg", quality_score=1.0, is_recommended_keep=True),
                _member("b.jpg", quality_score=0.5, is_recommended_keep=False),
            ),
            raw_score=raw,
            score_kind="max_pairwise_hamming_distance",
            rationale="r",
        )
        detail = present_cluster(cluster).technical_detail
        assert "%" not in detail
        assert "of 64 bits" in detail


# --- SEMANTIC_IMAGE -------------------------------------------------------------------------


def test_semantic_image_is_explicitly_browse_only() -> None:
    cluster = AICluster(
        cluster_id="semantic-0",
        track=AITrack.SEMANTIC_IMAGE,
        members=(_member("a.jpg"), _member("b.jpg"), _member("c.jpg")),
        raw_score=0.12,
        score_kind="max_pairwise_cosine_distance",
        rationale="3 images share similar semantic content (CLIP cosine ...).",
    )
    presentation = present_cluster(cluster)

    assert presentation.headline == "3 photos from what looks like the same scene"
    assert presentation.is_suggestion is False
    assert presentation.keep_path is None
    assert presentation.browse_only_note == BROWSE_ONLY_NOTE
    assert "cosine distance" in presentation.technical_detail
    assert "%" not in presentation.technical_detail


# --- NEAR_DUP_DOCUMENT ------------------------------------------------------------------------


def test_near_dup_document_names_recommended_keeper_with_no_fabricated_chain_order() -> None:
    cluster = AICluster(
        cluster_id="near-dup-document-0",
        track=AITrack.NEAR_DUP_DOCUMENT,
        members=(
            _member("report_draft.docx", is_recommended_keep=False),
            _member("report_final.docx", is_recommended_keep=True),
        ),
        raw_score=0.94,
        score_kind="min_pairwise_cosine_similarity_within_cluster",
        rationale="2 documents are near-duplicates (cosine >= 0.9).",
    )
    presentation = present_cluster(cluster)

    assert presentation.headline == "Looks like earlier drafts of the same document"
    assert presentation.is_suggestion is True
    assert presentation.keep_path == "report_final.docx"
    assert presentation.browse_only_note is None


# --- VERSION_CHAIN --------------------------------------------------------------------------


def test_version_chain_agreeing_signals_orders_chain_and_names_newest() -> None:
    cluster = AICluster(
        cluster_id="version-chain-0",
        track=AITrack.VERSION_CHAIN,
        members=(
            _member("report_v1.docx", position=0, is_recommended_keep=False),
            _member("report_v2.docx", position=1, is_recommended_keep=False),
            _member("report_v3_final.docx", position=2, is_recommended_keep=True),
        ),
        raw_score=0.91,
        score_kind="min_pairwise_content_similarity_within_chain",
        rationale="3 files ordered as a version chain by filename pattern + mtime.",
    )
    presentation = present_cluster(cluster)

    assert presentation.is_suggestion is True
    assert presentation.keep_path == "report_v3_final.docx"
    assert presentation.browse_only_note is None
    assert len(presentation.detail_lines) == 3
    assert "newest, recommended to keep" in presentation.detail_lines[-1]
    assert "1 of 3" in presentation.detail_lines[0]


def test_version_chain_disagreeing_signals_refuses_to_pick_a_keeper() -> None:
    """The safety property ADR-0017's version-chain follow-up describes: when filename-version
    and mtime signals disagree, no member is a recommended keep — the presentation layer must
    say exactly that, never silently pick one anyway."""
    cluster = AICluster(
        cluster_id="version-chain-1",
        track=AITrack.VERSION_CHAIN,
        members=(
            _member("report_final.docx", position=0),  # no is_recommended_keep anywhere
            _member("report_v2.docx", position=1),
        ),
        raw_score=0.9,
        score_kind="min_pairwise_content_similarity_within_chain",
        rationale="2 files show filename-version and modification-time signals that DISAGREE.",
    )
    presentation = present_cluster(cluster)

    assert presentation.is_suggestion is False
    assert presentation.keep_path is None
    assert presentation.browse_only_note == VERSION_DISAGREEMENT_NOTE
    assert presentation.detail_lines == ()


# --- SCREENSHOT_BURST -----------------------------------------------------------------------


def test_screenshot_burst_all_transient_ui_recommends_a_keeper_without_naming_the_tag() -> None:
    cluster = AICluster(
        cluster_id="screenshot-burst-0",
        track=AITrack.SCREENSHOT_BURST,
        members=(
            _member("shot1.png", quality_score=5.0, is_recommended_keep=True),
            _member("shot2.png", quality_score=4.0, is_recommended_keep=False),
        ),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="2 screenshots taken in a burst, all OCR-tagged transient-UI content.",
    )
    presentation = present_cluster(cluster)

    assert presentation.headline == "A run of near-identical screenshots"
    assert presentation.is_suggestion is True
    assert presentation.keep_path == "shot1.png"
    # The specific OCR content tag is a structural privacy boundary (screenshot_review.py's
    # PRIVACY LOCK) — this module must never invent/reconstruct it.
    joined = " ".join(presentation.detail_lines)
    assert "receipt" not in joined.lower() or "not a receipt" in joined.lower()


def test_screenshot_burst_mixed_content_refuses_a_keeper() -> None:
    cluster = AICluster(
        cluster_id="screenshot-burst-1",
        track=AITrack.SCREENSHOT_BURST,
        members=(_member("shot1.png"), _member("shot2.png")),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale=(
            "2 screenshots form a burst, but at least one member's OCR content tag is NOT "
            "transient-UI."
        ),
    )
    presentation = present_cluster(cluster)

    assert presentation.is_suggestion is False
    assert presentation.keep_path is None
    joined = " ".join(presentation.detail_lines)
    assert "won't suggest deleting" in joined


# --- RANKED_CLUTTER -------------------------------------------------------------------------


def test_ranked_clutter_is_framed_as_ordering_not_a_verdict() -> None:
    cluster = AICluster(
        cluster_id="ranked-clutter-0",
        track=AITrack.RANKED_CLUTTER,
        members=(_member("old_download.zip"),),
        raw_score=0.83,
        score_kind="clutter_likelihood_lambdamart",
        rationale="Generic clutter-likelihood ranker score.",
    )
    presentation = present_cluster(cluster)

    assert presentation.is_suggestion is False
    assert presentation.keep_path is None
    assert "not a verdict" in (presentation.browse_only_note or "")
    assert "not a probability" in presentation.technical_detail
    assert "%" not in presentation.technical_detail


# --- Cross-cutting: every track's technical_detail is present and non-empty -----------------


@pytest.mark.parametrize(
    ("track", "score_kind"),
    [
        (AITrack.NEAR_IDENTICAL_IMAGE, "max_pairwise_hamming_distance"),
        (AITrack.SEMANTIC_IMAGE, "max_pairwise_cosine_distance"),
        (AITrack.NEAR_DUP_DOCUMENT, "min_pairwise_cosine_similarity_within_cluster"),
        (AITrack.VERSION_CHAIN, "min_pairwise_content_similarity_within_chain"),
        (AITrack.SCREENSHOT_BURST, "max_pairwise_hamming_distance"),
        (AITrack.RANKED_CLUTTER, "clutter_likelihood_lambdamart"),
    ],
)
def test_every_track_returns_a_presentation_with_non_empty_technical_detail(
    track: AITrack, score_kind: str
) -> None:
    is_keep_eligible = track in {
        AITrack.NEAR_IDENTICAL_IMAGE,
        AITrack.NEAR_DUP_DOCUMENT,
        AITrack.VERSION_CHAIN,
        AITrack.SCREENSHOT_BURST,
    }
    members = (
        _member("a", is_recommended_keep=is_keep_eligible),
        _member("b", is_recommended_keep=False),
    )
    cluster = AICluster(
        cluster_id="x",
        track=track,
        members=members,
        raw_score=1.0,
        score_kind=score_kind,
        rationale="r",
    )
    presentation = present_cluster(cluster)
    assert isinstance(presentation, ClusterPresentation)
    assert presentation.technical_detail
    assert presentation.headline
