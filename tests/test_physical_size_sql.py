# ruff: noqa: S311 -- seeded PRNG for deterministic fixtures, not cryptography
from __future__ import annotations

import random
from pathlib import Path

import pytest
from test_api import _config, _make_app

from reclaim.api import service
from reclaim.index import ScanIndex, physical_size_bytes
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FileRecord

# perf/summary-physical-size: `ScanIndex.physical_size_bytes_total` must return EXACTLY what
# `physical_size_bytes(full_inventory(...))` returns, without materializing rows, and
# `/api/summary` must memoize it per scan generation.

_SEED = 42


def _rec(
    path: str, *, size: int, dev: int = 0, ino: int = 0, is_dir: bool = False, cloud: bool = False
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=0 if is_dir else size,
        attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if cloud else 0,
        ext=p.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=100.0,
        ctime=100.0,
        dev=dev,
        ino=ino,
    )


def _random_records(rng: random.Random, n: int) -> list[FileRecord]:
    """Adversarial mix: hardlink groups (sometimes with INCONSISTENT sizes, to pin first-seen),
    dev=ino=0 rows, ino=0 with dev!=0, dev=0 with ino!=0, ids above 2**63 (wrapped in the DB),
    directories, cloud placeholders, zero-byte files."""
    devs = [0, 1, 2, 2**63 + 5, 2**64 - 1]
    records: list[FileRecord] = []
    group_ids = [(rng.choice(devs), rng.randint(0, 6)) for _ in range(8)]
    for i in range(n):
        top = rng.choice(["A", "B", "C"])
        path = f"C:/Vol/{top}/d{rng.randint(0, 4)}/f{i}.bin"
        kind = rng.random()
        if kind < 0.1:
            records.append(_rec(f"C:/Vol/{top}/dir{i}", size=0, is_dir=True, dev=1, ino=i + 1))
            continue
        size = rng.choice([0, 0, 1, 7, 4096, rng.randint(1, 10**6)])
        if kind < 0.45:
            dev, ino = rng.choice(group_ids)  # hardlink-ish group, possibly (0, 0)
            if rng.random() < 0.2:
                size = rng.randint(1, 999)  # inconsistent size inside one inode group
        elif kind < 0.6:
            dev, ino = 0, 0
        else:
            dev, ino = rng.choice(devs), rng.randint(1, 10**6)
        records.append(_rec(path, size=size, dev=dev, ino=ino, cloud=rng.random() < 0.15))
    return records


@pytest.mark.parametrize("case", range(50))
def test_sql_total_equals_python_physical_size_for_random_indexes(
    tmp_path: Path, case: int
) -> None:
    rng = random.Random(_SEED + case)
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        idx.upsert_records(_random_records(rng, rng.randint(1, 120)), scanned_at=1.0)
        assert idx.physical_size_bytes_total() == physical_size_bytes(idx.full_inventory())
        for scope in ("C:/Vol", "C:/Vol/A", "C:/Vol/B/d1", "C:/Vol/Z"):
            under = Path(scope)
            assert idx.physical_size_bytes_total(under) == physical_size_bytes(
                idx.full_inventory(under=under)
            ), scope


def test_empty_index_is_zero(tmp_path: Path) -> None:
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        assert idx.physical_size_bytes_total() == 0


def test_hand_checked_semantics(tmp_path: Path) -> None:
    rows = [
        _rec("C:/x/a", size=100, dev=1, ino=5),
        _rec("C:/x/b", size=100, dev=1, ino=5),  # hardlink: counted once
        _rec("C:/x/c", size=10),  # (0, 0): each counts
        _rec("C:/x/d", size=20),
        _rec("C:/x/e", size=7, dev=3, ino=0),  # ino=0 but dev!=0: deduplicated by Python rule
        _rec("C:/x/f", size=7, dev=3, ino=0),
        _rec("C:/x/g", size=1000, dev=9, ino=9, cloud=True),  # placeholders ARE counted
        _rec("C:/x/dir", size=0, is_dir=True, dev=1, ino=77),
    ]
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        idx.upsert_records(rows, scanned_at=1.0)
        assert idx.physical_size_bytes_total() == 100 + 10 + 20 + 7 + 1000


def _plan(idx: ScanIndex, sql: str) -> list[str]:
    return [r["detail"] for r in idx._conn.execute("EXPLAIN QUERY PLAN " + sql)]


