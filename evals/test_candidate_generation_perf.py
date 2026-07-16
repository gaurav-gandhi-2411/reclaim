from __future__ import annotations

import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
from reclaim.config import CategoriesConfig, Config, DuplicatesConfig
from reclaim.dedup import find_duplicate_clusters, generate_duplicate_candidates
from reclaim.detectors import _drop_nested_candidates, generate_candidates
from reclaim.index import ScanIndex
from reclaim.models import Candidate, FileRecord, RawCandidate, Tier
from reclaim.safety import SafetyValidator

# Regression eval for the real-disk-run memory incident: `generate_candidates()` and
# `generate_duplicate_candidates()` each used to call `ScanIndex.candidate_inventory()` (a
# full-table load into a `{path: FileRecord}` dict) before any detector ran — on a 3.1M-file
# real disk index that cost ~5GB of RAM and 20+ minutes before duplicate-hashing even started.
# This is a perf SMOKE test (mirrors `test_scanner_perf.py`'s framing), not the real spec
# number: it proves peak Python-level memory stays bounded by the number of ACTUAL candidates
# on a 500K-row synthetic index where all but four rows are deliberately unique (never
# hashable, never detector-matched), rather than measuring an absolute number.
_ROW_COUNT = 500_000
_BATCH_SIZE = 20_000
# A full materialization of 500K `FileRecord` objects (each holding a `Path`, several strings,
# ints, floats) plus the old design's two whole-inventory dicts costs several hundred MB on a
# typical CPython build. This ceiling is deliberately generous — well below that, but not so
# tight that normal allocator/generator overhead for the ~6 real candidates flakes it.
_PEAK_MEMORY_CEILING_MB = 50.0


def _mk(
    path: str, *, is_dir: bool = False, size_bytes: int = 1024, mtime: float = 100.0
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=0,
        ext=p.suffix.lower() if not is_dir else "",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=mtime,
        ctime=mtime,
    )


def _synthetic_records(count: int, *, dup_a: Path, dup_b: Path) -> Iterator[FileRecord]:
    """Exactly four rows are real candidates (one dev-artifact dir + its manifest, one
    duplicate-size pair); every other row has a unique name/ext/size and must never be
    detector-matched or hash-candidate-selected. `dup_a`/`dup_b` must be real files on disk
    (with identical content) — the dedup pipeline's hash pass reads real bytes, unlike the
    rule detectors, which only ever query the index."""
    yield _mk("C:/Proj/package.json", size_bytes=50)
    yield _mk("C:/Proj/node_modules", is_dir=True, size_bytes=0)
    yield _mk(str(dup_a), size_bytes=123_456)
    yield _mk(str(dup_b), size_bytes=123_456)
    for i in range(count - 4):
        yield _mk(f"C:/Bulk/dir{i % 5000}/unique_{i}.dat", size_bytes=i + 1_000_000)


def test_candidate_generation_memory_does_not_scale_with_row_count(tmp_path: Path) -> None:
    dup_a = tmp_path / "dup_a.bin"
    dup_b = tmp_path / "dup_b.bin"
    dup_a.write_bytes(b"x" * 123_456)
    dup_b.write_bytes(b"x" * 123_456)

    db_path = tmp_path / "large_index.sqlite3"
    with ScanIndex(db_path) as index:
        batch: list[FileRecord] = []
        for record in _synthetic_records(_ROW_COUNT, dup_a=dup_a, dup_b=dup_b):
            batch.append(record)
            if len(batch) >= _BATCH_SIZE:
                index.upsert_records(batch, scanned_at=1000.0)
                batch.clear()
        if batch:
            index.upsert_records(batch, scanned_at=1000.0)

        # min_reclaim_bytes=0: the dup pair here is 123,456 bytes (below the real 1MB
        # materiality default) by design, to keep this eval's fixture-writing fast — this test
        # is about memory scaling with row count, not materiality (tested separately in
        # test_index.py/test_dedup.py).
        config = Config(
            categories=CategoriesConfig(duplicates=DuplicatesConfig(min_reclaim_bytes=0))
        )
        safety = SafetyValidator(config)

        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        candidates: list[Candidate] = generate_candidates(index, config, safety)
        skips: list = []
        candidates += generate_duplicate_candidates(index, config, safety, skips=skips)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    peak_delta_mb = (peak - baseline) / (1024 * 1024)
    print(  # noqa: T201 -- perf smoke number; run with `pytest -s` to see it
        f"\n[candidate-generation perf smoke] rows={_ROW_COUNT} candidates={len(candidates)} "
        f"peak_python_memory_delta={peak_delta_mb:.2f}MB"
    )

    # Correctness: exactly the deliberate candidates were found, nothing from the 500K bulk
    # unique-size/unique-name rows leaked in as a false positive.
    paths = {c.path for c in candidates}
    assert Path("C:/Proj/node_modules") in paths
    assert dup_a in paths or dup_b in paths
    assert len(candidates) <= 4

    assert peak_delta_mb < _PEAK_MEMORY_CEILING_MB, (
        f"peak Python memory grew {peak_delta_mb:.2f}MB generating {len(candidates)} "
        f"candidates from a {_ROW_COUNT}-row index — this should be independent of row count; "
        "a regression back to a full-table load (e.g. ScanIndex.candidate_inventory()) would "
        "make this scale with _ROW_COUNT instead of staying flat"
    )


