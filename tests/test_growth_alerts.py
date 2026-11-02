from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from reclaim import growth
from reclaim.config import Config, GrowthConfig
from reclaim.growth import Environment, Measurement, Root, Snapshot
from reclaim.growth_alerts import scheduled_snapshot
from reclaim.safety_env import RealProfileAccessError

# Hermetic: every store lives under tmp_path and the sampler is always a fake, so no drive is
# walked and no profile root is read.

NOW = 1_000_000.0


def _config(*, enabled: bool = True) -> Config:
    return Config(growth=GrowthConfig(enabled=enabled))


class _Collector:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    def __call__(self, env: Environment, **kwargs: object) -> Snapshot:
        self.calls.append(kwargs)
        if self.fail:
            raise OSError("simulated listing failure")
        root = Root("C:\\", "volume", "C:\\")
        return Snapshot(NOW, 120.0, True, [Measurement(root, 10)])


def _env(_config: Config) -> Environment:
    return Environment(Path("p"), Path("t"), [], [], [], [])


def _run(config: Config, store: Path, collector: _Collector, now: float = NOW, **kw: object) -> str:
    return scheduled_snapshot(
        config,
        store,
        now=now,
        collect=collector,
        env_factory=_env,
        **kw,  # type: ignore[arg-type]
    )


def test_disabled_does_no_growth_io(tmp_path: Path) -> None:
    store = tmp_path / "data" / "growth.sqlite3"
    collector = _Collector()
    assert _run(_config(enabled=False), store, collector) == "disabled"
    assert collector.calls == []
    assert not store.parent.exists()  # not even the store directory was created


