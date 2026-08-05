from __future__ import annotations

import ctypes
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path

import pytest
from fixtures.build_scale_tree import (
    DEFAULT_FILLER_FILE_COUNT,
    LARGE_FILLER_FILE_COUNT,
    build_scale_tree,
    load_scale_tree_manifest_json,
)

from reclaim.ai.document_similarity import build_near_dup_document_clusters
from reclaim.ai.image_similarity import build_near_identical_clusters
from reclaim.config import Config
from reclaim.dedup import find_duplicate_clusters
from reclaim.executor import (
    QuarantineManifestEntry,
    apply_batch,
    fold_latest_manifest_entries,
    read_manifest_entries,
    restore_batch,
    rmtree_clear_readonly,
)
from reclaim.index import ScanIndex
from reclaim.models import Candidate, Tier, Verdict
from reclaim.recovery import reconcile_manifest
from reclaim.safety import SafetyValidator
from reclaim.scanner import long_path, scan_tree

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# Wave 1 P0-C: end-to-end proof over ONE coherent, ground-truthed synthetic filesystem
# (evals/fixtures/build_scale_tree.py) that scan -> dedup -> AI near-dup -> executor
# (quarantine/restore) -> crash-recovery all behave correctly together at real file-count scale,
# not just in each stage's own narrower unit fixture.
#
# `max_hamming_distance=14` / `minhash_threshold=0.1` / `embedding_threshold=0.95` are this
# codebase's own MEASURED production operating points (ADR-0012/0015 for the image threshold;
# `reclaim.ai.document_similarity.build_near_dup_document_clusters`'s own docstring for the
# document thresholds) -- reused here, not re-guessed, since this eval's job is proving the
# PIPELINE works end-to-end, not re-deriving operating points `evals/test_ai_copydays_gold.py`/
# `evals/test_ai_document_templated_gold.py` already own.
_IMAGE_MAX_HAMMING_DISTANCE = 14
_DOCUMENT_MINHASH_THRESHOLD = 0.1
_DOCUMENT_EMBEDDING_THRESHOLD = 0.95

# --- CI throughput regression floors for THIS fixture (scale-tree), distinct from
# evals/test_scanner_perf.py's own 150 files/sec floor -----------------------------------------
#
# NOT the same measurement as evals/test_scanner_perf.py's clean ~4,025-plain-file fixture (whose
# own floor is anchored to a 10,247 files/sec baseline, ~11x margin under that number) -- this
# scale-tree fixture is structurally heavier per entry (long paths, unicode names, sparse files,
# junctions, a permission-denied dir, real near-dup image/document content) and measures a
# genuinely different, slower workload. Conflating the two numbers would either make this floor
# meaninglessly loose (10,247-derived) or make test_scanner_perf.py's floor meaninglessly tight
# (this fixture's own, much lower, number) -- kept separate on purpose.
#
# MEASURED (this session, this machine, worktree HEAD at the time of writing), 4 fresh runs of
# the fast-tier scan alone: 6,075 / 6,321 / 6,886 / 9,777 entries/sec. The 4,000 floor this
# produced (70% of the 6,075 minimum, rounded down "for extra headroom on a slower CI runner")
# turned out to be WRONG in practice, not just untested guesswork: real GitHub Actions CI
# (windows-latest) flaked on this exact assertion on unchanged code -- PR #31, run
# 30998493407, first attempt measured 2,368 entries/sec (well under 4,000) and FAILED; an
# immediate rerun of the identical commit PASSED. That's real CI-fleet hardware/scheduling
# variance, not a code regression (rule: flaky tests get fixed the day they flake, not tolerated).
# Root cause this floor didn't account for: the fast tier's own scan is very short (~1-1.5s wall
# time for ~3,100 entries), so a fixed amount of one-time overhead (Defender scanning freshly
# created fixture files, OS filesystem cache still cold, a GC pause, a noisy-neighbor VM) is a much
# larger RELATIVE hit on a ~1s measurement than on the 100k-tier's ~170s one -- short-duration
# throughput floors are inherently noisier than long ones, and 4,000 didn't leave real margin for
# that. New floor set well below the one observed real CI failure (2,368), not re-derived from the
# dev-machine-only sample above -- this is now a "catch an order-of-magnitude regression" tripwire,
# not a tight performance gate; the dev-machine numbers above stay as a local sanity-check
# reference, not what CI is actually held to.
_FAST_TIER_MIN_ENTRIES_PER_SECOND = 1_200.0

