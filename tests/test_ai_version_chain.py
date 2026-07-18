from __future__ import annotations

import os
from pathlib import Path

from reclaim.ai.version_chain import (
    build_version_chain_cluster,
    filename_version_rank,
    order_version_chain,
)


def test_filename_version_rank_numbered_versions() -> None:
    assert filename_version_rank(Path("report_v1.docx")) == 1.0
    assert filename_version_rank(Path("report_v2.docx")) == 2.0
    assert filename_version_rank(Path("report version 3.docx")) == 3.0


def test_filename_version_rank_windows_duplicate_suffix() -> None:
    assert filename_version_rank(Path("report (1).docx")) == 1.0
    assert filename_version_rank(Path("report (2).docx")) == 2.0


def test_filename_version_rank_copy_marker() -> None:
    assert filename_version_rank(Path("report - Copy.docx")) == 1.0


def test_filename_version_rank_final_outranks_numbered_versions() -> None:
    assert filename_version_rank(Path("report_final.docx")) > filename_version_rank(
        Path("report_v9.docx")
    )


def test_filename_version_rank_final_survives_underscore_separators() -> None:
    """Regression: `\\bfinal\\b` fails to match inside "report_final" because `\\w` (what
    `\\b` boundaries on) includes underscore -- the real-world common case, not an edge case
    (spec's own example filename is "final_v2_FINAL.docx")."""
    assert filename_version_rank(Path("report_final.docx")) is not None
    assert filename_version_rank(Path("final_v2_FINAL.docx")) is not None


def test_filename_version_rank_no_pattern_returns_none() -> None:
    assert filename_version_rank(Path("quarterly_report.docx")) is None


def test_order_version_chain_by_filename_pattern(tmp_path: Path) -> None:
    v1 = tmp_path / "report_v1.docx"
    v2 = tmp_path / "report_v2.docx"
    final = tmp_path / "report_final.docx"
    for path in (v1, v2, final):
        path.write_text("x")

    ordered = order_version_chain([final, v1, v2])  # deliberately out of order
    assert ordered == [v1, v2, final]


def test_order_version_chain_falls_back_to_mtime_when_no_pattern(tmp_path: Path) -> None:
    older = tmp_path / "draft.docx"
    newer = tmp_path / "notes.docx"
    older.write_text("x")
    newer.write_text("x")
    now = 1_700_000_000.0
    os.utime(older, (now, now))
    os.utime(newer, (now + 100, now + 100))

    ordered = order_version_chain([newer, older])
    assert ordered == [older, newer]


def test_build_version_chain_cluster_marks_latest_as_keeper_with_positions(
    tmp_path: Path,
) -> None:
    v1 = tmp_path / "report_v1.docx"
    v2 = tmp_path / "report_v2.docx"
    final = tmp_path / "report_final.docx"
    for path in (v1, v2, final):
        path.write_text("x")

    cluster = build_version_chain_cluster("chain-1", [final, v1, v2], min_content_similarity=0.9)

    assert [m.path for m in cluster.members] == [v1, v2, final]
    assert [m.position for m in cluster.members] == [0, 1, 2]
    keepers = [m for m in cluster.members if m.is_recommended_keep]
    assert len(keepers) == 1
    assert keepers[0].path == final
    assert cluster.suggests_deletion is True
