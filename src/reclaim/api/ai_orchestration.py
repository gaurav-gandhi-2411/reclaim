from __future__ import annotations

import importlib.util
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from reclaim.ai._optional import AIExtraNotInstalledError
from reclaim.ai.clutter_ranker import DEFAULT_MODEL_PATH, ClutterRanker
from reclaim.ai.document_similarity import build_near_dup_document_clusters
from reclaim.ai.document_text import is_supported_document
from reclaim.ai.feedback_store import (
    ClusterStats,
    FeatureVector,
    SiblingDecisionContext,
    classify_path_class,
)
from reclaim.ai.image_similarity import build_near_identical_clusters
from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.ai.screenshot_review import build_screenshot_burst_clusters
from reclaim.ai.semantic_image_grouping import build_semantic_image_clusters
from reclaim.ai.version_chain import build_version_chain_cluster, filename_version_rank
from reclaim.models import FileRecord
from reclaim.safety import SafetyValidator
from reclaim.scanner import GitRepoCache, build_record_for_path

logger = structlog.get_logger(__name__)

# Orchestrates the already-built, already-tested `reclaim.ai` pipelines into one dashboard-
# facing analysis pass (ADR-0025). This is the ONLY module under `reclaim.api` that calls into
# `reclaim.ai`'s COMPUTE functions (image/document similarity, screenshot review, semantic
# grouping, the clutter ranker) -- `reclaim.api.service`/`routes` call into this module, never
# into `reclaim.ai` submodules directly, keeping the wiring surface in one reviewable place.
# Structurally unable to reach the executor: this module imports neither `reclaim.executor` nor
# `send2trash` (same AST-scanned guarantee `evals/test_ai_safety_gate.py` already proves for
# every file under `src/reclaim/ai/` -- this file additionally never even imports that package's
# `review_queue`/`safety` modules for anything beyond read-only clustering).

_PROBE_MODULE = "imagehash"


def ai_extra_available() -> bool:
    """Cheap, non-executing probe for whether the `ai` extra is installed at all --
    `importlib.util.find_spec` resolves whether a module COULD be imported without running its
    top-level code, so this costs nothing and pulls in zero heavy dependencies. `imagehash` is
    the single representative probe: every `ai`-extra dependency ships together as one `pip
    install reclaim[ai]` -- there is no supported partial install -- so if the most fundamental
    one (Track A's pHash prefilter) is missing, every other AI dependency will be too."""
    return importlib.util.find_spec(_PROBE_MODULE) is not None


# --- Bounded work (ADR-0025 decision 4): every cap here is counted and reported by the caller
# (`service.run_ai_analysis`), never a silent truncation. ---------------------------------------

MAX_IMAGE_FILE_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_FILE_BYTES = 15 * 1024 * 1024
MAX_IMAGES_FOR_NEAR_IDENTICAL = 1500
MAX_RESIDUAL_IMAGES_FOR_SEMANTIC = 300
MAX_DOCUMENTS_FOR_NEAR_DUP = 800

_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif"}
)