# MEASURED (this session, this machine), one fresh run (the 100k+ tier is too slow to sample
# repeatedly in routine development -- see this test's own @pytest.mark.scale gating): 7,255
# entries/sec. 70% tolerance -> ~5,079; rounded down to 5,000. This tier only ever runs in the
# scale-gated nightly/main-push job (see .github/workflows/), never per-PR, so a same-session
# re-run to build variance the way the fast tier's 4-run sample did wasn't worth the extra ~2.5
# minutes per run -- a single real measurement plus the SAME 70%-tolerance convention already used
# above is the honest, disclosed baseline here, not a fabricated multi-run statistic.
_SCALE_TIER_MIN_ENTRIES_PER_SECOND = 5_000.0


def _cleanup_tree(root: Path) -> None:
    """`shutil.rmtree` alone can fail against this fixture for two DELIBERATE reasons (both are
    the fixture doing its job, not a bug): the long-path case genuinely exceeds MAX_PATH (needs
    `long_path()`'s `\\\\?\\` prefix), and the permission-denied case carries a real deny ACE
    (needs `icacls /reset` before anything under it can be removed). Mirrors `executor.py`'s own
    `rmtree_clear_readonly` for the read-only-file case (git objects, etc.).

    Confirmed empirically while writing this eval: `icacls /reset` itself is NOT reliably able
    to reverse the deny ACE -- whether it succeeds depends on ACL-inheritance context this eval
    doesn't control (observed both outcomes across otherwise-identical directories in this same
    session), matching `build_scale_tree.PermissionDeniedInfo`'s own "measure, don't assume"
    posture and `tests/test_recovery.py`'s documented finding that this exact mechanism isn't
    universally reliable. A leftover-undeletable directory must never fail an otherwise-passing
    test's teardown (that would misreport a real test PASS as a harness ERROR) -- best-effort,
    logged loudly if incomplete, same "cleanup best-effort... discoverable, never silent"
    posture as `executor._cleanup_dst_and_empty_parent`.
    """
    denied_dir = root / "special" / "permission_denied"
    if denied_dir.exists():
        subprocess.run(  # noqa: S603 -- fixed argv, local fixture path only
            ["icacls", str(denied_dir), "/reset", "/T", "/C"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    if not root.exists():
        return
    try:
        shutil.rmtree(long_path(root), onexc=rmtree_clear_readonly)
    except PermissionError as exc:
        print(  # noqa: T201 -- best-effort cleanup diagnostic, not a test assertion
            f"[e2e scale test cleanup] could not fully remove {root} -- left behind for manual "
            f"cleanup (pytest retains only its 3 most recent tmp roots, so this self-limits): "
            f"{exc}"
        )


@pytest.fixture
def scale_root(tmp_path: Path) -> Iterator[Path]:
    """A dedicated subdirectory (not `tmp_path` itself) so `_cleanup_tree`'s explicit teardown
    below -- required because pytest's own `tmp_path` cleanup has neither `long_path()` prefixing
    nor an ACL-reset step, and would otherwise leave a locked/undeletable directory behind in the
    shared pytest temp root across every test in this module -- has an unambiguous target.
    """
    root = tmp_path / "scale_tree"
    yield root
    _cleanup_tree(root)


def _safety() -> SafetyValidator:
    return SafetyValidator(Config())


# --- pairwise-clustering precision/recall, shared by the exact-dup and both near-dup evals ------


def _pairs(clusters: list[list[Path]]) -> set[frozenset[Path]]:
    """Every unordered pair of paths that share a cluster, across every cluster of size >= 2.
    Standard pairwise-clustering evaluation (see e.g. `evals/ai_fixtures`' own BCubed framing
    for a related but member-weighted alternative) -- chosen here because it degrades gracefully
    when predicted/ground-truth cluster BOUNDARIES don't match exactly (e.g. a 3-member true
    cluster split into a correctly-identified pair plus one missed singleton still scores
    partial credit, rather than an all-or-nothing per-cluster match)."""
    pairs: set[frozenset[Path]] = set()
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        for a, b in itertools.combinations(cluster, 2):
            pairs.add(frozenset((a, b)))
    return pairs


def _pairwise_precision_recall(
    predicted_clusters: list[list[Path]], truth_clusters: list[list[Path]]
) -> tuple[float, float]:
    """`(precision, recall)` over same-cluster PAIRS. `precision=1.0`/`recall=1.0` by convention
    when the respective denominator (predicted pairs / truth pairs) is zero -- an empty
    prediction against an empty ground truth is a vacuous perfect match, never a divide-by-zero
    crash; an empty prediction against a NON-empty ground truth still correctly scores
    `recall=0.0` since that branch is only taken when `truth_pairs` itself is empty."""
    predicted_pairs = _pairs(predicted_clusters)
    truth_pairs = _pairs(truth_clusters)
    if not predicted_pairs and not truth_pairs:
        return 1.0, 1.0
    correct = predicted_pairs & truth_pairs
    precision = len(correct) / len(predicted_pairs) if predicted_pairs else 0.0
    recall = len(correct) / len(truth_pairs) if truth_pairs else 1.0
    return precision, recall


# --- 1. full scan_tree() completes without raising, fast tier + 100k+ tier ----------------------


def test_full_scan_completes_without_raising_fast_tier(scale_root: Path) -> None:
    """`scan_tree` must walk the WHOLE fixture (every special case: long path, junctions
    including the deliberate cycle, unicode/emoji names, zero-byte/large/sparse files,
    permission-denied dir, exact/near-dup sets, bulk filler) without raising. This is the
    structural proof the junction-cycle case can't hang the walk -- see `build_scale_tree.
    _plant_junction_cycle`'s own docstring for why that's already guaranteed by `scan_tree`'s
    reparse-point-is-never-recursed-into contract, and
    `tests/test_scanner.py::test_scan_tree_reparse_point_is_recorded_but_not_recursed_into` for
    the existing unit proof of that contract in isolation."""
    manifest = build_scale_tree(scale_root, filler_file_count=DEFAULT_FILLER_FILE_COUNT)

    db_path = scale_root.parent / "_index.sqlite3"
    start = time.perf_counter()
    with ScanIndex(db_path) as index:
        stats = scan_tree(manifest.root, index, incremental=False)
    elapsed = time.perf_counter() - start
    entries_per_second = stats.entries_total / elapsed

    print(  # noqa: T201 -- eval numbers; run with `pytest -s` to see them
        f"\n[e2e scale, fast tier] filler={manifest.filler_file_count} "
        f"planted={manifest.total_planted_files} entries_total={stats.entries_total} "
        f"dirs_visited={stats.dirs_visited} skipped={stats.skipped_unreadable_count} "
        f"elapsed={elapsed:.2f}s ({entries_per_second:.0f} entries/sec)"
    )
    assert stats.entries_total > manifest.filler_file_count
    # The junction and its cycle are recorded as leaf entries (D12 contract), never skipped.
    assert stats.skipped_unreadable_count >= 0  # always true; see the dedicated assertion below
    # CI regression floor -- see this module's own comment above _FAST_TIER_MIN_ENTRIES_PER_SECOND
    # for the measured baseline and tolerance. Distinct from evals/test_scanner_perf.py's
    # 10,247-baseline floor -- this fixture is a heavier, different workload.
    assert entries_per_second >= _FAST_TIER_MIN_ENTRIES_PER_SECOND, (
        f"scale-tree fast-tier scan throughput ({entries_per_second:.0f} entries/sec) fell below "
        f"the {_FAST_TIER_MIN_ENTRIES_PER_SECOND:.0f} entries/sec CI regression floor"
    )


def test_full_scan_records_every_special_case_leaf_entry(scale_root: Path) -> None:
    """Narrower companion to the smoke test above: every individually-named special-case path
    must actually appear in the final index (not just "the scan didn't crash") -- the long-path
    payload, both junctions (as reparse-point leaves, D12), the sparse/large/zero-byte files, and
    every unicode/emoji-named file."""
    manifest = build_scale_tree(scale_root, filler_file_count=200)

    db_path = scale_root.parent / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(manifest.root, index, incremental=False)
        inventory = index.full_inventory(under=manifest.root)

    indexed_paths = {record.path for record in inventory}
    assert manifest.long_path_file in indexed_paths
    assert manifest.zero_byte_file in indexed_paths
    assert manifest.large_file in indexed_paths
    assert manifest.sparse_file.path in indexed_paths
    for unicode_path in manifest.unicode_emoji_files:
        assert unicode_path in indexed_paths, f"{unicode_path} missing from scan inventory"
    if manifest.junction.created:
        assert manifest.junction.link_path in indexed_paths
        # The junction's TARGET content must never appear via the link -- proves the walk
        # recorded the reparse point as a leaf and never recursed through it.
        assert not any(
            "inside_target.txt" in str(p) and manifest.junction.link_path in p.parents
            for p in indexed_paths
        )
    if manifest.junction_cycle.created:
        assert manifest.junction_cycle.link_path in indexed_paths


@pytest.mark.scale
def test_full_scan_completes_without_raising_100k_tier(scale_root: Path) -> None:
    """The task's explicit 100k+ tier. Marked `@pytest.mark.scale` -- NOT wired into any default
    CI selection here (that's an explicit follow-up per this eval's own scope); run directly via
    `pytest evals/test_e2e_scale.py -m scale -v`. Reports real generation + scan wall-clock time
    and, best-effort, real peak process working-set memory (Win32 `GetProcessMemoryInfo` --
    no `psutil` dependency needed; see the helper below) -- house rule 65b, never an estimate.
    """
    generation_start = time.perf_counter()
    manifest = build_scale_tree(scale_root, filler_file_count=LARGE_FILLER_FILE_COUNT)
    generation_elapsed = time.perf_counter() - generation_start

    db_path = scale_root.parent / "_index.sqlite3"
    peak_before = _peak_working_set_bytes()
    scan_start = time.perf_counter()
    with ScanIndex(db_path) as index:
        stats = scan_tree(manifest.root, index, incremental=False)
    scan_elapsed = time.perf_counter() - scan_start
    peak_after = _peak_working_set_bytes()
    entries_per_second = stats.entries_total / scan_elapsed

    print(  # noqa: T201
        f"\n[e2e scale, 100k+ tier] filler={manifest.filler_file_count} "
        f"planted={manifest.total_planted_files} generation_elapsed={generation_elapsed:.2f}s "
        f"({manifest.total_planted_files / generation_elapsed:.0f} files/sec) "
        f"entries_total={stats.entries_total} dirs_visited={stats.dirs_visited} "
        f"scan_elapsed={scan_elapsed:.2f}s ({entries_per_second:.0f} entries/sec) "
        f"peak_working_set_before={peak_before / 1024 / 1024:.1f}MB "
        f"peak_working_set_after={peak_after / 1024 / 1024:.1f}MB "
        "(NOTE: peak_working_set_before/after include this process's fixture-generation memory "
        "too -- NOT a scan-isolated figure; see evals/test_scanner_peak_rss_budget.py for the "
        "clean, subprocess-isolated peak-RSS regression budget)"
    )
    assert stats.entries_total >= LARGE_FILLER_FILE_COUNT
    # CI regression floor (scale-gated -- this whole test only runs via `pytest -m scale`, never
    # per-PR). See this module's own comment above _SCALE_TIER_MIN_ENTRIES_PER_SECOND for the
    # measured baseline and tolerance.
    assert entries_per_second >= _SCALE_TIER_MIN_ENTRIES_PER_SECOND, (
        f"scale-tree 100k+-tier scan throughput ({entries_per_second:.0f} entries/sec) fell "
        f"below the {_SCALE_TIER_MIN_ENTRIES_PER_SECOND:.0f} entries/sec CI regression floor"
    )


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_working_set_bytes() -> int:
    """Real OS-level peak RSS for THIS process via `psapi.GetProcessMemoryInfo` -- no `psutil`
    dependency (not currently installed; adding it wasn't authorized for this task). Requires
    explicit `argtypes`/`restype` on both Win32 calls -- confirmed empirically while writing
    this eval that ctypes' default `c_int` treatment of every argument silently breaks the call
    (returns success but a bogus zero value) without `HANDLE`/`DWORD`-typed `argtypes`."""
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    ok = get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb)
    if not ok:
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


# --- 2. duplicate/near-dup precision & recall against the fixture's ground truth -----------------


def test_exact_duplicate_detection_precision_recall(scale_root: Path) -> None:
    """`find_duplicate_clusters` (the real Stage-4 BLAKE3 exact-dup pipeline) against
    `build_scale_tree`'s planted `exact_duplicate_sets` ground truth. `min_reclaim_bytes=0`
    disables the materiality gate -- this fixture's deliberately small dup-set sizes (512B-64KB)
    would otherwise be excluded before hashing even runs, which would test the materiality gate
    instead of the clustering algorithm itself."""
    manifest = build_scale_tree(scale_root, filler_file_count=DEFAULT_FILLER_FILE_COUNT)

    db_path = scale_root.parent / "_index.sqlite3"
    with ScanIndex(db_path) as index:
        scan_tree(manifest.root, index, incremental=False)
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)

    predicted = [[m.path for m in cluster.members] for cluster in clusters]
    truth = [list(s.paths) for s in manifest.exact_duplicate_sets]
    precision, recall = _pairwise_precision_recall(predicted, truth)

    print(f"\n[e2e scale] exact-dup precision={precision:.4f} recall={recall:.4f}")  # noqa: T201

    # MEASURED at 1.0/1.0 on this fixture (deterministic content, no size/hash collisions with
    # bulk filler by construction -- see build_scale_tree's own docstring). Floor set at 0.95,
    # not 1.0, to absorb legitimate non-determinism (a filesystem-timing/hash-cache edge case)
    # without the whole eval flapping on a one-off — never loosened further without a fresh
    # measurement replacing this comment.
    assert precision >= 0.95, f"exact-dup precision {precision:.4f} below floor"
    assert recall >= 0.95, f"exact-dup recall {recall:.4f} below floor"


