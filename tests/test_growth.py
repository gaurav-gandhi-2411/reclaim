from __future__ import annotations

# ruff: noqa: PTH118 -- the fake filesystem keys on plain str paths exactly like the product code
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from reclaim import growth
from reclaim.config import Config, exclusion_patterns, load_config
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


# ---------------------------------------------------------------- exclusions on every root kind


class _ExcludedIndex:
    """Index stub: the excluded folders hold 4242 bytes, nothing is opaque."""

    def subtree_size_bytes(self, path: Path) -> int:
        return 4242 if "secret-app" in str(path) else 0

    def opaque_aggregates(self, *_: object, **__: object) -> list[tuple[str, int, float]]:
        return []


def test_excluded_cache_root_is_never_walked_or_named(tmp_path: Path) -> None:
    # F2b: the cache loop used to add every configured cache without the exclusion check.
    cache = str(tmp_path / "d" / "secret-app" / "hf")
    fs = FakeFS()
    fs.add(cache, ("f", 0, 5))
    env = _env(tmp_path, volumes=[], cache_paths=[cache], exclusions=["*secret-app*"])
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage, index=_ExcludedIndex())  # type: ignore[arg-type]
    assert not any("secret" in m.root.path.lower() for m in snap.sizes)
    assert not any("secret" in c.lower() for c in fs.calls), fs.calls
    # its share is only ever the aggregate line
    assert _by_label(snap)["Excluded projects"].bytes == 4242


def test_excluded_temp_and_project_root_are_never_listed(tmp_path: Path) -> None:
    fs = FakeFS()
    temp = tmp_path / "secret-app" / "Temp"
    fs.add(str(temp / "claude"), ("proj", DIR, 0))
    root = tmp_path / "secret-app-projects"
    fs.add(str(root), ("a", DIR, 0))
    env = _env(
        tmp_path,
        volumes=[],
        temp=temp,
        project_roots=[root],
        cache_paths=[str(temp)],
        exclusions=["*secret-app*"],
    )
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage)
    assert not any("secret" in c.lower() for c in fs.calls), fs.calls
    assert not any("secret" in m.root.path.lower() for m in snap.sizes)


def test_excluded_aggregate_has_the_excluded_kind_and_key(tmp_path: Path) -> None:
    # D4: kills the mutant that changes the aggregate's root kind (it is neither walked nor
    # attributable; `toplevel` would make it a ranked folder named "<excluded projects>").
    env = _env(tmp_path, volumes=[], exclusions=["*secret-app*"])
    fs = FakeFS()
    fs.add(str(tmp_path / "home"))
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage, index=_ExcludedIndex())  # type: ignore[arg-type]
    agg = next(m for m in snap.sizes if m.root.label == "Excluded projects")
    assert (agg.root.kind, agg.root.path) == ("excluded", growth.EXCLUDED_KEY)


def _junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 -- fixed argv, paths under tmp_path
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        check=True,
        capture_output=True,
    )