def test_unscoped_plan_is_a_table_scan_with_or_without_statistics(tmp_path: Path) -> None:
    """Same discipline as `_DISTINCT_INODES_PER_SIZE_SQL` (#135): the aggregate reads every row
    once, so its plan must not flip to a non-covering index once ANALYZE statistics exist."""
    captured: list[str] = []
    with ScanIndex(tmp_path / "index.sqlite3") as idx:
        idx.upsert_records(
            [
                _rec(f"C:/Big/d{i % 40}/f{i}.bin", size=i % 997, dev=1, ino=i % 5000)
                for i in range(20_000)
            ],
            scanned_at=1.0,
        )
        idx._conn.set_trace_callback(captured.append)
        for stage in ("no_stats", "analyzed"):
            if stage == "analyzed":
                idx.refresh_planner_stats()
            captured.clear()
            idx.physical_size_bytes_total()
            sql = next(s for s in captured if "GROUP BY" in s)
            assert "NOT INDEXED" in sql
            plan = _plan(idx, sql)
            assert any(d == "SCAN files" for d in plan), (stage, plan)
            assert not any("USING" in d and "INDEX" in d for d in plan if "files" in d), (
                stage,
                plan,
            )
            if stage == "analyzed":
                # Why the pin exists: unpinned, the planner walks the non-covering
                # `idx_files_dev_ino` (a random row lookup per entry) once stats exist.
                unpinned = _plan(idx, sql.replace(" NOT INDEXED", ""))
                assert any("idx_files_dev_ino" in d for d in unpinned), unpinned


# --- /api/summary wiring --------------------------------------------------------------------


def _seed_state_index(state: object, rng: random.Random) -> None:
    with ScanIndex(state.db_path) as idx:  # type: ignore[attr-defined]
        idx.upsert_records(_random_records(rng, 80), scanned_at=1.0)


def test_build_summary_is_identical_to_the_pre_change_computation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    state = client.app.state.reclaim  # type: ignore[attr-defined]
    _seed_state_index(state, random.Random(_SEED))

    new = service.build_summary(state, require_warm=False)

    def old_computation(index: ScanIndex, _state: object, **_kw: object) -> int:
        return physical_size_bytes(index.full_inventory())  # verbatim pre-change expression

    monkeypatch.setattr(service, "cached_physical_size_bytes", old_computation)
    old = service.build_summary(state, require_warm=False)
    assert new.has_scan is True
    assert new.total_indexed_bytes > 0
    assert new.model_dump_json() == old.model_dump_json()


def test_summary_total_is_computed_once_per_scan_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    state = client.app.state.reclaim  # type: ignore[attr-defined]
    _seed_state_index(state, random.Random(_SEED))

    calls: list[Path | None] = []
    real = ScanIndex.physical_size_bytes_total

    def spy(self: ScanIndex, under: Path | None = None) -> int:
        calls.append(under)
        return real(self, under)

    monkeypatch.setattr(ScanIndex, "physical_size_bytes_total", spy)
    # The first call also computes candidates, and the dedup pass writes hash columns into the
    # index (changing its WAL stat), so the cache may legitimately miss once more on call 2;
    # what matters is that a WARM steady state stops recomputing.
    first = service.build_summary(state, require_warm=False)
    service.build_summary(state, require_warm=False)
    warm_calls = len(calls)
    assert warm_calls <= 2
    third = service.build_summary(state, require_warm=False)
    fourth = service.build_summary(state, require_warm=False)
    assert len(calls) == warm_calls
    assert first.total_indexed_bytes == third.total_indexed_bytes == fourth.total_indexed_bytes

    state.scan_generation += 1  # a new scan completed
    service.build_summary(state, require_warm=False)
    assert len(calls) == warm_calls + 1


def test_summary_total_recomputes_when_the_index_changes_without_a_generation_bump(
    tmp_path: Path,
) -> None:
    """Another process (CLI scan / prune) can change the index; the stat signature catches it."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    state = client.app.state.reclaim  # type: ignore[attr-defined]
    _seed_state_index(state, random.Random(_SEED))
    before = service.build_summary(state, require_warm=False).total_indexed_bytes
    with ScanIndex(state.db_path) as idx:
        idx.upsert_records([_rec("C:/Vol/new.bin", size=12_345, dev=4, ino=999_999)], scanned_at=2)
    after = service.build_summary(state, require_warm=False).total_indexed_bytes
    assert after == before + 12_345
