from __future__ import annotations

# ruff: noqa: PTH118 -- the fake filesystem keys on plain str paths exactly like the product code
import os
import sqlite3
from pathlib import Path

import pytest

from reclaim import growth
from reclaim.config import Config, load_config
from reclaim.growth import (
    GIB,
    AnomalyConfig,
    Environment,
    GrowthStore,
    KeySeries,
    Measurement,
    Root,
    Snapshot,
    collect_snapshot,
    detect_anomalies,
    growth_report,
    window_growth,
)

DIR = 0x10
REPARSE = 0x400
DAY = 86400.0


class FakeFS:
    """In-memory directory tree behind an injected listing function; records every call."""

    def __init__(self) -> None:
        self.dirs: dict[str, list[growth.Entry]] = {}
        self.calls: list[str] = []

    def add(self, path: str, *entries: growth.Entry) -> None:
        self.dirs[growth.norm(path)] = list(entries)

    def __call__(self, path: str) -> list[growth.Entry]:
        self.calls.append(path)
        try:
            return self.dirs[growth.norm(path)]
        except KeyError:
            raise FileNotFoundError(path) from None


def _env(tmp: Path, **kw: object) -> Environment:
    base: dict[str, object] = {
        "profile": tmp / "home",
        "temp": tmp / "home" / "Temp",
        "volumes": [tmp / "vol"],
        "project_roots": [],
        "cache_paths": [],
        "exclusions": [],
    }
    base.update(kw)
    return Environment(**base)  # type: ignore[arg-type]


def _usage(_: str) -> tuple[int, int, int]:
    return 1000 * GIB, 400 * GIB, 600 * GIB


def _clock_steps(step: float) -> object:
    t = [0.0]

    def clock() -> float:
        t[0] += step
        return t[0]

    return clock


def _by_label(snap: Snapshot) -> dict[str, Measurement]:
    return {m.root.label: m for m in snap.sizes}


def test_snapshot_happy_path_sizes_folders_sysfiles_and_volume(tmp_path: Path) -> None:
    fs = FakeFS()
    home, vol = str(tmp_path / "home"), str(tmp_path / "vol")
    fs.add(home, ("proj", DIR, 0), ("notes.txt", 0, 10))
    fs.add(os.path.join(home, "proj"), ("a.bin", 0, 1000), ("sub", DIR, 0))
    fs.add(os.path.join(home, "proj", "sub"), ("b.bin", 0, 500))
    fs.add(vol, ("pagefile.sys", 0, 7 * GIB), ("Windows", DIR, 0), ("x.txt", 0, 1))
    fs.add(os.path.join(vol, "Windows"), ("w.dll", 0, 42))
    snap = collect_snapshot(_env(tmp_path), list_fn=fs, disk_usage=_usage, now=lambda: 5.0)
    sizes = _by_label(snap)
    assert sizes["proj"].bytes == 1500 and not sizes["proj"].partial
    assert sizes["Windows"].bytes == 42
    assert sizes["pagefile.sys"].bytes == 7 * GIB and sizes["pagefile.sys"].root.kind == "sysfile"
    assert snap.complete and snap.taken_at == 5.0
    volume = next(m for m in snap.sizes if m.root.kind == "volume")
    assert volume.bytes == 400 * GIB


def test_budget_exhaustion_marks_root_partial_lower_bound(tmp_path: Path) -> None:
    fs = FakeFS()
    home = str(tmp_path / "home")
    fs.add(home, ("big", DIR, 0))
    fs.add(os.path.join(home, "big"), ("f", 0, 100), *[(f"d{i}", DIR, 0) for i in range(5)])
    for i in range(5):
        fs.add(os.path.join(home, "big", f"d{i}"), ("f", 0, 1000))
    snap = collect_snapshot(
        _env(tmp_path, volumes=[]),
        list_fn=fs,
        disk_usage=_usage,
        budget_entries=9,  # planning spends 2; the root listing spends 7: nothing left for subdirs
    )
    big = _by_label(snap)["big"]
    assert big.partial and 100 <= big.bytes < 5100
    assert not snap.complete


