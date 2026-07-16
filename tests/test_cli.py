from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.cli import main


def test_apply_dry_run_skips_duplicates_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression test for the real-disk-run stall: `apply` (dry-run) must be usable without
    ever triggering the size/hash-based duplicate pipeline — that pass is what had zero output
    for as long as anyone watched a 3.1M-file run. Default (no --include-duplicates) must
    report fast and never mention the duplicate category."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 200)
    (root / "b.bin").write_bytes(b"x" * 200)  # exact duplicate of a.bin
    db = tmp_path / "index.sqlite3"
    missing_config = tmp_path / "config.toml"

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(["apply", str(root), "--db", str(db), "--config", str(missing_config)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "duplicate detection skipped" in out
    assert "exact_duplicate" not in out


def test_apply_dry_run_include_duplicates_runs_dedup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--include-duplicates` opts back into the hash-based pipeline; the byte-identical pair
    must then surface as an `exact_duplicate` candidate in the printed report."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 200)
    (root / "b.bin").write_bytes(b"x" * 200)
    db = tmp_path / "index.sqlite3"
    missing_config = tmp_path / "config.toml"

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "apply",
            str(root),
            "--db",
            str(db),
            "--config",
            str(missing_config),
            "--include-duplicates",
            "--tier",
            "both",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "duplicate detection skipped" not in out
    assert "exact_duplicate" in out