def test_near_dup_image_detection_precision_recall(scale_root: Path) -> None:
    """`build_near_identical_clusters` (Feature 1a Track A) against the reused `ai_fixtures.
    build_image_similarity_fixtures` ground truth, at this codebase's own MEASURED production
    operating point (`max_hamming_distance=14`, ADR-0012/0015)."""
    manifest = build_scale_tree(scale_root, filler_file_count=200)
    image_paths = [p for cluster in manifest.near_dup_image_clusters for p in cluster.paths]
    # Include the distractor images too (real negative-control signal for precision) -- read
    # them straight off disk rather than re-deriving from the manifest, which only records real
    # multi-member clusters.
    image_paths += list((manifest.images_root / "distractors").glob("*.jpg"))

    clusters = build_near_identical_clusters(
        image_paths, safety=_safety(), max_hamming_distance=_IMAGE_MAX_HAMMING_DISTANCE
    )

    predicted = [[m.path for m in cluster.members] for cluster in clusters]
    truth = [list(c.paths) for c in manifest.near_dup_image_clusters]
    precision, recall = _pairwise_precision_recall(predicted, truth)

    print(f"\n[e2e scale] near-dup-image precision={precision:.4f} recall={recall:.4f}")  # noqa: T201

    # MEASURED at 1.0/1.0 on this fixture. Floor set at 0.8 (real margin, not just-above-measured)
    # -- this fixture's clusters are a subset of `evals/test_ai_image_similarity.py`'s own richer
    # gold set, so a small measured shortfall here would be a real signal worth investigating, but
    # this eval's job is pipeline integration, not re-owning that gold-set's calibration.
    assert precision >= 0.8, f"near-dup-image precision {precision:.4f} below floor"
    assert recall >= 0.8, f"near-dup-image recall {recall:.4f} below floor"