def test_wall_clock_budget_is_honoured_with_injected_clock(tmp_path: Path) -> None:
    fs = FakeFS()
    home = str(tmp_path / "home")
    fs.add(home, ("a", DIR, 0), ("b", DIR, 0))
    fs.add(os.path.join(home, "a"), ("f", 0, 1))
    fs.add(os.path.join(home, "b"), ("f", 0, 1))
    snap = collect_snapshot(
        _env(tmp_path, volumes=[]),
        list_fn=fs,
        disk_usage=_usage,
        budget_s=3.0,
        clock=_clock_steps(2.0),  # type: ignore[arg-type]
    )
    assert not snap.complete
    assert len([m for m in snap.sizes if m.root.kind == "toplevel"]) < 2


def test_reparse_point_is_never_followed_or_counted(tmp_path: Path) -> None:
    fs = FakeFS()
    home = str(tmp_path / "home")
    fs.add(home, ("proj", DIR, 0))
    fs.add(os.path.join(home, "proj"), ("junction", DIR | REPARSE, 0), ("f", 0, 10))
    fs.add(os.path.join(home, "proj", "junction"), ("huge", 0, 999 * GIB))
    snap = collect_snapshot(_env(tmp_path, volumes=[]), list_fn=fs, disk_usage=_usage)
    assert _by_label(snap)["proj"].bytes == 10
    assert not any("junction" in c for c in fs.calls)


def test_excluded_folder_is_never_listed_or_named(tmp_path: Path) -> None:
    fs = FakeFS()
    root = str(tmp_path / "projects")
    fs.add(root, ("secret-app", DIR, 0), ("ok", DIR, 0))
    fs.add(os.path.join(root, "ok"), ("f", 0, 5))
    fs.add(os.path.join(root, "secret-app"), ("f", 0, 777))
    env = _env(tmp_path, volumes=[], project_roots=[Path(root)], exclusions=["*secret-app*"])

    class StubIndex:
        def subtree_size_bytes(self, path: Path) -> int:
            return 4242 if "secret-app" in str(path) else 0

        def opaque_aggregates(self, *_: object, **__: object) -> list[tuple[str, int, float]]:
            return []

    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage, index=StubIndex())  # type: ignore[arg-type]
    assert not any("secret" in c.lower() for c in fs.calls)
    assert not any("secret" in m.root.path.lower() for m in snap.sizes)
    excluded = _by_label(snap)["Excluded projects"]
    assert excluded.bytes == 4242 and not excluded.partial
    # without an index the excluded share is unknown (a lower bound), never a guess
    snap2 = collect_snapshot(env, list_fn=fs, disk_usage=_usage)
    assert _by_label(snap2)["Excluded projects"].partial


def test_scratch_sessions_are_labelled_and_excluded_projects_skipped(tmp_path: Path) -> None:
    fs = FakeFS()
    temp = tmp_path / "home" / "Temp"
    claude = str(temp / "claude")
    fs.add(claude, ("proj-a", DIR, 0), ("proj-secret", DIR, 0))
    fs.add(os.path.join(claude, "proj-a"), ("sess1", DIR, 0))
    fs.add(os.path.join(claude, "proj-a", "sess1"), ("f", 0, 3 * GIB))
    env = _env(tmp_path, volumes=[], exclusions=["*secret*"], cache_paths=[str(temp)])
    fs.add(str(temp), ("claude", DIR, 0))
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage)
    scratch = [m for m in snap.sizes if m.root.kind == "scratch"]
    assert [(m.root.project, m.root.session, m.bytes) for m in scratch] == [
        ("proj-a", "sess1", 3 * GIB)
    ]
    assert not any("secret" in c for c in fs.calls)


