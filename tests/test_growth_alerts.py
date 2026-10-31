from __future__ import annotations

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
