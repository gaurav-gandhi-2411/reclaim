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
    must then surface as an `exact_duplicate` candidate in the printed report.

    Files are 2MB (not a tiny size) so the pair clears the default materiality gate
    (`config.categories.duplicates.min_reclaim_bytes`, 1MB) — a duplicate pair below that
    floor is deliberately never hashed at all (see `test_index.py`'s materiality tests)."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    (root / "b.bin").write_bytes(b"x" * 2 * 1024 * 1024)
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


def test_apply_report_shows_materiality_exclusion_alongside_real_duplicate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tiny duplicate pair (below the default 1MB materiality floor) must be reported as
    excluded rather than silently dropped, while a real 2MB duplicate pair in the same tree is
    still detected and reported normally."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "tiny_a.bin").write_bytes(b"t" * 100)
    (root / "tiny_b.bin").write_bytes(b"t" * 100)
    (root / "large_a.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    (root / "large_b.bin").write_bytes(b"x" * 2 * 1024 * 1024)
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
    assert "exact_duplicate" in out  # the real 2MB pair still surfaces
    assert "1 size bucket(s) excluded as immaterial" in out
    assert "theoretical best-case size 100 bytes" in out


def test_apply_include_categories_restricts_to_named_categories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--include-categories` narrows an already tier/root-filtered selection to just the named
    fine-grained categories — the staged-rollout mechanism for applying a reviewed subset of one
    enabled group (here: dev_artifacts) while deferring the rest of that same group to a later
    run. `dev_artifacts.enabled=True` makes BOTH node_modules and pycache Tier A candidates;
    `--include-categories dev_artifact_pycache` must apply only the pycache one."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "package.json").write_bytes(b"{}")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.js").write_bytes(b"x" * 100)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "mod.pyc").write_bytes(b"y" * 100)
    db = tmp_path / "index.sqlite3"
    config_path = tmp_path / "config.toml"
    config_path.write_text("[categories.dev_artifacts]\nenabled = true\n", encoding="utf-8")

    assert main(["scan", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "apply",
            str(root),
            "--db",
            str(db),
            "--config",
            str(config_path),
            "--include-categories",
            "dev_artifact_pycache",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "restricted selection to ['dev_artifact_pycache']" in out
    assert "1/2 tier/root-eligible candidate(s) kept" in out
    assert "dev_artifact_pycache: count=1" in out
    assert "dev_artifact_node_modules" not in out