def test_stalest_root_is_refreshed_first_and_nested_roots_listed_once(tmp_path: Path) -> None:
    fs = FakeFS()
    home = str(tmp_path / "home")
    fs.add(home, ("a", DIR, 0), ("b", DIR, 0))
    fs.add(os.path.join(home, "a"), ("f", 0, 1))
    fs.add(os.path.join(home, "b"), ("f", 0, 2))
    seen = {growth.norm(os.path.join(home, "a")): 100.0, growth.norm(os.path.join(home, "b")): 50.0}
    snap = collect_snapshot(
        _env(tmp_path, volumes=[]),
        last_seen=seen,
        list_fn=fs,
        disk_usage=_usage,
        budget_entries=4,  # home listing (3) + exactly one root before exhaustion
    )
    done = [m.root.label for m in snap.sizes if m.root.kind == "toplevel"]
    assert done == ["b"]  # b was measured at t=50, older than a at t=100

    # a cache root nested inside a top-level root is memoised, not listed twice
    fs.calls.clear()
    fs.add(os.path.join(home, "a"), ("deep", DIR, 0))
    fs.add(os.path.join(home, "a", "deep"), ("f", 0, 8))
    env = _env(tmp_path, volumes=[], cache_paths=[os.path.join(home, "a", "deep")])
    collect_snapshot(env, list_fn=fs, disk_usage=_usage)
    assert len([c for c in fs.calls if c.endswith("deep")]) == 1


def test_opaque_roots_come_from_index_with_scan_time(tmp_path: Path) -> None:
    class StubIndex:
        def opaque_aggregates(self, *_: object, **__: object) -> list[tuple[str, int, float]]:
            return [(str(tmp_path / "home" / "p" / "node_modules"), 123, 77.0)]

        def subtree_size_bytes(self, path: Path) -> int:
            return 0

    fs = FakeFS()
    fs.add(str(tmp_path / "home"))
    snap = collect_snapshot(
        _env(tmp_path, volumes=[]),
        list_fn=fs,
        disk_usage=_usage,
        index=StubIndex(),  # type: ignore[arg-type]
    )
    opaque = [m for m in snap.sizes if m.root.kind == "opaque"]
    assert [(m.bytes, m.observed_at) for m in opaque] == [(123, 77.0)]
    assert not any("node_modules" in c for c in fs.calls)  # no listing for the opaque root


# ---------------------------------------------------------------- store and window math


def _snap(t: float, **sizes: tuple[int, bool]) -> Snapshot:
    snap = Snapshot(t, 1.0, True)
    for name, (value, partial) in sizes.items():
        kind = "volume" if name == "VOL" else "toplevel"
        path = "V:\\" if name == "VOL" else f"V:\\{name}"
        snap.sizes.append(Measurement(Root(path, kind, name), value, partial))
    return snap


def test_store_roundtrip_schema_idempotent_and_partial_excluded(tmp_path: Path) -> None:
    db = tmp_path / "growth.sqlite3"
    with GrowthStore(db) as store:
        store.write(_snap(1000.0, a=(10, False)))
        store.write(_snap(2000.0, a=(99, True)))
    with GrowthStore(db) as store:  # reopening re-runs the schema script
        series = store.load_series()
        assert series["V:\\a"].points == [(1000.0, 10)]
        assert store.last_seen()[growth.norm("V:\\a")] == 2000.0  # partial still counts as seen


def test_store_retention_drops_old_snapshots_and_orphan_keys(tmp_path: Path) -> None:
    with GrowthStore(tmp_path / "g.sqlite3") as store:
        store.write(_snap(0.0, old=(1, False)))
        store.write(_snap(401 * DAY, new=(2, False)))
        assert list(store.load_series()) == ["V:\\new"]


def test_store_collapses_repeated_index_observations(tmp_path: Path) -> None:
    with GrowthStore(tmp_path / "g.sqlite3") as store:
        for t in (10.0, 20.0):
            snap = Snapshot(t, 1.0, True)
            snap.sizes.append(Measurement(Root("V:\\nm", "opaque", "nm"), 5, False, 3.0))
            store.write(snap)
        assert store.load_series()["V:\\nm"].points == [(3.0, 5)]


def test_store_size_for_a_simulated_year_is_within_spec_budget(tmp_path: Path) -> None:
    db = tmp_path / "year.sqlite3"
    with GrowthStore(db) as store:
        for i in range(60):  # a sample of 60 snapshots x 250 keys, scaled below
            snap = Snapshot(i * 4 * 3600.0, 1.0, True)
            for k in range(250):
                path = f"C:\\Users\\gaura\\ml-projects\\folder{k}"
                snap.sizes.append(Measurement(Root(path, "toplevel", f"folder{k}"), k * i * 997))
            store.write(snap)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM sizes").fetchone()[0]
    per_row = db.stat().st_size / rows
    year_mb = per_row * 250 * 6 * 365 / 1e6
    assert year_mb <= 15  # the spec's BELIEVED ceiling; the measured value is in the eval report


