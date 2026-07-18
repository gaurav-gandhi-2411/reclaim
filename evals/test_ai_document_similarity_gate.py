from __future__ import annotations

from pathlib import Path

from ai_fixtures.build_document_similarity_fixtures import build_document_similarity_fixtures

from reclaim.ai.document_keep_best import select_document_keep
from reclaim.ai.document_similarity import build_near_dup_document_clusters
from reclaim.ai.document_text import extract_text
from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    bcubed_precision_recall,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)
from reclaim.ai.minhash_lsh import (
    cluster_by_jaccard_similarity,
    compute_document_minhash,
    jaccard_similarity,
)
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator

# Feature 1b's CI eval gate (spec §7): BCubed precision/recall for clustering, keep-best
# top-1/safety, and MinHash-Jaccard PR-curve-derived operating-point selection — all against
# SYNTHETIC CI fixtures only. Same ADR-0016-hardened posture as Feature 1a: every threshold
# selected here is PROVISIONAL (synthetic, is_synthetic_only=True, min_recall=0.0 — this suite
# proves the selection MACHINERY works, not a shipped gate). See ADR-0017 for the MEASURED
# operating point derived from real data.

_TARGET_PRECISION = 0.95  # spec §7.3: near-identical/deletion-suggestion tracks >= 0.95
_COMMAND = "uv run pytest evals/test_ai_document_similarity_gate.py -v"
_FIXTURE = "evals/ai_fixtures/build_document_similarity_fixtures.py"
_SYNTHETIC_DISTRIBUTION = DistributionDeclaration(
    description=_FIXTURE,
    is_realistic=False,
    is_adversarial_tail_only=False,
    is_synthetic_only=True,
    untested_variation_note="entirely synthetic — see ADR-0017 for the real measurement",
)


def _all_pairwise_jaccard_scored(records: list) -> list[tuple[float, bool]]:
    """One (jaccard_similarity, is_same_true_cluster) pair per unordered pair — similarity is
    already a "higher = more similar" score, no negation needed (unlike phash's Hamming
    distance)."""
    scored: list[tuple[float, bool]] = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            case_i, record_i = records[i]
            case_j, record_j = records[j]
            similarity = jaccard_similarity(record_i, record_j)
            is_same_cluster = case_i.true_cluster_id == case_j.true_cluster_id
            scored.append((similarity, is_same_cluster))
    return scored


def test_minhash_operating_point_meets_target_precision_on_synthetic_fixtures(
    tmp_path: Path,
) -> None:
    cases = build_document_similarity_fixtures(tmp_path)
    records = []
    for case in cases:
        text = extract_text(tmp_path / case.relative_path)
        assert text is not None
        record = compute_document_minhash(tmp_path / case.relative_path, text)
        assert record is not None, "every synthetic fixture must shingle to something"
        records.append((case, record))

    scored = _all_pairwise_jaccard_scored(records)
    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)
    operating_point = select_operating_point(
        curve,
        target_precision=_TARGET_PRECISION,
        min_recall=0.0,
        distribution=_SYNTHETIC_DISTRIBUTION,
        source_description=f"{_FIXTURE} — PROVISIONAL, not a real gold set",
    )

    assert operating_point is not None, (
        f"no threshold on the synthetic-fixture PR curve reaches target precision "
        f"{_TARGET_PRECISION} — the fixtures or the MinHash pipeline need investigation"
    )
    assert operating_point.is_provisional is True
    assert operating_point.precision >= _TARGET_PRECISION

    report = EvalReport(
        metric_name="minhash_operating_point_jaccard_threshold",
        value=operating_point.threshold,
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path=_FIXTURE,
    )
    print(report)  # noqa: T201


def test_clustering_bcubed_precision_recall_meets_floor_at_provisional_threshold(
    tmp_path: Path,
) -> None:
    min_similarity = 0.3  # provisional; see ADR-0017 for the measured value
    cases = build_document_similarity_fixtures(tmp_path)

    true_clusters = {case.id: case.true_cluster_id for case in cases}
    minhash_records = []
    case_by_path: dict[Path, object] = {}
    for case in cases:
        path = tmp_path / case.relative_path
        text = extract_text(path)
        assert text is not None
        record = compute_document_minhash(path, text)
        assert record is not None
        minhash_records.append(record)
        case_by_path[path] = case

    predicted_groups = cluster_by_jaccard_similarity(minhash_records, min_similarity=min_similarity)
    predicted_clusters: dict[str, str] = {}
    for group_index, group in enumerate(predicted_groups):
        for record in group:
            predicted_clusters[case_by_path[record.path].id] = f"predicted_{group_index}"
    for case in cases:
        predicted_clusters.setdefault(case.id, f"singleton_{case.id}")

    result = bcubed_precision_recall(predicted_clusters, true_clusters)

    print(  # noqa: T201
        EvalReport(
            metric_name="near_dup_document_clustering_bcubed_precision",
            value=result.precision,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    print(  # noqa: T201
        EvalReport(
            metric_name="near_dup_document_clustering_bcubed_recall",
            value=result.recall,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    assert result.precision >= 0.9
    assert result.recall >= 0.8


def test_keep_best_never_selects_a_non_largest_member(tmp_path: Path) -> None:
    cases = build_document_similarity_fixtures(tmp_path)
    by_cluster: dict[str, list] = {}
    for case in cases:
        if case.true_cluster_id.startswith("distractor"):
            continue
        by_cluster.setdefault(case.true_cluster_id, []).append(case)

    correct = 0
    for cluster_cases in by_cluster.values():
        paths = [tmp_path / c.relative_path for c in cluster_cases]
        keeper = select_document_keep(paths)
        keeper_case = next(c for c in cluster_cases if tmp_path / c.relative_path == keeper)
        if keeper_case.is_best_quality:
            correct += 1

    top1_agreement = correct / len(by_cluster)
    print(  # noqa: T201
        EvalReport(
            metric_name="document_keep_best_top1_agreement",
            value=top1_agreement,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path=_FIXTURE,
        )
    )
    assert top1_agreement == 1.0  # unambiguous-by-construction fixture; see the generator


def test_end_to_end_build_near_dup_document_clusters_respects_safety(tmp_path: Path) -> None:
    cases = build_document_similarity_fixtures(tmp_path)
    all_paths = [tmp_path / case.relative_path for case in cases]

    protected_dir = tmp_path / "Windows"
    protected_dir.mkdir()
    protected_copy = protected_dir / "system_config.txt"
    protected_copy.write_bytes((tmp_path / cases[0].relative_path).read_bytes())
    all_paths.append(protected_copy)

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected_dir.as_posix()}/*"]))
    )

    clusters = build_near_dup_document_clusters(
        all_paths, safety=safety, minhash_threshold=0.3, embedding_threshold=0.7
    )

    assert len(clusters) >= 1
    for cluster in clusters:
        for member in cluster.members:
            assert protected_dir not in member.path.parents
        assert sum(1 for m in cluster.members if m.is_recommended_keep) == 1
        assert cluster.suggests_deletion is True
