from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.api import ai_orchestration
from reclaim.config import Config
from reclaim.models import FileRecord
from reclaim.safety import SafetyValidator

# ADR-0025: unit coverage for the orchestration layer that wires reclaim.ai's already-tested
# pipelines into one dashboard-facing analysis pass. Endpoint-level behavior (status/analyze/
# suggestions, degraded mode) is covered separately in tests/test_api_ai.py; the critical
# apply-safety proof lives in tests/test_api_ai_apply_safety.py.


def _file_record(path: Path, *, size_bytes: int = 100, is_dir: bool = False) -> FileRecord:
    return FileRecord(
        path=path,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=0,
        ext=path.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=True,
    )


# --- ai_extra_available -----------------------------------------------------------------------


def test_ai_extra_available_true_when_probe_module_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "imagehash" else None
    )
    assert ai_orchestration.ai_extra_available() is True


def test_ai_extra_available_false_when_probe_module_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert ai_orchestration.ai_extra_available() is False


# --- classify_scan_files -----------------------------------------------------------------------


def test_classify_scan_files_splits_images_and_documents_and_skips_directories(
    tmp_path: Path,
) -> None:
    records = [
        _file_record(tmp_path / "photo.jpg"),
        _file_record(tmp_path / "report.docx"),
        _file_record(tmp_path / "notes.txt"),
        _file_record(tmp_path / "archive.zip"),  # neither image nor document
        _file_record(tmp_path / "a_directory", is_dir=True),
    ]

    classified = ai_orchestration.classify_scan_files(records)

    assert classified.image_paths == (tmp_path / "photo.jpg",)
    assert set(classified.document_paths) == {tmp_path / "report.docx", tmp_path / "notes.txt"}
    assert classified.images_capped == 0
    assert classified.documents_capped == 0
    assert classified.images_skipped_too_large == 0
    assert classified.documents_skipped_too_large == 0


def test_classify_scan_files_counts_oversized_files_without_silently_dropping_the_rest(
    tmp_path: Path,
) -> None:
    oversized = _file_record(
        tmp_path / "huge.jpg", size_bytes=ai_orchestration.MAX_IMAGE_FILE_BYTES + 1
    )
    normal = _file_record(tmp_path / "normal.jpg", size_bytes=100)

    classified = ai_orchestration.classify_scan_files([oversized, normal])

    assert classified.image_paths == (tmp_path / "normal.jpg",)
    assert classified.images_skipped_too_large == 1


def test_classify_scan_files_caps_count_and_reports_it(tmp_path: Path) -> None:
    records = [
        _file_record(tmp_path / f"photo_{i}.jpg")
        for i in range(ai_orchestration.MAX_IMAGES_FOR_NEAR_IDENTICAL + 5)
    ]

    classified = ai_orchestration.classify_scan_files(records)

    assert len(classified.image_paths) == ai_orchestration.MAX_IMAGES_FOR_NEAR_IDENTICAL
    assert classified.images_capped == 5


@pytest.mark.parametrize(
    "name",
    ["Screenshot 2024-01-01.png", "screen shot.png", "scrnli_123.png", "screen_capture.png"],
)
def test_classify_scan_files_screenshot_heuristic_matches_common_names(
    tmp_path: Path, name: str
) -> None:
    classified = ai_orchestration.classify_scan_files([_file_record(tmp_path / name)])
    assert classified.screenshot_candidate_paths == (tmp_path / name,)


def test_classify_scan_files_screenshot_heuristic_excludes_ordinary_photo_names(
    tmp_path: Path,
) -> None:
    classified = ai_orchestration.classify_scan_files([_file_record(tmp_path / "vacation.jpg")])
    assert classified.screenshot_candidate_paths == ()


# --- _split_near_dup_and_version_chains ---------------------------------------------------------


def _near_dup_cluster(
    members: tuple[AIClusterMember, ...], *, raw_score: float = 0.97
) -> AICluster:
    return AICluster(
        cluster_id="near-dup-document-0-0",
        track=AITrack.NEAR_DUP_DOCUMENT,
        members=members,
        raw_score=raw_score,
        score_kind="min_pairwise_cosine_similarity_within_cluster",
        rationale="test fixture",
    )


def test_split_promotes_a_filename_versioned_near_dup_cluster_to_version_chain(
    tmp_path: Path,
) -> None:
    v1 = tmp_path / "report_v1.docx"
    v2 = tmp_path / "report_v2.docx"
    v1.write_bytes(b"draft")
    v2.write_bytes(b"final draft")
    now = 1_700_000_000.0
    os.utime(v1, (now, now))
    os.utime(v2, (now + 100, now + 100))

    cluster = _near_dup_cluster(
        (
            AIClusterMember(path=v1, size_bytes=5, is_recommended_keep=True),
            AIClusterMember(path=v2, size_bytes=11),
        )
    )

    result = ai_orchestration._split_near_dup_and_version_chains([cluster])

    assert len(result) == 1
    assert result[0].track is AITrack.VERSION_CHAIN
    # The newest-ordered file (v2, later mtime AND higher filename rank -- signals agree) is
    # the recommended keep, not v1 (which the ORIGINAL near-dup cluster had marked as keeper).
    keeper = next(m for m in result[0].members if m.is_recommended_keep)
    assert keeper.path == v2


