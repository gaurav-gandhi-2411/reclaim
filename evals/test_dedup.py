from __future__ import annotations

import itertools
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.build_dedup_tree import build_dedup_fixture_tree

from reclaim.config import CategoriesConfig, Config, DuplicatesConfig, SafetyConfig
from reclaim.dedup import find_duplicate_clusters, generate_duplicate_candidates
from reclaim.index import ScanIndex
from reclaim.models import Candidate, DuplicateCluster, Tier
from reclaim.safety import SafetyValidator

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dedup_root() -> Iterator[Path]:
    """A scratch tree rooted under `data/_test_scratch/`, deliberately *not* pytest's built-in
    `tmp_path` — that fixture lives under the OS's own %TEMP% directory, whose ancestry would
    make every fixture path "under Temp" and defeat the keep-heuristic's Downloads/Temp cases
    (rule 1 would spuriously tie for every member). Removed on teardown; `.gitignore`d as a
    backstop if a run is interrupted before cleanup runs.
    """
    root = _REPO_ROOT / "data" / "_test_scratch" / f"dedup_{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _config(root: Path, *, duplicates_enabled: bool) -> Config:
    """Fixture-relative protected roots — same pattern as test_candidate_generation.py's
    `_config` — so real C:\\Windows is never touched during development/CI.

    `min_reclaim_bytes=0`: this golden-fixture tree uses small test files by design (fast,
    deterministic byte content); the real 1MB materiality-gate default is tested in isolation
    in `test_index.py`, not conflated with this pipeline-correctness fixture."""
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            duplicates=DuplicatesConfig(enabled=duplicates_enabled, min_reclaim_bytes=0)
        ),
    )


def _by_path(candidates: list[Candidate]) -> dict[Path, Candidate]:
    return {c.path: c for c in candidates}


def _cluster_containing(clusters: list[DuplicateCluster], path: Path) -> DuplicateCluster:
    for cluster in clusters:
        if any(member.path == path for member in cluster.members):
            return cluster
    raise AssertionError(f"no cluster contains {path}")


