from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fixtures.build_detector_tree import build_detector_fixture_tree

from reclaim.config import (
    ArchivePairsConfig,
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    LargeLogsConfig,
    OldInstallersConfig,
    SafetyConfig,
)
from reclaim.detectors import generate_candidates
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Mode, Tier
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


def _config(root: Path, *, categories: CategoriesConfig) -> Config:
    """Fixture-relative protected roots — see test_safety_gate.py's `golden_tree_config` for
    the same pattern — so real C:\\Windows is never touched during development/CI.

    `mode=Mode.POWER`: this eval predates ADR-0023 (Stage 2 safe mode) and its whole point is
    the enabled-vs-disabled Tier A/B comparison below — safe mode forces every candidate to
    Tier B regardless of category-enabled state (ADR-0023 guarantee 3), which would collapse
    that comparison to nothing. Explicit, not ambient: `Config.mode` defaults to `Mode.SAFE`
    (the honest default for an unresolved log), so this eval silently broke the day that
    default changed. Same fix/reasoning as `tests/test_api.py::_make_app`'s pre-seeded
    power-mode log for the same class of pre-Stage-2 test.
    """
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(
            protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"],
        ),
        categories=categories,
        mode=Mode.POWER,
    )


def _by_path(candidates: list[Candidate]) -> dict[Path, Candidate]:
    return {c.path: c for c in candidates}


def test_candidate_generation_end_to_end(tmp_path: Path) -> None:
    """Golden-tree-style CI gate for Stage 3: exercises the real scanner -> SQLite index ->
    detector -> SafetyValidator boundary against a materialized fixture tree, run twice (all
    Stage 3 categories disabled vs. enabled) against the *same* scanned index, so every
    assertion is a direct comparison of the two runs rather than a single hardcoded snapshot.
    """
    now = time.time()
    tree = build_detector_fixture_tree(tmp_path, now=now)

    db_path = tmp_path / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(tree.root, index, incremental=False)

        disabled_config = _config(tree.root, categories=CategoriesConfig())
        disabled = _by_path(
            generate_candidates(index, disabled_config, SafetyValidator(disabled_config), now=now)
        )

        enabled_config = _config(
            tree.root,
            categories=CategoriesConfig(
                dev_artifacts=DevArtifactsConfig(enabled=True),
                archive_pairs=ArchivePairsConfig(enabled=True),
                old_installers=OldInstallersConfig(enabled=True, max_age_days=90),
                large_logs=LargeLogsConfig(enabled=True),
            ),
        )
        enabled = _by_path(
            generate_candidates(index, enabled_config, SafetyValidator(enabled_config), now=now)
        )

    # node_modules WITH adjacent package.json: proposed either way; Tier A only when the
    # dev_artifacts category is enabled, Tier B (never dropped) when it isn't.
    assert tree.node_modules_with_manifest_dir in disabled
    assert disabled[tree.node_modules_with_manifest_dir].tier == Tier.B
    assert tree.node_modules_with_manifest_dir in enabled
    assert enabled[tree.node_modules_with_manifest_dir].tier == Tier.A

    # node_modules WITHOUT adjacent manifest: never a candidate at all, in either config —
    # the absolute "no manifest adjacent = never proposed" invariant.
    assert tree.node_modules_without_manifest_dir not in disabled
    assert tree.node_modules_without_manifest_dir not in enabled

    # Archive + extracted-directory pair: only the archive is ever proposed. The extracted
    # directory and everything inside it must never appear, in either config.
    assert tree.archive_zip in enabled
    assert enabled[tree.archive_zip].tier == Tier.A
    for run in (disabled, enabled):
        assert tree.extracted_dir not in run
        assert tree.extracted_dir_file not in run

    # Old installer vs. recent installer under Downloads: only the older one is ever proposed;
    # review-queue (Tier B) unless old_installers is explicitly enabled.
    assert tree.old_installer in disabled
    assert disabled[tree.old_installer].tier == Tier.B
    assert tree.old_installer in enabled
    assert enabled[tree.old_installer].tier == Tier.A
    for run in (disabled, enabled):
        assert tree.recent_installer not in run

    # Large-and-old log vs. large-but-recent log: only the old one is ever proposed.
    assert tree.old_log in disabled
    assert disabled[tree.old_log].tier == Tier.B
    assert tree.old_log in enabled
    assert enabled[tree.old_log].tier == Tier.A
    for run in (disabled, enabled):
        assert tree.recent_log not in run

    # Dev-artifact directory sitting inside a protected root, with an otherwise-valid adjacent
    # manifest: SafetyValidator BLOCKS it, so it must never appear as a candidate at all —
    # not even with dev_artifacts enabled. Proves the safety boundary actually excludes it
    # end-to-end (scanner -> index -> detector -> SafetyValidator), not just in isolation.
    for run in (disabled, enabled):
        assert tree.blocked_node_modules_dir not in run
