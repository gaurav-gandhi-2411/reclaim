from __future__ import annotations

import os
from pathlib import Path

from reclaim.ai.version_chain import (
    build_version_chain_cluster,
    filename_version_rank,
    order_version_chain,
    version_signals_agree,
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


# --- Safety property: filename-version vs. mtime disagreement must block deletion ----------
# GG's explicit instruction: version-chain is the one Feature 1b path that can delete a
# genuinely-wanted file, so a deletion suggestion may only fire when both signals agree.


def test_version_signals_agree_when_filename_and_mtime_align(tmp_path: Path) -> None:
    v1 = tmp_path / "report_v1.docx"
    v2 = tmp_path / "report_v2.docx"
    v1.write_text("x")
    v2.write_text("x")
    now = 1_700_000_000.0
    os.utime(v1, (now, now))
    os.utime(v2, (now + 60, now + 60))  # later filename rank, later mtime -- agrees
    assert version_signals_agree([v1, v2]) is True


def test_version_signals_disagree_when_final_is_older_than_a_numbered_version(
    tmp_path: Path,
) -> None:
    """The exact scenario GG named: report_final.docx (highest filename rank) modified
    BEFORE report_v2.docx (lower filename rank) -- the filename claims "final" is the latest
    version, but the file on disk was touched earlier than v2 was. A real, ambiguous case."""
    v2 = tmp_path / "report_v2.docx"
    final = tmp_path / "report_final.docx"
    v2.write_text("x")
    final.write_text("x")
    now = 1_700_000_000.0
    os.utime(final, (now, now))  # final: OLDER mtime
    os.utime(v2, (now + 100, now + 100))  # v2: NEWER mtime
    assert version_signals_agree([v2, final]) is False


def test_version_signals_agree_ignores_files_with_no_filename_pattern(tmp_path: Path) -> None:
    """A file with no recognizable filename pattern carries no filename signal to conflict
    with mtime in the first place -- it must not be treated as a disagreement just because
    its mtime happens to be "out of order" relative to a ranked file."""
    unversioned = tmp_path / "notes.docx"
    v2 = tmp_path / "report_v2.docx"
    unversioned.write_text("x")
    v2.write_text("x")
    now = 1_700_000_000.0
    os.utime(v2, (now, now))  # v2 mtime is EARLIER than the unversioned file
    os.utime(unversioned, (now + 100, now + 100))
    assert version_signals_agree([unversioned, v2]) is True


def test_version_signals_agree_treats_tied_mtimes_as_agreement(tmp_path: Path) -> None:
    v1 = tmp_path / "report_v1.docx"
    v2 = tmp_path / "report_v2.docx"
    v1.write_text("x")
    v2.write_text("x")
    now = 1_700_000_000.0
    os.utime(v1, (now, now))
    os.utime(v2, (now, now))  # identical mtime -- a tie, not a disagreement
    assert version_signals_agree([v1, v2]) is True


def test_build_version_chain_cluster_flags_for_review_when_signals_disagree(
    tmp_path: Path,
) -> None:
    v2 = tmp_path / "report_v2.docx"
    final = tmp_path / "report_final.docx"
    v2.write_text("x")
    final.write_text("x")
    now = 1_700_000_000.0
    os.utime(final, (now, now))
    os.utime(v2, (now + 100, now + 100))

    cluster = build_version_chain_cluster("chain-1", [v2, final], min_content_similarity=0.9)

    assert cluster.suggests_deletion is False
    assert all(not member.is_recommended_keep for member in cluster.members)
    assert "DISAGREE" in cluster.rationale


def test_build_version_chain_cluster_still_orders_members_when_signals_disagree(
    tmp_path: Path,
) -> None:
    """Even without a confident deletion suggestion, the chain's members still carry their
    computed order/position -- useful for a human reviewing the flagged cluster, even though
    no keeper is recommended."""
    v2 = tmp_path / "report_v2.docx"
    final = tmp_path / "report_final.docx"
    v2.write_text("x")
    final.write_text("x")
    now = 1_700_000_000.0
    os.utime(final, (now, now))
    os.utime(v2, (now + 100, now + 100))

    cluster = build_version_chain_cluster("chain-1", [final, v2], min_content_similarity=0.9)

    assert [m.path for m in cluster.members] == [v2, final]
    assert [m.position for m in cluster.members] == [0, 1]
