from __future__ import annotations

import os
from pathlib import Path

import pytest

from reclaim.ai.document_keep_best import select_document_keep


def test_select_document_keep_prefers_larger_file(tmp_path: Path) -> None:
    small = tmp_path / "small.txt"
    large = tmp_path / "large.txt"
    small.write_text("x" * 10)
    large.write_text("x" * 1000)
    assert select_document_keep([small, large]) == large


def test_select_document_keep_breaks_size_tie_with_newer_mtime(tmp_path: Path) -> None:
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("x" * 100)
    newer.write_text("x" * 100)
    now = 1_700_000_000.0
    os.utime(older, (now, now))
    os.utime(newer, (now + 100, now + 100))
    assert select_document_keep([older, newer]) == newer


def test_select_document_keep_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="empty"):
        select_document_keep([])