def test_near_dup_document_detection_precision_recall(scale_root: Path) -> None:
    """`build_near_dup_document_clusters` (Feature 1b) against the reused `ai_fixtures.
    build_document_similarity_fixtures` ground truth, at this codebase's own MEASURED production
    operating points (`minhash_threshold=0.1`, `embedding_threshold=0.95`)."""
    manifest = build_scale_tree(scale_root, filler_file_count=200)
    doc_paths = [p for cluster in manifest.near_dup_document_clusters for p in cluster.paths]
    doc_paths += list((manifest.documents_root / "distractors").glob("*.txt"))

    clusters = build_near_dup_document_clusters(
        doc_paths,
        safety=_safety(),
        minhash_threshold=_DOCUMENT_MINHASH_THRESHOLD,
        embedding_threshold=_DOCUMENT_EMBEDDING_THRESHOLD,
    )

    predicted = [[m.path for m in cluster.members] for cluster in clusters]
    truth = [list(c.paths) for c in manifest.near_dup_document_clusters]
    precision, recall = _pairwise_precision_recall(predicted, truth)

    print(f"\n[e2e scale] near-dup-document precision={precision:.4f} recall={recall:.4f}")  # noqa: T201

    # See test_near_dup_image_detection_precision_recall's comment for the floor-setting
    # rationale -- same posture, applied to the document pipeline's own MEASURED number here.
    assert precision >= 0.8, f"near-dup-document precision {precision:.4f} below floor"
    assert recall >= 0.8, f"near-dup-document recall {recall:.4f} below floor"


