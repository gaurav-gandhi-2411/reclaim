from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL")

from ai_fixtures.build_image_similarity_fixtures import build_image_similarity_fixtures

from reclaim.ai.eval_harness import (
    DistributionDeclaration,
    EvalReport,
    bcubed_precision_recall,
    current_commit_sha,
    precision_recall_curve,
    select_operating_point,
)
from reclaim.ai.image_similarity import build_near_identical_clusters
from reclaim.ai.keep_best import score_image_quality
from reclaim.ai.phash import cluster_by_hamming_distance, compute_image_hashes, hamming_distance
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator

# Feature 1a Track A eval (spec §7): BCubed precision/recall for clustering (§7.2), top-1 +
# "never picks the worst" for keep-best (§7.2), the target-precision operating-point
# selection (§7.3) — all against SYNTHETIC CI fixtures only. Per the explicit autonomy
# boundary in the build brief: every threshold selected here is PROVISIONAL and labeled as
# such (see ADR-0012) — GG's real gold-set labels are required before any of this is a final
# operating point.

_TARGET_PRECISION = 0.95  # spec §7.3: near-identical/deletion-suggestion tracks >= 0.95
_COMMAND = "uv run pytest evals/test_ai_image_similarity.py -v"


def _all_pairwise_hamming_scored(
    records: list, hash_kind: str = "phash"
) -> list[tuple[float, bool]]:
    """One (negated Hamming distance, is_same_true_cluster) pair per unordered pair of
    fixture images — negated so `precision_recall_curve`'s "higher score = more similar"
    convention holds without needing a separate distance-direction code path in the harness
    for this call site."""
    scored: list[tuple[float, bool]] = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            case_i, record_i = records[i]
            case_j, record_j = records[j]
            hash_i = record_i.phash_hex if hash_kind == "phash" else record_i.dhash_hex
            hash_j = record_j.phash_hex if hash_kind == "phash" else record_j.dhash_hex
            distance = hamming_distance(hash_i, hash_j)
            is_same_cluster = case_i.true_cluster_id == case_j.true_cluster_id
            scored.append((float(-distance), is_same_cluster))
    return scored


def test_phash_operating_point_meets_target_precision_on_synthetic_fixtures(
    tmp_path: Path,
) -> None:
    """Derives the near-identical Hamming-distance operating point from a real PR curve over
    the fixtures (spec §7.3's selection METHOD — the data source is synthetic, so the result
    is provisional, but the selection process itself is the real one, not hand-set)."""
    cases = build_image_similarity_fixtures(tmp_path, n_clusters=6, n_distractors=8)
    records = [(case, compute_image_hashes(tmp_path / case.relative_path)) for case in cases]
    assert all(record is not None for _, record in records), "every synthetic fixture must decode"

    scored = _all_pairwise_hamming_scored(records)
    curve = precision_recall_curve(scored, higher_score_is_more_similar=True)
    operating_point = select_operating_point(
        curve,
        target_precision=_TARGET_PRECISION,
        min_recall=0.0,  # this test proves the selection MACHINERY works, not a shipped gate
        distribution=DistributionDeclaration(
            description=(
                "synthetic CI fixtures (evals/ai_fixtures/build_image_similarity_fixtures.py)"
            ),
            is_realistic=False,
            is_adversarial_tail_only=False,
            is_synthetic_only=True,
            untested_variation_note=(
                "entirely synthetic — no real-world variation covered at all; see "
                "evals/test_ai_copydays_realistic_distribution.py for the real measurement"
            ),
        ),
        source_description=(
            "synthetic CI fixtures (evals/ai_fixtures/build_image_similarity_fixtures.py) — "
            "PROVISIONAL, not GG's gold set"
        ),
    )

    assert operating_point is not None, (
        f"no threshold on the synthetic-fixture PR curve reaches the target precision "
        f"{_TARGET_PRECISION} — the fixtures or the hash pipeline need investigation before "
        "this feature can ship any operating point at all"
    )
    assert operating_point.is_provisional is True
    assert operating_point.precision >= _TARGET_PRECISION

    report = EvalReport(
        metric_name="phash_operating_point_max_hamming_distance",
        value=-operating_point.threshold,  # undo the negation used for the PR-curve convention
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path="evals/ai_fixtures/build_image_similarity_fixtures.py",
    )
    print(report)  # noqa: T201 -- eval reporting output, not application logging