def test_window_growth_uses_latest_at_or_before_start() -> None:
    pts = [(0.0, 100), (50.0, 130), (100.0, 160), (150.0, 400)]
    assert window_growth(pts, 150.0, 100.0) == (270, (50.0, 130))
    assert window_growth(pts, 150.0, 150.0) == (300, (0.0, 100))
    assert window_growth(pts, 150.0, 400.0) is None  # no sample at/before the window start


def test_growth_report_ranks_and_reports_not_attributed_remainder() -> None:
    def ks(path: str, kind: str, a: int, b: int) -> KeySeries:
        return KeySeries(Root(path, kind, path), [(0.0, a), (100.0, b)])

    series = {
        "V:\\": ks("V:\\", "volume", 1000, 1600),
        "V:\\big": ks("V:\\big", "toplevel", 100, 400),
        "V:\\big\\inner": ks("V:\\big\\inner", "cache", 10, 300),
        "V:\\small": ks("V:\\small", "toplevel", 0, 50),
        "<excluded projects>": ks("<excluded projects>", "excluded", 0, 9999),
    }
    report = growth_report(series, 100.0, 100.0)
    assert [r.path for r in report.rows] == ["V:\\big", "V:\\big\\inner", "V:\\small"]
    vol = report.volumes[0]
    # the nested folder is not double counted: big (300) + small (50) attributed
    assert (vol.delta, vol.attributed, vol.not_attributed) == (600, 350, 250)


# ---------------------------------------------------------------- anomaly detector


def _series(values: list[int], step_h: float = 4.0) -> list[growth.Point]:
    return [(i * step_h * 3600.0, v) for i, v in enumerate(values)]


def _history(n_days: int = 15, step: int = 20 * 1024**2) -> list[int]:
    """6 samples a day with a tiny deterministic wobble (no randomness)."""
    out, v = [], 50 * GIB
    for i in range(n_days * 6):
        v += step + (i % 3) * 1024**2
        out.append(v)
    return out


def _detect(
    keys: dict[str, list[int]], vol: list[int], cfg: AnomalyConfig | None = None
) -> list[growth.Anomaly]:
    n = len(vol)
    now = (n - 1) * 4 * 3600.0
    key_series = {
        k: KeySeries(Root(k, "toplevel", k.split("\\")[-1]), _series(v)) for k, v in keys.items()
    }
    return detect_anomalies(key_series, "C:\\", _series(vol), 1000 * GIB, now, cfg)


def test_spike_is_detected_and_attributed_with_explanation() -> None:
    hist = _history()
    spike = [*hist[:-1], hist[-1] + 20 * GIB]
    vol = [400 * GIB + i * 10**7 for i in range(len(hist) - 1)] + [400 * GIB + 20 * GIB]
    found = _detect({"C:\\scratch": spike, "C:\\quiet": hist}, vol)
    kinds = {a.scope for a in found}
    assert kinds == {"key", "volume"}
    key_event = next(a for a in found if a.scope == "key")
    assert key_event.path == "C:\\scratch" and (key_event.z or 0) >= 4
    vol_event = next(a for a in found if a.scope == "volume")
    assert vol_event.attributed == "C:\\scratch"
    assert "GiB from scratch" in vol_event.explanation