def test_takes_a_snapshot_when_none_exists_with_the_configured_budget(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    collector = _Collector()
    assert _run(_config(), store, collector) == "taken"
    assert collector.calls[0]["budget_s"] == 120.0
    assert collector.calls[0]["budget_entries"] == 3_000_000
    with growth.GrowthStore(store) as s:
        assert s.last_snapshot_time() == NOW


def test_not_due_inside_min_interval_then_due_after(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    collector = _Collector()
    assert _run(_config(), store, collector) == "taken"
    assert _run(_config(), store, collector, now=NOW + 3 * 3600 - 1) == "not_due"
    assert len(collector.calls) == 1
    assert _run(_config(), store, collector, now=NOW + 3 * 3600) == "taken"
    assert len(collector.calls) == 2


def test_a_sampler_failure_is_contained(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    assert _run(_config(), store, _Collector(fail=True)) == "failed"
    with growth.GrowthStore(store) as s:
        assert s.last_snapshot_time() is None  # nothing half-written


def test_an_unusable_store_path_is_contained(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert _run(_config(), blocker / "growth.sqlite3", _Collector()) == "failed"


def test_real_sampler_is_refused_under_pytest(tmp_path: Path) -> None:
    with pytest.raises(RealProfileAccessError):
        scheduled_snapshot(_config(), tmp_path / "growth.sqlite3", now=NOW)


def test_toast_text_on_an_empty_store_file_leaves_it_untouched(tmp_path: Path) -> None:
    # D5, toast side: the text builders open the store read-only too
    from reclaim.growth_alerts import growth_anomaly_text, recent_growth_line

    store = tmp_path / "growth.sqlite3"
    store.write_bytes(b"")
    assert recent_growth_line(_config(), store, NOW, "C:\\") is None
    assert growth_anomaly_text(_config(), store, NOW, "C:\\") is None
    assert store.stat().st_size == 0


# ------------------------------------------------ the scan index is opened read-only


def _no_fs(_path: str) -> list[growth.Entry]:
    raise FileNotFoundError(_path)


def _real_collect(env: Environment, **kwargs: object) -> Snapshot:
    """The real sampler over an empty fake filesystem: only the index can contribute keys."""
    return growth.collect_snapshot(
        env,
        list_fn=_no_fs,
        disk_usage=lambda _p: (1000, 400, 600),
        **kwargs,  # type: ignore[arg-type]
    )


def _index_with_opaque_root(path: Path) -> None:
    from reclaim.index import ScanIndex
    from reclaim.models import FileRecord

    record = FileRecord(
        path=Path("p/proj/node_modules"),
        is_dir=True,
        size_bytes=0,
        attributes=0x10,
        ext="",
        git_repo_root=None,
        git_repo_clean=False,
        mtime=50.0,
        ctime=50.0,
        dev=1,
        ino=0,
        opaque=True,
        agg_files=3,
        agg_bytes=123,
        agg_newest_mtime=50.0,
        agg_physical_bytes=123,
    )
    with ScanIndex(path) as idx:
        idx.upsert_records([record], scanned_at=77.0)


def test_scheduled_snapshot_reads_the_index_without_modifying_it(tmp_path: Path) -> None:
    # D7: the 4-hourly task opened the real index read-write (WAL pragma, schema, checkpoint)
    # only to read the opaque aggregates
    index = tmp_path / "index.sqlite3"
    _index_with_opaque_root(index)
    before = (index.stat().st_size, hashlib.sha256(index.read_bytes()).hexdigest())
    store = tmp_path / "growth.sqlite3"
    status = scheduled_snapshot(
        _config(), store, index_path=index, now=NOW, collect=_real_collect, env_factory=_env
    )
    assert status == "taken"
    assert (index.stat().st_size, hashlib.sha256(index.read_bytes()).hexdigest()) == before
    with growth.GrowthStore(store) as s:
        opaque = [ks for ks in s.load_series().values() if ks.root.kind == "opaque"]
    assert [(ks.root.label, ks.points) for ks in opaque] == [("node_modules", [(77.0, 123)])]


def test_scan_index_read_only_rejects_writes_and_does_not_create_a_file(tmp_path: Path) -> None:
    from reclaim.index import ScanIndex

    index = tmp_path / "index.sqlite3"
    _index_with_opaque_root(index)
    with ScanIndex(index, read_only=True) as idx:
        assert idx.opaque_aggregates(Path("p"), limit=5)[0][1] == 123
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            idx._conn.execute("DELETE FROM files")
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        ScanIndex(missing, read_only=True)
    assert not missing.exists()


@pytest.mark.parametrize("content", [b"", b"not a database " * 40], ids=["empty", "foreign"])
def test_an_unreadable_index_only_costs_the_opaque_keys(tmp_path: Path, content: bytes) -> None:
    index = tmp_path / "index.sqlite3"
    index.write_bytes(content)
    store = tmp_path / "growth.sqlite3"
    status = scheduled_snapshot(
        _config(), store, index_path=index, now=NOW, collect=_real_collect, env_factory=_env
    )
    assert status == "taken"
    assert index.read_bytes() == content
    with growth.GrowthStore(store) as s:
        assert s.last_snapshot_time() is not None  # the snapshot was taken and stored


def test_a_locked_index_skips_opaque_keys_and_keeps_the_snapshot(tmp_path: Path) -> None:
    class Locked:
        def opaque_aggregates(self, *_: object, **__: object) -> list[tuple[str, int, float]]:
            raise sqlite3.OperationalError("database is locked")

        def subtree_size_bytes(self, _path: Path) -> int:
            raise sqlite3.OperationalError("database is locked")

    env = Environment(Path("p"), Path("t"), [], [], [], ["*secret*"])
    snap = growth.collect_snapshot(
        env,
        list_fn=_no_fs,
        disk_usage=lambda _p: (1000, 400, 600),
        index=Locked(),  # type: ignore[arg-type]
    )
    assert not [m for m in snap.sizes if m.root.kind == "opaque"]
    (excluded,) = [m for m in snap.sizes if m.root.kind == "excluded"]
    assert excluded.partial  # unknown, not zero


def test_both_snapshot_entry_points_open_the_index_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim import cli
    from reclaim.index import ScanIndex

    index = tmp_path / "index.sqlite3"
    _index_with_opaque_root(index)
    opened: list[bool] = []

    def spy(path: Path, **kwargs: bool) -> ScanIndex:
        opened.append(bool(kwargs.get("read_only")))
        return ScanIndex(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("reclaim.growth_alerts.ScanIndex", spy)
    scheduled_snapshot(
        _config(),
        tmp_path / "growth.sqlite3",
        index_path=index,
        now=NOW,
        collect=_real_collect,
        env_factory=_env,
    )
    opened_by_cli = cli._open_growth_index(index)
    assert opened_by_cli is not None
    opened_by_cli.close()
    assert cli._open_growth_index(tmp_path / "absent.sqlite3") is None
    assert opened == [True, True]