# A cheap filename prefilter for "this looks like a screenshot" -- bounds the OCR-bearing
# screenshot-burst pipeline to a plausible subset of the image set rather than every image.
# Disclosed limitation (ADR-0025): a renamed screenshot is invisible to this heuristic.
_SCREENSHOT_FILENAME_RE = re.compile(
    r"screen\s*shot|screenshot|scrnli|screen[_-]?capture|snip", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ClassifiedFiles:
    """The current scan's inventory, split by AI-relevant file kind, already cap-bounded."""

    image_paths: tuple[Path, ...]
    document_paths: tuple[Path, ...]
    screenshot_candidate_paths: tuple[Path, ...]
    images_capped: int
    documents_capped: int
    images_skipped_too_large: int
    documents_skipped_too_large: int


def classify_scan_files(records: Sequence[FileRecord]) -> ClassifiedFiles:
    """Splits a scan's file records into images/documents by extension, drops oversized files
    (counted, never silent), and bounds each list to its pipeline's count cap (also counted).
    Directories are skipped -- every AI pipeline here operates on files only."""
    images: list[Path] = []
    documents: list[Path] = []
    images_skipped_too_large = 0
    documents_skipped_too_large = 0

    for record in records:
        if record.is_dir:
            continue
        ext = record.ext.lower()
        if ext in _IMAGE_EXTENSIONS:
            if record.size_bytes > MAX_IMAGE_FILE_BYTES:
                images_skipped_too_large += 1
                continue
            images.append(record.path)
        elif is_supported_document(record.path):
            if record.size_bytes > MAX_DOCUMENT_FILE_BYTES:
                documents_skipped_too_large += 1
                continue
            documents.append(record.path)

    images_capped = max(0, len(images) - MAX_IMAGES_FOR_NEAR_IDENTICAL)
    images_bounded = images[:MAX_IMAGES_FOR_NEAR_IDENTICAL]
    documents_capped = max(0, len(documents) - MAX_DOCUMENTS_FOR_NEAR_DUP)
    documents_bounded = documents[:MAX_DOCUMENTS_FOR_NEAR_DUP]

    screenshot_candidates = tuple(
        path for path in images_bounded if _SCREENSHOT_FILENAME_RE.search(path.name)
    )

    return ClassifiedFiles(
        image_paths=tuple(images_bounded),
        document_paths=tuple(documents_bounded),
        screenshot_candidate_paths=screenshot_candidates,
        images_capped=images_capped,
        documents_capped=documents_capped,
        images_skipped_too_large=images_skipped_too_large,
        documents_skipped_too_large=documents_skipped_too_large,
    )


@dataclass(frozen=True, slots=True)
class PipelineSkip:
    """One pipeline's honest skip reason -- surfaced to the dashboard, never silently dropped."""

    track: str
    reason: str


@dataclass(slots=True)
class AIAnalysisResult:
    clusters: list[AICluster] = field(default_factory=list)
    tracks_run: list[str] = field(default_factory=list)
    tracks_skipped: list[PipelineSkip] = field(default_factory=list)
    files_considered: dict[str, int] = field(default_factory=dict)
    files_capped: dict[str, int] = field(default_factory=dict)


def _run_pipeline(
    track: str,
    tracks_run: list[str],
    tracks_skipped: list[PipelineSkip],
    fn: Callable[[], list[AICluster]],
) -> list[AICluster]:
    """Runs one pipeline; any failure (missing optional dependency, or an unexpected error) is
    recorded as a skip and never aborts the rest of the analysis -- one pipeline's problem must
    never sink every other pipeline's real results (ADR-0025 decision 3)."""
    try:
        clusters = fn()
    except AIExtraNotInstalledError as exc:
        tracks_skipped.append(PipelineSkip(track=track, reason=str(exc)))
        logger.info("ai_orchestration.pipeline_skipped", track=track, reason=str(exc))
        return []
    except Exception as exc:
        tracks_skipped.append(PipelineSkip(track=track, reason=f"unexpected error: {exc}"))
        logger.warning("ai_orchestration.pipeline_failed", track=track, error=str(exc))
        return []
    tracks_run.append(track)
    return clusters


def _split_near_dup_and_version_chains(clusters: list[AICluster]) -> list[AICluster]:
    """Re-presents any NEAR_DUP_DOCUMENT cluster whose members show a recognizable filename-
    version pattern as a VERSION_CHAIN over the SAME member set (ADR-0025 decision 4) -- reusing
    `version_chain.build_version_chain_cluster`'s real ordering and `version_signals_agree`
    safety gate rather than an independently-tuned second clustering pass. Clusters with no
    filename-version signal in any member pass through unchanged."""
    result: list[AICluster] = []
    for index, cluster in enumerate(clusters):
        member_paths = [member.path for member in cluster.members]
        if any(filename_version_rank(path) is not None for path in member_paths):
            result.append(
                build_version_chain_cluster(
                    f"version-chain-{index}",
                    member_paths,
                    min_content_similarity=cluster.raw_score,
                )
            )
        else:
            result.append(cluster)
    return result


def _representative_member(cluster: AICluster) -> AIClusterMember:
    for member in cluster.members:
        if member.is_recommended_keep:
            return member
    return cluster.members[0]


def _feature_vector_for_ranking(
    cluster: AICluster, member: AIClusterMember, *, git_cache: GitRepoCache
) -> FeatureVector:
    """Builds a `FeatureVector` the same way `feedback_store.record_feedback_decision` does,
    except `sibling_decision_context` is always zero -- no accept/reject/keep history exists for
    AI-suggestion decisions in this dashboard yet (ADR-0025 decision 4's disclosed limitation)."""
    record = build_record_for_path(member.path, git_cache)
    is_cloud_placeholder = record.is_cloud_placeholder if record is not None else False
    git_repo_root = record.git_repo_root if record is not None else None
    mtime = record.mtime if record is not None else 0.0
    ctime = record.ctime if record is not None else 0.0
    return FeatureVector(
        size_bytes=member.size_bytes,
        ext=member.path.suffix.lower(),
        path_class=classify_path_class(
            member.path, is_cloud_placeholder=is_cloud_placeholder, git_repo_root=git_repo_root
        ),
        mtime=mtime,
        ctime=ctime,
        cluster_stats=ClusterStats(
            cluster_size=len(cluster.members),
            position_in_cluster=member.position,
            raw_score=cluster.raw_score,
            score_kind=cluster.score_kind,
            is_recommended_keep=member.is_recommended_keep,
        ),
        category=cluster.track.value,
        cloud_sync_flag=is_cloud_placeholder,
        sibling_decision_context=SiblingDecisionContext(
            prior_accepted=0, prior_rejected=0, prior_kept=0
        ),
    )


def _fallback_order(clusters: list[AICluster]) -> list[AICluster]:
    """No trained clutter-ranker model available -- a stable, disclosed default order: deletion
    suggestions before browse-only, largest total cluster size first within each group."""

    def sort_key(item: tuple[int, AICluster]) -> tuple[bool, int, int]:
        index, cluster = item
        total_bytes = sum(member.size_bytes for member in cluster.members)
        return (not cluster.suggests_deletion, -total_bytes, index)

    ordered = sorted(enumerate(clusters), key=sort_key)
    return [cluster for _, cluster in ordered]


def _apply_clutter_ranker_ordering(
    result: AIAnalysisResult, *, clutter_ranker_model_path: Path
) -> None:
    if not result.clusters:
        return

    try:
        ranker = ClutterRanker(model_path=clutter_ranker_model_path)
    except (AIExtraNotInstalledError, FileNotFoundError) as exc:
        result.tracks_skipped.append(PipelineSkip(track="ranked_clutter_ordering", reason=str(exc)))
        result.clusters = _fallback_order(result.clusters)
        return
    except Exception as exc:
        result.tracks_skipped.append(
            PipelineSkip(track="ranked_clutter_ordering", reason=f"unexpected error: {exc}")
        )
        result.clusters = _fallback_order(result.clusters)
        return

    now = time.time()
    git_cache = GitRepoCache()
    scored: list[tuple[float, int, AICluster]] = []
    for original_index, cluster in enumerate(result.clusters):
        member = _representative_member(cluster)
        try:
            feature_vector = _feature_vector_for_ranking(cluster, member, git_cache=git_cache)
            score = ranker.score(feature_vector, now=now).raw_score
        except Exception as exc:
            # the whole ordering pass; it just sorts last within its tier.
            logger.warning(
                "ai_orchestration.clutter_score_failed",
                cluster_id=cluster.cluster_id,
                error=str(exc),
            )
            score = float("-inf")
        scored.append((score, original_index, cluster))

    scored.sort(key=lambda item: (-item[0], item[1]))
    result.clusters = [cluster for _, _, cluster in scored]
    result.tracks_run.append("ranked_clutter_ordering")


def run_ai_analysis(
    *,
    records: Sequence[FileRecord],
    safety: SafetyValidator,
    max_hamming_distance: int = 14,
    minhash_threshold: float = 0.1,
    embedding_threshold: float = 0.95,
    clutter_ranker_model_path: Path = DEFAULT_MODEL_PATH,
) -> AIAnalysisResult:
    """One full AI analysis pass over `records` (ADR-0025). Every pipeline's operating point is
    the already-measured value from its own ADR (image_similarity.py/document_similarity.py's
    docstrings), passed through explicitly here, never silently re-derived."""
    result = AIAnalysisResult()
    classified = classify_scan_files(records)
    result.files_considered = {
        "images": len(classified.image_paths),
        "documents": len(classified.document_paths),
        "screenshot_candidates": len(classified.screenshot_candidate_paths),
    }
    result.files_capped = {
        "images_over_count_cap": classified.images_capped,
        "documents_over_count_cap": classified.documents_capped,
        "images_over_size_cap": classified.images_skipped_too_large,
        "documents_over_size_cap": classified.documents_skipped_too_large,
    }

    near_identical = _run_pipeline(
        AITrack.NEAR_IDENTICAL_IMAGE.value,
        result.tracks_run,
        result.tracks_skipped,
        lambda: build_near_identical_clusters(
            classified.image_paths, safety=safety, max_hamming_distance=max_hamming_distance
        ),
    )
    result.clusters.extend(near_identical)

    track_a_member_paths = {member.path for cluster in near_identical for member in cluster.members}
    residual_images = tuple(
        path for path in classified.image_paths if path not in track_a_member_paths
    )[:MAX_RESIDUAL_IMAGES_FOR_SEMANTIC]
    semantic = _run_pipeline(
        AITrack.SEMANTIC_IMAGE.value,
        result.tracks_run,
        result.tracks_skipped,
        lambda: build_semantic_image_clusters(residual_images, safety=safety),
    )
    result.clusters.extend(semantic)

    documents = _run_pipeline(
        "near_dup_document_and_version_chain",
        result.tracks_run,
        result.tracks_skipped,
        lambda: build_near_dup_document_clusters(
            classified.document_paths,
            safety=safety,
            minhash_threshold=minhash_threshold,
            embedding_threshold=embedding_threshold,
        ),
    )
    result.clusters.extend(_split_near_dup_and_version_chains(documents))

    screenshots = _run_pipeline(
        AITrack.SCREENSHOT_BURST.value,
        result.tracks_run,
        result.tracks_skipped,
        lambda: build_screenshot_burst_clusters(
            classified.screenshot_candidate_paths, safety=safety
        ),
    )
    result.clusters.extend(screenshots)

    _apply_clutter_ranker_ordering(result, clutter_ranker_model_path=clutter_ranker_model_path)
    return result