# --- 3. vault apply is atomic: all-or-nothing per item, even mid-crash --------------------------

_HARNESS_PATH = Path(__file__).parent.parent / "tests" / "_recovery_crash_harness.py"
_CRASH_EXIT_CODE = 9  # mirrors tests/_recovery_crash_harness.py's own constant


def _run_crash_harness(
    config: dict[str, object], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Runs `tests/_recovery_crash_harness.py` as a genuinely separate child process so its
    `os._exit()` is a real simulated hard-crash, not a caught Python exception -- reusing the
    EXACT harness `tests/test_recovery.py` already owns (confirmed by reading it: its
    `operation="apply"` path calls `reclaim.executor.apply_batch` directly against real
    candidates, which is the real production entry point this eval needs), rather than
    inventing a second one."""
    config_path = tmp_path / "_harness_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted local test fixture
        [sys.executable, str(_HARNESS_PATH), str(config_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vault_apply_batch_is_atomic_per_item_across_a_hard_crash(
    scale_root: Path, tmp_path: Path
) -> None:
    """Proves ADR-0026's atomicity contract against a REAL scale-fixture batch: kill the process
    (`os._exit`, bypassing every `finally:`) right after item 2's intent is durably fsynced but
    before its real vault move runs. Items 0-1 must be fully moved (source gone, vault copy
    present, byte-identical); item 2 must be left in a well-defined PRE-move state (source
    untouched, no vault copy at all -- never a partial/half-written vault file); items 3-4 must
    never have been attempted at all (no manifest entry, source untouched). This is "all-or-
    nothing per item" -- never a file left half-moved with no record of what happened to it."""
    manifest = build_scale_tree(scale_root, filler_file_count=50)
    candidate_files = sorted(manifest.bulk_root.rglob("*.bin"))[:5]
    assert len(candidate_files) == 5
    original_hashes = {p: _sha256(p) for p in candidate_files}

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"
    harness_config = {
        "operation": "apply",
        "checkpoint": "after_intent_fsync",
        "crash_index": 2,
        "manifest_path": str(manifest_path),
        "vault_dir": str(vault_dir),
        "now": 1_700_000_000.0,
        "items": [{"path": str(p), "size_bytes": p.stat().st_size} for p in candidate_files],
    }
    result = _run_crash_harness(harness_config, tmp_path)
    assert result.returncode == _CRASH_EXIT_CODE, (
        f"crash hook never fired as expected -- stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    entries = fold_latest_manifest_entries(manifest_path)
    done_paths = {e.original_path for e in entries if e.phase == "done"}
    assert set(candidate_files[:2]) == done_paths

    for path in candidate_files[:2]:
        entry = next(e for e in entries if e.original_path == path)
        assert entry.vault_path is not None
        assert not path.exists(), f"{path} should have been moved out of its original location"
        assert entry.vault_path.exists()
        assert _sha256(entry.vault_path) == original_hashes[path], (
            f"{path}'s vaulted copy is not byte-identical to the original"
        )

    # Item 2 (crash_index=2): intent was durably written, but the real move never ran -- source
    # must be completely untouched, and there must be no vault copy anywhere (not even partial).
    crashed_path = candidate_files[2]
    assert crashed_path.exists()
    assert _sha256(crashed_path) == original_hashes[crashed_path]

    # Items 3-4: never attempted -- no manifest entry of any kind, source untouched.
    attempted_paths = {Path(e.original_path) for e in _raw_entries(manifest_path)}
    for path in candidate_files[3:]:
        assert path not in attempted_paths
        assert path.exists()
        assert _sha256(path) == original_hashes[path]


def _raw_entries(manifest_path: Path) -> list[QuarantineManifestEntry]:
    return read_manifest_entries(manifest_path)


# --- 4. crashed run recovers via recovery.py, verified against real post-crash disk state -------


def test_crashed_apply_reconciles_correctly_against_real_disk_state(
    scale_root: Path, tmp_path: Path
) -> None:
    """Same crash scenario as the atomicity test above, but this one calls `reclaim.recovery.
    reconcile_manifest` afterward and verifies the RECONCILED manifest state matches real,
    independently-checked disk state -- not just that reconciliation ran without raising."""
    manifest = build_scale_tree(scale_root, filler_file_count=50)
    candidate_files = sorted(manifest.bulk_root.rglob("*.bin"))[5:10]
    assert len(candidate_files) == 5
    original_hashes = {p: _sha256(p) for p in candidate_files}

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"
    harness_config = {
        "operation": "apply",
        "checkpoint": "after_action_before_done_fsync",
        "crash_index": 1,  # crash right after item 1's REAL move, before its "done" record
        "manifest_path": str(manifest_path),
        "vault_dir": str(vault_dir),
        "now": 1_700_000_000.0,
        "items": [{"path": str(p), "size_bytes": p.stat().st_size} for p in candidate_files],
    }
    result = _run_crash_harness(harness_config, tmp_path)
    assert result.returncode == _CRASH_EXIT_CODE

    report = reconcile_manifest(manifest_path, vault_dir, now=1_700_000_100.0)
    assert len(report.reconciled) == 1
    reconciled_item = report.reconciled[0]
    assert reconciled_item.outcome == "completed"  # action ran, only the done fsync was lost

    # Independently re-verify against REAL disk state, not the reconciliation report's own claim.
    crashed_path = candidate_files[1]
    assert not crashed_path.exists()
    entries = fold_latest_manifest_entries(manifest_path)
    crashed_entry = next(e for e in entries if e.original_path == crashed_path)
    assert crashed_entry.phase == "done"
    assert crashed_entry.vault_path is not None
    assert crashed_entry.vault_path.exists()
    assert _sha256(crashed_entry.vault_path) == original_hashes[crashed_path]

    # Items before the crash (item 0) completed normally; items after (2-4) never started.
    assert not candidate_files[0].exists()
    for path in candidate_files[2:]:
        assert path.exists()
        assert _sha256(path) == original_hashes[path]


# --- 5. restore returns bit-identical files -------------------------------------------------------


def test_restore_returns_bit_identical_files(scale_root: Path, tmp_path: Path) -> None:
    """Real (non-crashed) `apply_batch` -> `restore_batch` round trip against scale-fixture
    files, comparing SHA-256 hashes captured BEFORE quarantine against hashes read back AFTER
    restore -- not merely `path.exists()`, which would miss silent corruption."""
    manifest = build_scale_tree(scale_root, filler_file_count=50)
    candidate_files = sorted(manifest.bulk_root.rglob("*.bin"))[10:15]
    original_hashes = {p: _sha256(p) for p in candidate_files}

    candidates = [
        Candidate(
            path=p,
            is_dir=False,
            category="test_category",
            category_group="test_group",
            size_bytes=p.stat().st_size,
            tier=Tier.A,
            rationale="e2e scale restore-identity test",
            rebuild_instruction=None,
            safety_verdict=Verdict.ELIGIBLE,
            safety_reason_code="TEST_REASON",
            retention_days=30,
        )
        for p in candidate_files
    ]

    vault_dir = tmp_path / "vault"
    manifest_path = tmp_path / "manifest.jsonl"
    safety = _safety()
    apply_report = apply_batch(
        candidates,
        safety=safety,
        apply=True,
        method="vault",
        vault_dir=vault_dir,
        manifest_path=manifest_path,
        now=1_700_000_000.0,
    )
    assert apply_report.files_failed == 0
    for p in candidate_files:
        assert not p.exists()

    restore_report = restore_batch(
        apply_report.batch_id,
        manifest_path=manifest_path,
        vault_dir=vault_dir,
        safety=safety,
        now=1_700_000_100.0,
    )
    assert restore_report.files_failed == 0
    assert restore_report.files_succeeded == len(candidate_files)

    for p in candidate_files:
        assert p.exists(), f"{p} was not restored to its original location"
        restored_hash = _sha256(p)
        assert restored_hash == original_hashes[p], f"{p} restored with different content"


# --- 6. scan cancellation -- documented gap, not fabricated -------------------------------------


def test_cancel_mid_scan_leaves_a_consistent_index(
    scale_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un-skipped (Wave 1 scan cancellation): `scan_tree` now accepts a cooperative
    `cancel_event` (see `reclaim.scanner.scan_tree`'s own docstring for the full mechanism --
    checked at directory/entry granularity during the walk, every already-flushed batch stays
    durable, `prune_unseen_under_root` is skipped entirely on a cancelled run).

    This proves the real, load-bearing property a cancellation mechanism must guarantee against
    this codebase's actual special-case-heavy fixture (long path, junctions, unicode names,
    permission-denied dir, exact/near-dup sets -- not just a plain synthetic tree): a scan
    cancelled genuinely MID-WALK (not before it starts, not after it's already finished) leaves
    the index in a consistent, queryable state with no torn writes and no wrongly-pruned entries,
    and a second, uncancelled `scan_tree` pass over the SAME root then completes the inventory --
    the "resume" property the incremental design already gives for free (see
    `test_resumed_scan_after_a_partial_stop_completes_correctly` above), now exercised from a
    REAL cancellation rather than that test's narrower-root approximation.

    `_HEARTBEAT_INTERVAL_SECONDS` monkeypatched to 0.0 (this repo's established convention for
    exercising `on_progress`-driven behavior deterministically -- see
    `tests/test_scanner.py::test_scan_tree_cancel_event_stops_the_walk_early`) makes
    `scan_tree`'s progress callback fire on essentially every entry, so cancellation triggers on
    a deterministic entry-count threshold, never a wall-clock race that could flake on a slower
    CI runner.
    """
    import reclaim.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
    manifest = build_scale_tree(scale_root, filler_file_count=DEFAULT_FILLER_FILE_COUNT)

    db_path = scale_root.parent / "_index.sqlite3"
    cancel_event = threading.Event()
    _CANCEL_AT = 200  # well under this fixture's real entry count (>3,000 filler files alone)

    def on_progress(processed: int, _total: int | None, _elapsed: float) -> None:
        if processed >= _CANCEL_AT:
            cancel_event.set()

    with ScanIndex(db_path) as index:
        partial_stats = scan_tree(
            manifest.root,
            index,
            incremental=False,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        partial_inventory = index.full_inventory(under=manifest.root)

        # The scan call itself returned cleanly -- no exception, no hang -- and genuinely
        # stopped mid-walk rather than racing to completion before the threshold could land.
        assert partial_stats.cancelled is True
        assert partial_stats.entries_total >= _CANCEL_AT
        assert partial_stats.files_pruned == 0
        # No torn state: the index holds exactly what the (partial) scan reports, no more and
        # no fewer -- every already-flushed batch is durable, nothing half-written.
        assert len(partial_inventory) == partial_stats.entries_total

        # A second, uncancelled pass over the SAME root/index completes the inventory --
        # incremental=True exercises the real rescan-on-top-of-partial-state code path a real
        # resume would use, not a from-scratch rebuild.
        full_stats = scan_tree(manifest.root, index, incremental=True)
        full_inventory = index.full_inventory(under=manifest.root)

    assert full_stats.cancelled is False
    # Proves the first pass really was partial (not accidentally already-complete): the second
    # pass covers strictly more ground, and the final index strictly grew.
    assert full_stats.entries_total > partial_stats.entries_total
    assert len(full_inventory) == full_stats.entries_total
    assert len(full_inventory) > len(partial_inventory)
    assert full_stats.files_pruned == 0  # nothing was actually deleted from disk mid-test

    # The completed index is a genuinely correct full inventory, not just "bigger" -- every
    # special-case leaf entry test_full_scan_records_every_special_case_leaf_entry already pins
    # in isolation must also be present here, post-cancellation-and-resume.
    indexed_paths = {record.path for record in full_inventory}
    assert manifest.long_path_file in indexed_paths
    assert manifest.zero_byte_file in indexed_paths
    assert manifest.large_file in indexed_paths
    assert manifest.sparse_file.path in indexed_paths
    for unicode_path in manifest.unicode_emoji_files:
        assert unicode_path in indexed_paths, f"{unicode_path} missing from scan inventory"
    if manifest.junction.created:
        assert manifest.junction.link_path in indexed_paths


# --- 7. a resumed scan (after a clean stop) completes correctly ---------------------------------


def test_resumed_scan_after_a_partial_stop_completes_correctly(scale_root: Path) -> None:
    """Since `scan_tree` has no true cancellation token (see the skipped test above), this
    approximates "stop partway, then resume" the only way this codebase's actual primitives
    support: a first scan covers only PART of the tree (`bulk/` alone -- as if a caller had
    pointed `scan_tree` at a narrower root and then stopped), then a second, SEPARATE `scan_tree`
    call covers the FULL root (the "resume"). The correctness property under test is real either
    way: the final index must be a complete, correct inventory of the whole tree regardless of
    what was indexed by an earlier, narrower run against the SAME index file -- which is exactly
    what a true resume-after-cancel would also need to guarantee. `incremental=True` for the
    second call exercises the actual code path a real resume would use (rescan on top of
    existing index state), not `incremental=False`'s from-scratch rebuild.
    """
    manifest = build_scale_tree(scale_root, filler_file_count=500)
    db_path = scale_root.parent / "_index.sqlite3"

    with ScanIndex(db_path) as index:
        partial_stats = scan_tree(manifest.bulk_root, index, incremental=False)
        assert partial_stats.entries_total > 0

        full_stats = scan_tree(manifest.root, index, incremental=True)
        inventory = index.full_inventory(under=manifest.root)

    indexed_paths = {r.path for r in inventory}
    assert manifest.long_path_file in indexed_paths
    assert manifest.zero_byte_file in indexed_paths
    for p in (manifest.bulk_root).rglob("*.bin"):
        assert p in indexed_paths
    # The "resume" pass must have re-confirmed (not dropped) everything the partial pass already
    # found -- files_unchanged only counts entries the second pass recognized as already-current.
    assert full_stats.files_unchanged >= partial_stats.entries_total


# --- 8. manifest write/load round-trip (documents the ground-truth artifact is real) -------------


def test_manifest_json_is_written_and_round_trips(scale_root: Path) -> None:
    """Proves `ScaleTreeManifest.write()`'s output is a real, independently-loadable JSON
    artifact -- not just an in-process convenience object -- so a caller can diff against it
    without re-running generation."""
    manifest = build_scale_tree(scale_root, filler_file_count=50)
    manifest_path = manifest.root / "_scale_tree_manifest.json"
    assert manifest_path.exists()

    loaded = load_scale_tree_manifest_json(manifest_path)
    assert loaded["seed"] == manifest.seed
    assert loaded["filler_file_count"] == manifest.filler_file_count
    assert len(loaded["exact_duplicate_sets"]) == len(manifest.exact_duplicate_sets)
    assert len(loaded["near_dup_image_clusters"]) == len(manifest.near_dup_image_clusters)
    assert len(loaded["near_dup_document_clusters"]) == len(manifest.near_dup_document_clusters)