def test_clustering_bcubed_precision_recall_meets_floor_at_measured_threshold(
    tmp_path: Path,
) -> None:
    """CI precision-floor gate (spec §7.4): asserts BCubed precision/recall at a fixed,
    now-MEASURED threshold (hardcoded here for a fast, deterministic CI gate against synthetic
    fixtures — re-deriving the real PR curve on every test run isn't possible in CI anyway,
    since it needs the real Copydays dataset; see evals/test_ai_copydays_gold.py, run
    separately, locally, on demand)."""
    max_hamming_distance = 14  # MEASURED on INRIA Copydays — see ADR-0012/ADR-0015
    cases = build_image_similarity_fixtures(tmp_path, n_clusters=6, n_distractors=8)

    true_clusters = {case.id: case.true_cluster_id for case in cases}
    hash_records = []
    case_by_path: dict[Path, object] = {}
    for case in cases:
        path = tmp_path / case.relative_path
        record = compute_image_hashes(path)
        assert record is not None
        hash_records.append(record)
        case_by_path[path] = case

    predicted_groups = cluster_by_hamming_distance(hash_records, max_distance=max_hamming_distance)
    predicted_clusters: dict[str, str] = {}
    for group_index, group in enumerate(predicted_groups):
        for record in group:
            predicted_clusters[case_by_path[record.path].id] = f"predicted_{group_index}"
    # Every fixture case not swept into a multi-member predicted cluster is its own singleton
    # predicted cluster (BCubed needs every item assigned somewhere).
    for case in cases:
        predicted_clusters.setdefault(case.id, f"singleton_{case.id}")

    result = bcubed_precision_recall(predicted_clusters, true_clusters)

    report = EvalReport(
        metric_name="near_identical_clustering_bcubed_precision",
        value=result.precision,
        commit_sha=current_commit_sha(),
        command=_COMMAND,
        fixture_path="evals/ai_fixtures/build_image_similarity_fixtures.py",
    )
    print(report)  # noqa: T201
    print(  # noqa: T201
        EvalReport(
            metric_name="near_identical_clustering_bcubed_recall",
            value=result.recall,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path="evals/ai_fixtures/build_image_similarity_fixtures.py",
        )
    )

    # CI regression gate — build fails if a future change drops below this floor.
    assert result.precision >= 0.95
    assert result.recall >= 0.8


def test_keep_best_never_selects_the_worst_quality_member(tmp_path: Path) -> None:
    """The safety metric spec §7.2 says matters MORE than raw top-1 agreement: across every
    fixture cluster, the scorer must never select the deliberately-worst (blurred,
    downscaled, heavily-compressed) member as the recommended keeper."""
    cases = build_image_similarity_fixtures(tmp_path, n_clusters=6, n_distractors=8)
    by_cluster: dict[str, list] = {}
    for case in cases:
        if case.true_cluster_id.startswith("distractor"):
            continue
        by_cluster.setdefault(case.true_cluster_id, []).append(case)

    worst_selected_count = 0
    for cluster_cases in by_cluster.values():
        scores = [score_image_quality(tmp_path / c.relative_path) for c in cluster_cases]
        assert all(s is not None for s in scores)
        keeper = max(scores, key=lambda s: s.combined)
        keeper_case = next(c for c in cluster_cases if tmp_path / c.relative_path == keeper.path)
        if keeper_case.id.endswith("_worst"):
            worst_selected_count += 1

    assert worst_selected_count == 0, (
        f"{worst_selected_count} cluster(s) selected the deliberately-worst-quality member as "
        "keeper — this is the safety metric the spec weighs above raw top-1 agreement"
    )


def test_keep_best_top1_agreement_with_the_designated_best_member(tmp_path: Path) -> None:
    """Raw top-1 agreement (spec §7.2) — matters, but less than the safety metric above."""
    cases = build_image_similarity_fixtures(tmp_path, n_clusters=6, n_distractors=8)
    by_cluster: dict[str, list] = {}
    for case in cases:
        if case.true_cluster_id.startswith("distractor"):
            continue
        by_cluster.setdefault(case.true_cluster_id, []).append(case)

    correct = 0
    for cluster_cases in by_cluster.values():
        scores = [score_image_quality(tmp_path / c.relative_path) for c in cluster_cases]
        keeper = max(scores, key=lambda s: s.combined)
        keeper_case = next(c for c in cluster_cases if tmp_path / c.relative_path == keeper.path)
        if keeper_case.is_best_quality:
            correct += 1

    top1_agreement = correct / len(by_cluster)
    print(  # noqa: T201
        EvalReport(
            metric_name="keep_best_top1_agreement",
            value=top1_agreement,
            commit_sha=current_commit_sha(),
            command=_COMMAND,
            fixture_path="evals/ai_fixtures/build_image_similarity_fixtures.py",
        )
    )
    assert top1_agreement >= 0.8  # CI regression floor


def test_end_to_end_build_near_identical_clusters_respects_safety_and_produces_keepers(
    tmp_path: Path,
) -> None:
    """The full orchestration function (image_similarity.build_near_identical_clusters), not
    just its component parts — proves safety filtering, hashing, clustering, and keep-best
    scoring compose correctly end to end, and that a protected-root image never appears in
    the output at all."""
    cases = build_image_similarity_fixtures(tmp_path, n_clusters=3, n_distractors=2)
    all_paths = [tmp_path / case.relative_path for case in cases]

    protected_dir = tmp_path / "Windows"
    protected_dir.mkdir()
    protected_copy = protected_dir / "system_wallpaper.jpg"
    protected_copy.write_bytes((tmp_path / cases[0].relative_path).read_bytes())
    all_paths.append(protected_copy)

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected_dir.as_posix()}/*"]))
    )

    clusters = build_near_identical_clusters(all_paths, safety=safety, max_hamming_distance=14)

    assert len(clusters) >= 1
    for cluster in clusters:
        for member in cluster.members:
            assert protected_dir not in member.path.parents
        assert sum(1 for m in cluster.members if m.is_recommended_keep) == 1
        assert cluster.suggests_deletion is True