def test_split_leaves_a_non_versioned_near_dup_cluster_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "resume_alice.docx"
    b = tmp_path / "resume_bob.docx"
    a.write_bytes(b"x")
    b.write_bytes(b"y")

    cluster = _near_dup_cluster(
        (
            AIClusterMember(path=a, size_bytes=1, is_recommended_keep=True),
            AIClusterMember(path=b, size_bytes=1),
        )
    )

    result = ai_orchestration._split_near_dup_and_version_chains([cluster])

    assert result == [cluster]


# --- run_ai_analysis (end-to-end, empty input -- never needs a real ai-extra dependency) --------


def test_run_ai_analysis_with_no_files_runs_every_pipeline_and_finds_nothing(
    tmp_path: Path,
) -> None:
    """An empty file set never triggers a single heavy optional import in ANY pipeline (every
    pipeline's safety-filter -> compute loop is a no-op over an empty list) -- this proves the
    full orchestration wiring end-to-end without needing the `ai` extra installed."""
    safety = SafetyValidator(Config())

    result = ai_orchestration.run_ai_analysis(records=[], safety=safety)

    assert result.clusters == []
    assert result.files_considered == {"images": 0, "documents": 0, "screenshot_candidates": 0}
    assert set(result.tracks_run) == {
        AITrack.NEAR_IDENTICAL_IMAGE.value,
        AITrack.SEMANTIC_IMAGE.value,
        "near_dup_document_and_version_chain",
        AITrack.SCREENSHOT_BURST.value,
    }
    assert result.tracks_skipped == []


def test_run_ai_analysis_reports_capped_and_oversized_counts(tmp_path: Path) -> None:
    safety = SafetyValidator(Config())
    oversized = _file_record(
        tmp_path / "huge.png", size_bytes=ai_orchestration.MAX_IMAGE_FILE_BYTES + 1
    )

    result = ai_orchestration.run_ai_analysis(records=[oversized], safety=safety)

    assert result.files_capped["images_over_size_cap"] == 1


# --- clutter-ranker ordering fallback ------------------------------------------------------------


def _browse_only_cluster(cluster_id: str, *, size_bytes: int) -> AICluster:
    return AICluster(
        cluster_id=cluster_id,
        track=AITrack.SEMANTIC_IMAGE,
        members=(AIClusterMember(path=Path(f"{cluster_id}.jpg"), size_bytes=size_bytes),),
        raw_score=0.1,
        score_kind="max_pairwise_cosine_distance",
        rationale="test fixture",
    )


def _suggestion_cluster(cluster_id: str, *, size_bytes: int) -> AICluster:
    keep = AIClusterMember(
        path=Path(f"{cluster_id}_keep.jpg"), size_bytes=size_bytes, is_recommended_keep=True
    )
    drop = AIClusterMember(path=Path(f"{cluster_id}_drop.jpg"), size_bytes=size_bytes)
    return AICluster(
        cluster_id=cluster_id,
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=2.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test fixture",
    )


def test_fallback_order_puts_deletion_suggestions_before_browse_only() -> None:
    browse = _browse_only_cluster("browse-1", size_bytes=999_999)
    suggestion = _suggestion_cluster("suggest-1", size_bytes=10)

    ordered = ai_orchestration._fallback_order([browse, suggestion])

    assert [c.cluster_id for c in ordered] == ["suggest-1", "browse-1"]


def test_fallback_order_sorts_by_total_cluster_size_within_the_same_group() -> None:
    small = _suggestion_cluster("small", size_bytes=10)
    large = _suggestion_cluster("large", size_bytes=10_000)

    ordered = ai_orchestration._fallback_order([small, large])

    assert [c.cluster_id for c in ordered] == ["large", "small"]


def test_apply_clutter_ranker_ordering_falls_back_when_model_unavailable(tmp_path: Path) -> None:
    """Whether the real cause is a missing `lightgbm` install or a missing model file, both are
    caught the same way (ADR-0025 decision 3/4): ordering falls back to the deterministic
    default and the skip is recorded, never an unhandled exception."""
    from reclaim.api.ai_orchestration import AIAnalysisResult

    result = AIAnalysisResult(
        clusters=[
            _browse_only_cluster("browse-1", size_bytes=1),
            _suggestion_cluster("suggest-1", size_bytes=1),
        ]
    )

    ai_orchestration._apply_clutter_ranker_ordering(
        result, clutter_ranker_model_path=tmp_path / "does-not-exist.txt"
    )

    assert [c.cluster_id for c in result.clusters] == ["suggest-1", "browse-1"]
    assert any(skip.track == "ranked_clutter_ordering" for skip in result.tracks_skipped)
