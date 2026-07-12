from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fixtures.build_executor_tree import build_executor_fixture_tree

from reclaim.config import CategoriesConfig, Config, LargeLogsConfig, SafetyConfig
from reclaim.dedup import generate_duplicate_candidates
from reclaim.detectors import generate_candidates
from reclaim.executor import apply_batch, restore_batch
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


def _config(root: Path) -> Config:
    """Fixture-relative protected roots — same pattern as test_candidate_generation.py's
    `_config` — so real C:\\Windows is never touched during development/CI. Threshold override
    on large_logs keeps the fixture fast (real 50MB writes aren't needed to exercise the
    detector -> SafetyValidator -> executor pipeline)."""
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=True,
            large_logs=LargeLogsConfig(enabled=True, min_size_bytes=1_000, stale_days=30),
        ),
    )


def _by_path(candidates: list[Candidate]) -> dict[Path, Candidate]:
    return {c.path: c for c in candidates}


def test_executor_end_to_end_real_scan_apply_restore(tmp_path: Path) -> None:
    """Stage 5 CI gate: a real scan -> candidate generation -> `apply_batch` (vault method) ->
    `restore_batch` pipeline against a materialized fixture tree. Every claim is independently
    re-verified against the real filesystem (bytes read off disk, not trusted from any
    pipeline-internal value), per spec's "no fabricated confidence" principle applied to
    recoverability.
    """
    now = time.time()
    tree_root = tmp_path / "tree"
    tree = build_executor_fixture_tree(tree_root, now=now)

    # Ground truth captured BEFORE anything is quarantined.
    original_node_modules_a = tree.node_modules_file_a.read_bytes()
    original_node_modules_b = tree.node_modules_file_b.read_bytes()
    original_old_log = tree.old_log.read_bytes()
    original_kept_file = tree.kept_file.read_bytes()
    expected_node_modules_bytes = len(original_node_modules_a) + len(original_node_modules_b)
    expected_old_log_bytes = len(original_old_log)

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(tree.root, index, incremental=False)

        config = _config(tree.root)
        safety = SafetyValidator(config)
        candidates = generate_candidates(index, config, safety, now=now)
        candidates += generate_duplicate_candidates(index, config, safety)

    by_path = _by_path(candidates)
    assert tree.node_modules_dir in by_path
    assert by_path[tree.node_modules_dir].tier == Tier.A
    assert by_path[tree.node_modules_dir].size_bytes == expected_node_modules_bytes
    assert tree.old_log in by_path
    assert by_path[tree.old_log].tier == Tier.A
    assert by_path[tree.old_log].size_bytes == expected_old_log_bytes
    assert tree.kept_file not in by_path  # negative control: never a candidate at all

    tier_a_candidates = [c for c in candidates if c.tier == Tier.A]
    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"

    apply_report = apply_batch(
        tier_a_candidates,
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=now,
    )

    assert apply_report.files_failed == 0
    assert apply_report.files_succeeded == len(tier_a_candidates)

    # --- Genuinely gone from their original locations — not just "the report says so".
    assert not tree.node_modules_dir.exists()
    assert not tree.old_log.exists()
    assert tree.kept_file.exists()
    assert tree.kept_file.read_bytes() == original_kept_file

    # --- The report's bytes-freed number matches real measured sizes, independently
    # recomputed from the ground-truth content captured before anything moved.
    assert apply_report.bytes_freed == expected_node_modules_bytes + expected_old_log_bytes

    restore_report = restore_batch(apply_report.batch_id, manifest_path=manifest_path, now=now)
    assert restore_report.files_failed == 0
    assert restore_report.files_succeeded == len(tier_a_candidates)
    assert restore_report.bytes_restored == apply_report.bytes_freed

    # --- Every file restored, byte-identical, read straight from disk (ground truth, not the
    # manifest's own claim of success).
    assert tree.node_modules_file_a.read_bytes() == original_node_modules_a
    assert tree.node_modules_file_b.read_bytes() == original_node_modules_b
    assert tree.old_log.read_bytes() == original_old_log