# --- The second real-disk finding: a size-uniqueness prefilter barely narrows a real disk ------
#
# The first version of this eval (above) used all-but-4-unique sizes, which proved the
# prefilter's *correctness* but not real-world narrowing power. On the actual real-disk-run
# index, 80% of files (2,485,410 of 3,116,478) shared a size with at least one other file —
# the size-uniqueness prefilter alone barely shrinks the candidate set at all. The regression
# this exposed: `find_duplicate_clusters` used to collect *every* size bucket into one
# `dict[size, list[FileRecord]]` before hashing anything, so peak memory scaled with the total
# candidate count (millions) rather than the largest single bucket. Simulating a multi-million-
# row real disk here would make this eval too slow to run routinely, so this uses a smaller but
# representative shape: most files packed into a handful of common sizes (large buckets), not
# spread almost-uniquely — and asserts peak memory stays bounded by one bucket's worth of
# records, not the whole candidate set.

_SHARED_SIZE_ROW_COUNT = 400_000
_COMMON_SIZES = (111, 222, 333, 444, 555)
# Exactly one real duplicate pair per size bucket — every other same-size file gets a hash
# unique to itself. This is the realistic shape: on the actual real-disk run, files sharing a
# size overwhelmingly turned out to have *different* content once partial-hashed (that's why
# the run needed to hash ~2.5M candidates but presumably found nowhere near that many true
# duplicates). A bucket where every member collides all the way through to one giant cluster
# would be pathological, not representative — and would conflate "peak memory while processing
# one bucket" with "memory legitimately needed to return every true duplicate found," which is
# a different, non-bug cost this eval isn't about.
_DUP_PAIR_SUFFIX = "_dup"
# One bucket here holds ~80,000 records, of which only 2 are real duplicates. A regression back
# to "collect every bucket before hashing any of them" would hold all ~400K at once instead of
# ~80K — several times more memory than one bucket's worth. Generous but decisive.
_BUCKET_STREAMING_MEMORY_CEILING_MB = 150.0


def test_find_duplicate_clusters_memory_bounded_by_largest_bucket_not_total_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_hash(path: Path, *args: object) -> str:
        # The two designated "_dup" files per size share a digest; every other same-size file
        # gets a digest unique to its own name, so it can never collide with anything — matching
        # how same-size-but-different-content files fragment during real partial-hashing.
        name = path.name
        return (
            "shared-digest" if name.endswith(_DUP_PAIR_SUFFIX + path.suffix) else f"unique-{name}"
        )

    monkeypatch.setattr(dedup_module, "_compute_partial_hash", _fake_hash)
    monkeypatch.setattr(dedup_module, "_compute_full_hash", _fake_hash)

    db_path = tmp_path / "shared_size_index.sqlite3"
    with ScanIndex(db_path) as index:
        batch: list[FileRecord] = []
        dup_pair_counters = dict.fromkeys(_COMMON_SIZES, 0)
        for i in range(_SHARED_SIZE_ROW_COUNT):
            size = _COMMON_SIZES[i % len(_COMMON_SIZES)]
            if dup_pair_counters[size] < 2:
                dup_pair_counters[size] += 1
                name = f"file_{i}{_DUP_PAIR_SUFFIX}.bin"
            else:
                name = f"file_{i}.bin"
            batch.append(_mk(f"C:/Bulk/dir{i % 5000}/{name}", size_bytes=size))
            if len(batch) >= 20_000:
                index.upsert_records(batch, scanned_at=1000.0)
                batch.clear()
        if batch:
            index.upsert_records(batch, scanned_at=1000.0)

        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        # min_reclaim_bytes=0: this test is about bucket-streaming memory bounds, not
        # materiality (the common sizes here are large enough to clear the real 1MB default
        # anyway, but 0 keeps that an explicit non-concern rather than an implicit coincidence).
        clusters = find_duplicate_clusters(index, min_reclaim_bytes=0)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    peak_delta_mb = (peak - baseline) / (1024 * 1024)
    print(  # noqa: T201 -- perf smoke number; run with `pytest -s` to see it
        f"\n[dedup bucket-streaming perf smoke] rows={_SHARED_SIZE_ROW_COUNT} "
        f"buckets={len(_COMMON_SIZES)} clusters={len(clusters)} "
        f"peak_python_memory_delta={peak_delta_mb:.2f}MB"
    )

    # Correctness: exactly one 2-member cluster per size bucket — the bulk "everyone else"
    # rows correctly never cluster with anything, despite sharing a size with 79,998 others.
    assert len(clusters) == len(_COMMON_SIZES)
    for cluster in clusters:
        assert len(cluster.duplicates) == 1

    assert peak_delta_mb < _BUCKET_STREAMING_MEMORY_CEILING_MB, (
        f"peak Python memory grew {peak_delta_mb:.2f}MB across {len(_COMMON_SIZES)} buckets of "
        f"~{_SHARED_SIZE_ROW_COUNT // len(_COMMON_SIZES)} records each — expected bounded by "
        "one bucket's worth, not the full candidate set (a regression back to collecting every "
        "bucket before hashing any of them would show up here as multiples of this ceiling)"
    )


