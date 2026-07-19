from __future__ import annotations

from pathlib import Path

from reclaim.ai.document_similarity import build_near_dup_document_clusters
from reclaim.config import Config, SafetyConfig
from reclaim.safety import SafetyValidator

_BASE_TEXT = (
    "The quarterly financial report shows steady revenue growth across all regions "
    "during this period, driven primarily by strong performance in the cloud services "
    "division and continued expansion into emerging markets worldwide. "
) * 3


def test_end_to_end_finds_near_dup_documents_and_recommends_a_keeper(tmp_path: Path) -> None:
    near_dup_a = tmp_path / "report_v1.txt"
    near_dup_b = tmp_path / "report_v2.txt"
    unrelated = tmp_path / "recipe.txt"

    near_dup_a.write_text(_BASE_TEXT, encoding="utf-8")
    near_dup_b.write_text(
        _BASE_TEXT + "One additional closing sentence was appended.", encoding="utf-8"
    )
    unrelated.write_text(
        "A recipe for chocolate chip cookies requires flour, sugar, butter, and eggs, "
        "mixed together and baked at moderate heat until golden brown on the edges.",
        encoding="utf-8",
    )

    safety = SafetyValidator(Config())
    clusters = build_near_dup_document_clusters(
        [near_dup_a, near_dup_b, unrelated],
        safety=safety,
        minhash_threshold=0.3,
        embedding_threshold=0.8,
    )

    assert len(clusters) == 1
    cluster = clusters[0]
    member_paths = {member.path for member in cluster.members}
    assert member_paths == {near_dup_a, near_dup_b}
    assert unrelated not in member_paths
    assert sum(1 for m in cluster.members if m.is_recommended_keep) == 1
    assert cluster.suggests_deletion is True


def test_end_to_end_respects_safety_validator_protected_roots(tmp_path: Path) -> None:
    near_dup_a = tmp_path / "report_v1.txt"
    protected_dir = tmp_path / "Windows"
    protected_copy = protected_dir / "report_v2.txt"
    protected_dir.mkdir()

    near_dup_a.write_text(_BASE_TEXT, encoding="utf-8")
    protected_copy.write_text(_BASE_TEXT, encoding="utf-8")

    safety = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=[f"{protected_dir.as_posix()}/*"]))
    )
    clusters = build_near_dup_document_clusters(
        [near_dup_a, protected_copy],
        safety=safety,
        minhash_threshold=0.3,
        embedding_threshold=0.8,
    )

    all_paths = {member.path for cluster in clusters for member in cluster.members}
    assert protected_copy not in all_paths


def test_end_to_end_produces_no_clusters_for_all_distinct_documents(tmp_path: Path) -> None:
    doc_a = tmp_path / "a.txt"
    doc_b = tmp_path / "b.txt"
    doc_a.write_text("A history of ancient Roman architecture and engineering techniques.")
    doc_b.write_text("An introduction to modern jazz music theory and improvisation.")

    safety = SafetyValidator(Config())
    clusters = build_near_dup_document_clusters(
        [doc_a, doc_b], safety=safety, minhash_threshold=0.3, embedding_threshold=0.8
    )
    assert clusters == []
