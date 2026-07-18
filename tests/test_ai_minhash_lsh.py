from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.ai.minhash_lsh import (
    cluster_by_jaccard_similarity,
    compute_document_minhash,
    jaccard_similarity,
)

_REPEATED_SENTENCE = "The quarterly report shows steady revenue growth across all regions. " * 8


def test_compute_document_minhash_returns_none_for_empty_text(tmp_path: Path) -> None:
    assert compute_document_minhash(tmp_path / "empty.txt", "") is None
    assert compute_document_minhash(tmp_path / "whitespace.txt", "   \n\t  ") is None


def test_jaccard_similarity_identical_text_is_one(tmp_path: Path) -> None:
    record_a = compute_document_minhash(tmp_path / "a.txt", _REPEATED_SENTENCE)
    record_b = compute_document_minhash(tmp_path / "b.txt", _REPEATED_SENTENCE)
    assert record_a is not None
    assert record_b is not None
    assert jaccard_similarity(record_a, record_b) == pytest.approx(1.0)


def test_jaccard_similarity_completely_different_text_is_near_zero(tmp_path: Path) -> None:
    text_a = "The quarterly financial report discusses revenue and profit margins in detail."
    text_b = "A recipe for chocolate chip cookies requires flour sugar butter and eggs."
    record_a = compute_document_minhash(tmp_path / "a.txt", text_a)
    record_b = compute_document_minhash(tmp_path / "b.txt", text_b)
    assert record_a is not None
    assert record_b is not None
    assert jaccard_similarity(record_a, record_b) == pytest.approx(0.0, abs=0.05)


def test_jaccard_similarity_rejects_mismatched_num_perm(tmp_path: Path) -> None:
    record_a = compute_document_minhash(tmp_path / "a.txt", _REPEATED_SENTENCE, num_perm=64)
    record_b = compute_document_minhash(tmp_path / "b.txt", _REPEATED_SENTENCE, num_perm=128)
    assert record_a is not None
    assert record_b is not None
    with pytest.raises(ValueError, match="different num_perm"):
        jaccard_similarity(record_a, record_b)


def test_cluster_by_jaccard_similarity_groups_near_identical_and_drops_singletons(
    tmp_path: Path,
) -> None:
    near_dup_a = compute_document_minhash(tmp_path / "a.txt", _REPEATED_SENTENCE)
    near_dup_b = compute_document_minhash(
        tmp_path / "b.txt", _REPEATED_SENTENCE + "One more sentence appended at the end."
    )
    distinct = compute_document_minhash(
        tmp_path / "c.txt",
        "An entirely unrelated document about astronomy and distant galaxies far away.",
    )
    assert near_dup_a is not None
    assert near_dup_b is not None
    assert distinct is not None

    groups = cluster_by_jaccard_similarity([near_dup_a, near_dup_b, distinct], min_similarity=0.5)

    assert len(groups) == 1
    grouped_paths = {record.path for record in groups[0]}
    assert grouped_paths == {near_dup_a.path, near_dup_b.path}