class _SpyLister:
    """The real lister, recording every directory it is asked to list."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._real = growth.make_default_lister()

    def __call__(self, path: str) -> list[growth.Entry]:
        self.calls.append(path)
        return self._real(path)


@pytest.mark.skipif(os.name != "nt", reason="junctions")
def test_cache_root_that_is_a_junction_into_an_excluded_tree_is_not_walked(tmp_path: Path) -> None:
    # F2c: Walker.total used to enter the root without asking whether the root itself is a
    # junction, so a cache path configured as a link into an excluded project was listed.
    target = tmp_path / "real" / "secret-app"
    target.mkdir(parents=True)
    (target / "big.bin").write_bytes(b"x" * 1000)
    link = tmp_path / "home" / "cachelink"
    _junction(link, target)
    spy = _SpyLister()
    env = _env(tmp_path, volumes=[], cache_paths=[str(link)], exclusions=["*secret-app*"])
    snap = collect_snapshot(env, list_fn=spy, disk_usage=_usage)
    assert "cachelink" not in _by_label(snap)  # absent, not a 0-byte "complete" size
    assert not any("secret-app" in c.lower() for c in spy.calls), spy.calls
    assert not any(m.bytes == 1000 for m in snap.sizes)


@pytest.mark.skipif(os.name != "nt", reason="junctions")
def test_cache_root_that_is_a_junction_to_an_allowed_tree_is_skipped_too(tmp_path: Path) -> None:
    # a reparse point is never followed, root or not (spec 1.2): no row, nothing listed
    target = tmp_path / "real" / "fine"
    target.mkdir(parents=True)
    (target / "big.bin").write_bytes(b"x" * 1000)
    link = tmp_path / "home" / "cachelink"
    _junction(link, target)
    spy = _SpyLister()
    env = _env(tmp_path, volumes=[], cache_paths=[str(link)])
    snap = collect_snapshot(env, list_fn=spy, disk_usage=_usage)
    assert "cachelink" not in _by_label(snap)
    assert not any(c.lower().endswith("fine") for c in spy.calls), spy.calls


@pytest.mark.skipif(os.name != "nt", reason="junctions")
def test_cache_root_below_a_junction_into_an_excluded_tree_is_not_walked(tmp_path: Path) -> None:
    # the root itself is a plain directory, but an ancestor leads into the excluded project
    target = tmp_path / "real" / "secret-app"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "big.bin").write_bytes(b"x" * 1000)
    _junction(tmp_path / "home" / "linkdir", target)
    spy = _SpyLister()
    cache = tmp_path / "home" / "linkdir" / "sub"
    env = _env(tmp_path, volumes=[], cache_paths=[str(cache)], exclusions=["*secret-app*"])
    snap = collect_snapshot(env, list_fn=spy, disk_usage=_usage)
    assert "sub" not in _by_label(snap)
    assert not any("secret-app" in c.lower() for c in spy.calls), spy.calls
    assert not any(c.lower().endswith("sub") for c in spy.calls), spy.calls


class _OpaqueIndex:
    """Index stub with fixed opaque roots; a subtree under `secret-app` holds 9 GiB."""

    def __init__(self, rows: list[tuple[str, int, float]]) -> None:
        self.rows = rows

    def subtree_size_bytes(self, path: Path) -> int:
        return 9 * GIB if "secret-app" in str(path) else 0

    def opaque_aggregates(self, *_: object, **__: object) -> list[tuple[str, int, float]]:
        return self.rows


def test_opaque_root_under_an_excluded_project_folds_into_the_aggregate(tmp_path: Path) -> None:
    # F2a (verifier probe test_opaque_under_excluded_project_not_named): an index row such as
    # ...\secret-app\.venv used to become a named key; it must be only part of the aggregate.
    projects = tmp_path / "home" / "ml-projects"
    bad = str(projects / "secret-app" / ".venv")
    good = str(projects / "fine-app" / "node_modules")
    fs = FakeFS()
    fs.add(str(tmp_path / "home"))
    fs.add(str(projects))  # the project folder is not even listed: only the index knows it
    index = _OpaqueIndex([(bad, 9 * GIB, 7.0), (good, 2 * GIB, 7.0)])
    cfg = Config()
    cfg.exclusions.project_names = ["Secret-App"]
    env = _env(
        tmp_path,
        volumes=[],
        project_roots=[projects],
        exclusions=exclusion_patterns(cfg),
    )
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage, index=index)  # type: ignore[arg-type]
    assert not any("secret" in m.root.path.lower() for m in snap.sizes)
    assert [m.root.path for m in snap.sizes if m.root.kind == "opaque"] == [good]
    assert _by_label(snap)["Excluded projects"].bytes == 9 * GIB


def test_excluded_project_and_its_opaque_root_are_counted_once(tmp_path: Path) -> None:
    projects = tmp_path / "home" / "ml-projects"
    fs = FakeFS()
    fs.add(str(tmp_path / "home"))
    fs.add(str(projects), ("secret-app", DIR, 0))
    index = _OpaqueIndex([(str(projects / "secret-app" / ".venv"), 9 * GIB, 7.0)])
    env = _env(tmp_path, volumes=[], project_roots=[projects], exclusions=["*secret-app*"])
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage, index=index)  # type: ignore[arg-type]
    # the stub answers 9 GiB for the project and for the .venv inside it: one subtree, not two
    assert _by_label(snap)["Excluded projects"].bytes == 9 * GIB


def _write_two_point_series(path: Path, now: float) -> None:
    """C: +20 GiB over the last day, and an opaque root under `secret-app` +12 GiB."""
    bad = "C:\\Users\\g\\ml-projects\\secret-app\\.venv"
    with GrowthStore(path) as store:
        for t, used, opaque in ((now - 86400 - 60, 100, 1), (now - 60, 120, 13)):
            store.write(
                Snapshot(
                    t,
                    120.0,
                    True,
                    [
                        Measurement(Root("C:\\", "volume", "C:\\"), used * GIB),
                        Measurement(Root(bad, "opaque", ".venv"), opaque * GIB, False, t),
                    ],
                )
            )


def test_cli_report_hides_keys_of_a_project_excluded_after_the_snapshots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from reclaim import cli

    db = tmp_path / "growth.sqlite3"
    _write_two_point_series(db, time.time())
    plain = tmp_path / "plain.toml"
    plain.write_text("[growth]\nproject_roots = []\n", encoding="utf-8")
    excluding = tmp_path / "excluding.toml"
    excluding.write_text(
        '[growth]\nproject_roots = []\n[exclusions]\nproject_names = ["secret-app"]\n',
        encoding="utf-8",
    )
    base = ["growth", "--db", str(db), "report", "--window", "1d", "--json"]
    assert cli.main(["growth", "--config", str(plain), *base[1:]]) == 0
    assert "secret-app" in capsys.readouterr().out  # the scenario names it without exclusions
    assert cli.main(["growth", "--config", str(excluding), *base[1:]]) == 0
    out = capsys.readouterr().out
    assert "secret-app" not in out.lower()
    assert '"rows": []' in out


def test_cache_roots_get_the_name_people_know_them_by(tmp_path: Path) -> None:
    # pass 1 labelled every cache with its folder name, so pip's cache read "Cache"
    cfg = Config()
    labels = {
        p: growth.cache_label(p)
        for p in [*cfg.categories.package_caches.paths, *cfg.categories.model_caches.paths]
    }
    assert sorted(set(labels.values())) == sorted(
        [
            "pip cache",
            "npm cache",
            "uv cache",
            "Yarn cache",
            "conda packages",
            "Maven repository",
            "Gradle caches",
            "Hugging Face cache",
            "Hugging Face datasets cache",
            "Hugging Face xet cache",
            "PyTorch hub cache",
            "Ollama models",
        ]
    ), labels
    assert growth.cache_label("C:\\Users\\g\\AppData\\Local\\Docker") == "Docker"
    assert growth.cache_label("C:\\Windows\\Temp") == "Windows Temp"
    temp = tmp_path / "Temp"
    assert growth.cache_label(str(temp), temp) == "Temp"
    assert growth.cache_label("D:\\somewhere\\custom-cache") == "custom-cache"  # unknown: its name
    fs = FakeFS()
    cache = str(tmp_path / "AppData" / "Local" / "pip" / "Cache")
    fs.add(cache, ("f", 0, 5))
    env = _env(tmp_path, volumes=[], cache_paths=[cache])
    snap = collect_snapshot(env, list_fn=fs, disk_usage=_usage)
    assert [m.root.label for m in snap.sizes if m.root.kind == "cache"] == ["pip cache"]


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


def _steady_volume(gib_per_day: float, days: int = 14, surge_gib: int = 0) -> list[growth.Point]:
    """4-hourly volume series growing steadily, a deterministic wobble, optionally a surge of
    `surge_gib` spread over the last 24 h (6 samples)."""
    out, v = [], 400 * GIB
    for i in range(days * 6):
        v += int(gib_per_day * GIB / 6) + (i % 3) * 1024**2
        if surge_gib and i >= days * 6 - 6:
            v += surge_gib * GIB // 6
        out.append((i * 4 * _H, v))
    return out


def _volume_anomalies_at(vol: list[growth.Point], i: int) -> list[growth.Anomaly]:
    return detect_anomalies({}, "C:\\", vol[: i + 1], 1000 * GIB, vol[i][0])


@pytest.mark.parametrize("rate", [2.0, 3.0])
def test_steady_volume_growth_is_not_flagged_once_history_exists(rate: float) -> None:
    vol = _steady_volume(rate)
    # The 72 h window starts 18 samples back; it needs >= 6 intervals before that start.
    flagged = [i for i in range(24, len(vol)) if _volume_anomalies_at(vol, i)]
    assert flagged == []


def test_volume_surge_on_top_of_steady_growth_is_still_flagged() -> None:
    vol = _steady_volume(2.0, surge_gib=20)
    found = _volume_anomalies_at(vol, len(vol) - 1)
    assert [a.scope for a in found] == ["volume"]
    assert (found[0].z or 0) >= 4


def test_volume_rule_without_history_alerts_on_the_floor_alone() -> None:
    # 6 GiB in 24 h; only 5 intervals precede the window start, below min_ref_intervals
    vol = [(i * 4 * _H, 100 * GIB + i * GIB) for i in range(12)]
    found = detect_anomalies({}, "C:\\", vol, 1000 * GIB, vol[-1][0])
    assert [a.scope for a in found] == ["volume"]
    assert found[0].z is None


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


# ---------------------------------------------------------------- window freshness (spec 1.5)

_H = 3600.0


def test_window_growth_start_slack_boundary() -> None:
    now = 150 * _H
    # window 72 h + slack 6 h: the start sample may be at most 78 h old, inclusive
    at_limit = [(now - 78 * _H, 130), (now, 400)]
    assert window_growth(at_limit, now, 72 * _H, max_start_slack_s=6 * _H) == (
        270,
        (now - 78 * _H, 130),
    )
    past_limit = [(now - 78 * _H - 1.0, 130), (now, 400)]
    assert window_growth(past_limit, now, 72 * _H, max_start_slack_s=6 * _H) is None
    assert window_growth(past_limit, now, 72 * _H) is not None  # no limit: the old behaviour


def test_window_growth_end_age_boundary() -> None:
    pts = [(0.0, 100), (100 * _H, 400)]
    assert window_growth(pts, 101 * _H, 100 * _H, max_end_age_s=_H) == (300, (0.0, 100))
    assert window_growth(pts, 101 * _H + 1.0, 100 * _H, max_end_age_s=_H) is None


def test_detector_does_not_report_a_gap_as_growth_in_three_days() -> None:
    # 4-hourly samples for ~5 days, 20 days with the laptop off, then one sample 40 GiB up.
    vol = [(i * 4 * _H, 100 * GIB) for i in range(30)]
    now = vol[-1][0] + 20 * 86400.0
    vol.append((now, 140 * GIB))
    pre_fix = AnomalyConfig(max_start_slack_h=1e9, max_end_age_h=1e9)
    found = detect_anomalies({}, "C:\\", vol, 1000 * GIB, now, pre_fix)
    assert [a.scope for a in found] == ["volume"]  # what the old detector said
    assert detect_anomalies({}, "C:\\", vol, 1000 * GIB, now) == []


def test_detector_volume_rule_start_slack_boundary() -> None:
    def run(extra_s: float) -> list[growth.Anomaly]:
        now = 100 * _H
        start_t = now - 24 * _H - 6 * _H - extra_s
        vol = [(start_t, 100 * GIB), (now - _H, 100 * GIB), (now, 120 * GIB)]
        return detect_anomalies({}, "C:\\", vol, 1000 * GIB, now)

    assert [a.window_h for a in run(0.0)] == [24]
    assert run(1.0) == []


def test_key_rule_needs_a_fresh_newest_sample() -> None:
    hist = _history()
    spike = [*hist[:-1], hist[-1] + 20 * GIB]
    path = "C:\\scratch"
    keys = {path: KeySeries(Root(path, "toplevel", "scratch"), _series(spike))}
    vol = _series([400 * GIB] * len(hist))
    now = vol[-1][0]
    assert any(a.scope == "key" for a in detect_anomalies(keys, "C:\\", vol, 1000 * GIB, now))
    later = now + 2 * _H  # the key's newest sample is 2 h old: not part of a fresh snapshot
    vol2 = [*vol, (later, 400 * GIB)]
    assert detect_anomalies(keys, "C:\\", vol2, 1000 * GIB, later) == []


def test_growth_report_says_insufficient_data_for_a_stale_window() -> None:
    def ks(path: str, kind: str, pts: list[growth.Point]) -> KeySeries:
        return KeySeries(Root(path, kind, path), pts)

    series = {
        "V:\\": ks("V:\\", "volume", [(0.0, 100), (100 * _H, 900)]),
        "V:\\big": ks("V:\\big", "toplevel", [(0.0, 10), (100 * _H, 500)]),
    }
    report = growth_report(series, 24 * _H, 100 * _H)  # the earliest sample is 100 h old
    assert report.rows == []
    assert [(v.volume, v.status) for v in report.volumes] == [("V:\\", "insufficient data")]
    ok = growth_report(series, 100 * _H, 100 * _H)
    assert [(v.status, v.delta) for v in ok.volumes] == [("ok", 800)]


# ---------------------------------------------------------------- config and CLI


def test_view_freshness_default_is_the_cadence_plus_slack_and_alerts_keep_one_hour() -> None:
    cfg = Config().growth
    assert (cfg.view_max_end_age_h, cfg.max_end_age_h) == (6.0, 1.0)


def test_growth_report_carries_the_as_of_time_of_each_volume() -> None:
    now = 10 * DAY
    vol = Root("C:\\", "volume", "C:\\")
    fresh = {vol.path: KeySeries(vol, [(now - DAY - 600, 100), (now - 3 * 3600, 120)])}
    (v,) = growth_report(fresh, DAY, now, 6 * 3600.0, 6 * 3600.0).volumes
    assert (v.status, v.delta, v.as_of) == ("ok", 20, now - 3 * 3600)
    # past the limit: no number, but the newest sample time is still reported
    (v,) = growth_report(fresh, DAY, now, 6 * 3600.0, 1 * 3600.0).volumes
    assert (v.status, v.as_of) == ("insufficient data", now - 3 * 3600)


def test_cli_report_uses_the_view_limit_and_prints_the_as_of_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from reclaim import cli

    now = time.time()
    db = tmp_path / "growth.sqlite3"
    with GrowthStore(db) as store:
        for t, used in ((now - 25 * 3600, 100), (now - 3 * 3600, 120)):  # newest sample: 3 h old
            root = Root("C:\\", "volume", "C:\\")
            store.write(Snapshot(t, 120.0, True, [Measurement(root, used * GIB)]))
    cfg = tmp_path / "config.toml"
    cfg.write_text("[growth]\nproject_roots = []\n", encoding="utf-8")
    assert cli.main(["growth", "--config", str(cfg), "--db", str(db), "report"]) == 0
    out = capsys.readouterr().out
    assert "used 20.0 GiB" in out and "insufficient" not in out
    assert re.search(r"\(as of \d\d:\d\d\)", out), out


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