def test_dedup_pipeline_end_to_end(dedup_root: Path) -> None:
    """CI gate for Stage 4: precision = 1.0 on fixtures (byte-identical check), plus a recall
    check (every real duplicate cluster is found), plus end-to-end SafetyValidator/tier-gating
    integration through `generate_duplicate_candidates`.
    """
    now = 1_700_000_000.0
    tree_root = dedup_root / "tree"
    db_path = dedup_root / "_index.sqlite3"

    with ScanIndex(db_path) as index:
        tree = build_dedup_fixture_tree(tree_root, index, now=now)

        # min_reclaim_bytes=0: see _config()'s docstring above.
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)

        # --- Ground-truth precision proof: independently re-read every cluster member's bytes
        # off disk and assert pairwise byte-equality — trusts nothing the hash-based pipeline
        # computed, per spec's "precision = 1.0 required ... (byte-identical check)".
        for cluster in clusters:
            contents = [member.path.read_bytes() for member in cluster.members]
            for content_a, content_b in itertools.combinations(contents, 2):
                assert content_a == content_b

        # --- Recall: exactly the expected clusters were found, with exactly the expected
        # membership — a bug that fails to find real duplicates must fail this too, not just a
        # bug that over-clusters.
        expected_membership = [
            {tree.dup_pair_a, tree.dup_pair_b},
            {tree.dup_triple_a, tree.dup_triple_b, tree.dup_triple_c},
            {tree.downloads_copy, tree.nondownloads_copy},
            {tree.ctime_older, tree.ctime_newer},
            {tree.depth_shallow, tree.depth_deep},
            {tree.blocked_keep, tree.blocked_duplicate},
        ]
        actual_membership = [{member.path for member in cluster.members} for cluster in clusters]

        def _sort_key(members: set[Path]) -> list[str]:
            return sorted(str(p) for p in members)

        assert sorted(actual_membership, key=_sort_key) == sorted(
            expected_membership, key=_sort_key
        )

        # --- Adversarial near-duplicates (same size, same first/last 64KB, different middle)
        # and the simpler same-size/different-content pair must never cluster together.
        clustered_paths = {member.path for cluster in clusters for member in cluster.members}
        for path in (
            tree.near_dup_a,
            tree.near_dup_b,
            tree.same_size_diff_a,
            tree.same_size_diff_b,
            tree.unique_1,
            tree.unique_2,
        ):
            assert path not in clustered_paths

        # --- Keep-heuristic rule 1: location (outside Downloads/Temp) beats ctime, even
        # though the Downloads copy here is deliberately the *older* one.
        downloads_cluster = _cluster_containing(clusters, tree.downloads_copy)
        assert downloads_cluster.keep.path == tree.nondownloads_copy

        # --- Keep-heuristic rule 2: oldest ctime wins when location ties.
        ctime_cluster = _cluster_containing(clusters, tree.ctime_older)
        assert ctime_cluster.keep.path == tree.ctime_older

        # --- Keep-heuristic rule 3: shortest path depth wins when location and ctime tie.
        depth_cluster = _cluster_containing(clusters, tree.depth_shallow)
        assert depth_cluster.keep.path == tree.depth_shallow

        # --- The blocked pair: clustering must still find it (recall check above already
        # covers this), and its keep is the non-blocked member (shallower path, ctime tied).
        blocked_cluster = _cluster_containing(clusters, tree.blocked_duplicate)
        assert blocked_cluster.keep.path == tree.blocked_keep

        dup_pair_cluster = _cluster_containing(clusters, tree.dup_pair_a)
        assert dup_pair_cluster.keep.path in {tree.dup_pair_a, tree.dup_pair_b}
        dup_pair_non_keep = (
            tree.dup_pair_b if dup_pair_cluster.keep.path == tree.dup_pair_a else tree.dup_pair_a
        )

        triple_cluster = _cluster_containing(clusters, tree.dup_triple_a)
        triple_non_keep = {
            tree.dup_triple_a,
            tree.dup_triple_b,
            tree.dup_triple_c,
        } - {triple_cluster.keep.path}

        disabled_config = _config(tree.root, duplicates_enabled=False)
        disabled = _by_path(
            generate_duplicate_candidates(index, disabled_config, SafetyValidator(disabled_config))
        )
        enabled_config = _config(tree.root, duplicates_enabled=True)
        enabled = _by_path(
            generate_duplicate_candidates(index, enabled_config, SafetyValidator(enabled_config))
        )

    # --- SafetyValidator exclusion, proven end-to-end (not just that clustering found the
    # pair): the blocked member never appears as a candidate in either config.
    for run in (disabled, enabled):
        assert tree.blocked_duplicate not in run

    # --- The keep member of every cluster is never itself a candidate, in either config.
    for cluster in clusters:
        for run in (disabled, enabled):
            assert cluster.keep.path not in run

    # --- Tier gating: Tier B by default (Decision Policy's duplicate-cluster example), Tier A
    # only once categories.duplicates is explicitly enabled.
    assert disabled[dup_pair_non_keep].tier == Tier.B
    assert enabled[dup_pair_non_keep].tier == Tier.A
    assert disabled[dup_pair_non_keep].category == "exact_duplicate"
    assert disabled[dup_pair_non_keep].category_group == "duplicates"

    # --- Rationale names the specific kept path concretely (spec: "rationale naming the
    # specific kept path and why it was kept").
    assert str(dup_pair_cluster.keep.path) in disabled[dup_pair_non_keep].rationale
    assert disabled[dup_pair_non_keep].rebuild_instruction is None

    # --- Triple cluster: exactly the two non-keep members are proposed, in both configs.
    for run in (disabled, enabled):
        for path in triple_non_keep:
            assert path in run
        assert triple_cluster.keep.path not in run