# --- The third real-disk finding: _drop_nested_candidates was O(candidates * kept_dirs * depth) -
#
# After the SQL-pushdown and materiality fixes, the real-disk run's candidate-generation phase
# still stalled — this time in pure Python. `_run_all_detectors` found 42,185 raw candidates in
# 3.42s (dominated by many sibling, non-nested `__pycache__`/dev-artifact directories from a
# heavily-used Python dev machine — packages don't nest their bytecode caches inside each
# other), but `_drop_nested_candidates` then ran for 3.5+ minutes without finishing, versus
# milliseconds in every existing test fixture (all of which used at most a few dozen
# candidates). Root cause: `any(directory in candidate.path.parents for directory in
# kept_dirs)` re-scanned the *entire* `kept_dirs` list for every candidate — an
# O(candidates * kept_dirs * depth) blow-up that only shows up once `kept_dirs` itself grows
# large (which requires many non-nested directory candidates, exactly this real-disk shape).
# Fixed by making `kept_dirs` a `set` (O(1) ancestor lookup instead of an O(kept_dirs) scan),
# measured against the real 3.1M-row disk index: 3.5+ minutes (never finished) -> 1.55s.

_LARGE_SIBLING_CANDIDATE_COUNT = 40_000
# Generous but decisive: the old algorithm's growth was severe enough that it hadn't finished
# in over 3.5 minutes (210+ seconds) for a comparable candidate count on the real disk.
_DROP_NESTED_TIME_CEILING_SECONDS = 5.0


def test_drop_nested_candidates_scales_with_candidates_not_kept_dirs_squared() -> None:
    """Worst-case shape for the old algorithm: every candidate is its own kept directory (none
    nested under another), so `kept_dirs` grows to the full candidate count — exactly what
    `__pycache__` directories from thousands of independent packages look like on a real dev
    machine. Mixed in: a genuinely nested case, proving the set-based rewrite didn't trade
    correctness for speed.
    """
    raw: list[RawCandidate] = [
        RawCandidate(
            path=Path(f"C:/proj{i}/__pycache__"),
            is_dir=True,
            category="dev_artifact_pycache",
            category_group="dev_artifacts",
            suggested_tier=Tier.A,
            rationale="test",
        )
        for i in range(_LARGE_SIBLING_CANDIDATE_COUNT)
    ]
    raw.append(
        RawCandidate(
            path=Path("C:/proj0/node_modules"),
            is_dir=True,
            category="dev_artifact_node_modules",
            category_group="dev_artifacts",
            suggested_tier=Tier.A,
            rationale="test",
        )
    )
    raw.append(
        RawCandidate(
            path=Path("C:/proj0/node_modules/pkg/file.js"),
            is_dir=False,
            category="dev_artifact_node_modules",
            category_group="dev_artifacts",
            suggested_tier=Tier.A,
            rationale="test",
        )
    )

    start = time.monotonic()
    kept = _drop_nested_candidates(raw)
    elapsed = time.monotonic() - start
    print(  # noqa: T201 -- perf smoke number; run with `pytest -s` to see it
        f"\n[drop-nested-candidates perf smoke] candidates={len(raw)} kept={len(kept)} "
        f"elapsed={elapsed:.3f}s"
    )

    kept_paths = {c.path for c in kept}
    assert Path("C:/proj0/node_modules") in kept_paths
    assert Path("C:/proj0/node_modules/pkg/file.js") not in kept_paths  # correctly dropped
    assert len(kept) == _LARGE_SIBLING_CANDIDATE_COUNT + 1  # all pycache siblings + node_modules

    assert elapsed < _DROP_NESTED_TIME_CEILING_SECONDS, (
        f"_drop_nested_candidates took {elapsed:.3f}s for {len(raw)} mostly-non-nested "
        f"candidates — expected well under {_DROP_NESTED_TIME_CEILING_SECONDS}s with the "
        "set-based lookup; a regression back to the list-based O(candidates * kept_dirs * "
        "depth) scan would show up here as many seconds to minutes, not a fraction of one"
    )