def test_steady_growth_is_not_flagged_by_the_key_rule() -> None:
    steady = _history(step=1 * GIB // 2)  # 3 GiB a day, every day for 15 days
    vol = [400 * GIB + i * 10**6 for i in range(len(steady))]
    found = _detect({"C:\\steady": steady}, vol)
    assert [a for a in found if a.scope == "key"] == []


def test_noise_below_floors_is_not_flagged() -> None:
    wobble = [50 * GIB + (i % 5) * 100 * 1024**2 for i in range(90)]
    wobble[-1] += 1 * GIB  # a jump, but under the 5 GiB floor
    vol = [400 * GIB for _ in range(90)]
    assert _detect({"C:\\n": wobble}, vol) == []


def test_key_rule_needs_history_but_volume_rule_still_names_the_cause() -> None:
    short = [10 * GIB] * 8 + [40 * GIB]
    vol = [100 * GIB] * 8 + [130 * GIB]
    found = _detect({"C:\\new": short}, vol)
    assert [a.scope for a in found] == ["volume"]
    assert found[0].attributed == "C:\\new"


def test_drill_down_prefers_the_specific_folder_over_its_ancestor() -> None:
    rows = [
        growth.GrowthRow("C:\\Users\\g", "toplevel", "g", "", "", 20 * GIB, 0),
        growth.GrowthRow(
            "C:\\Users\\g\\Temp\\claude\\p\\s", "scratch", "p\\s", "p", "s", 19 * GIB, 0
        ),
        growth.GrowthRow("C:\\Users\\g\\other", "toplevel", "other", "", "", 1 * GIB, 0),
    ]
    assert growth.drill_down(rows, rows[0]).label == "p\\s"
    assert [r.label for r in growth.contributors(rows)] == ["p\\s", "other"]


def test_partial_samples_never_become_detector_endpoints(tmp_path: Path) -> None:
    with GrowthStore(tmp_path / "g.sqlite3") as store:
        for i in range(10):
            store.write(_snap(i * 3600.0, a=(10 * GIB, False)))
        store.write(_snap(10 * 3600.0, a=(500 * GIB, True)))  # a cut-off walk: lower bound only
        series = store.load_series()
    report = growth_report(series, 5 * 3600.0, 10 * 3600.0)
    assert report.rows == [] or report.rows[0].growth == 0


def test_snapshot_due_respects_min_interval(tmp_path: Path) -> None:
    with GrowthStore(tmp_path / "g.sqlite3") as store:
        assert growth.snapshot_due(store, 3.0, 1e6)
        store.write(_snap(1e6, a=(1, False)))
        assert not growth.snapshot_due(store, 3.0, 1e6 + 3600)
        assert growth.snapshot_due(store, 3.0, 1e6 + 3 * 3600)


# ---------------------------------------------------------------- config and CLI


def test_growth_config_defaults_and_toml(tmp_path: Path) -> None:
    cfg = Config().growth
    assert (cfg.enabled, cfg.budget_seconds, cfg.budget_entries, cfg.min_interval_hours) == (
        True,
        120.0,
        3_000_000,
        3.0,
    )
    assert (cfg.abs_floor_gib, cfg.share_floor_pct, cfg.project_roots) == (
        5.0,
        2.0,
        ["~/ml-projects"],
    )
    path = tmp_path / "config.toml"
    path.write_text('[growth]\nbudget_seconds = 30\nproject_roots = ["~/x"]\n', encoding="utf-8")
    loaded = load_config(path).growth
    assert loaded.budget_seconds == 30 and loaded.project_roots == ["~/x"]


def test_cli_snapshot_then_report_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from reclaim import cli

    vol = tmp_path / "vol"
    (vol / "data").mkdir(parents=True)
    (vol / "data" / "f.bin").write_bytes(b"x" * 2048)
    db = tmp_path / "out" / "growth.sqlite3"
    cfg = tmp_path / "config.toml"
    cfg.write_text("[growth]\nproject_roots = []\n", encoding="utf-8")
    missing_index = tmp_path / "no-index.sqlite3"
    args = ["growth", "--config", str(cfg), "--db", str(db)]
    code = cli.main([*args, "snapshot", "--volume", str(vol), "--index", str(missing_index)])
    assert code == 0
    assert "reclaim growth snapshot:" in capsys.readouterr().out
    with GrowthStore(db) as store:
        assert any(
            ks.root.label == "data" and ks.points[0][1] == 2048
            for ks in store.load_series().values()
        )
    assert cli.main([*args, "report", "--window", "1d", "--json"]) == 0
    assert '"rows"' in capsys.readouterr().out
    assert cli.main([*args, "report"]) == 0
    assert "reclaim growth report" in capsys.readouterr().out
