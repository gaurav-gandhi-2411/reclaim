"""Unit tests for the leak-verdict logic in packaging/smoke/soak_serve.py (pure functions only;
the Windows sampling / server-launch paths are exercised by the harness's own self-test)."""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "smoke" / "soak_serve.py"
_MB = 1024 * 1024


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("soak_serve", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soak_serve"] = mod
    spec.loader.exec_module(mod)
    return mod


soak = _load()


def _series(
    *,
    base_mb: float = 200.0,
    ramp_mb_per_hour: float = 0.0,
    noise_mb: float = 1.0,
    handle_ramp_per_hour: float = 0.0,
    minutes: int = 120,
    cycle_minutes: int = 10,
    sawtooth_mb: float = 0.0,
) -> list[dict[str, float | str]]:
    """One 'idle' row per minute (plus, with sawtooth, a high 'scan' row at each cycle start)."""
    rng = random.Random(42)  # noqa: S311 -- deterministic synthetic data, not security
    rows: list[dict[str, float | str]] = []
    for m in range(minutes + 1):
        hours = m / 60.0
        cycle = m // cycle_minutes + 1
        priv = base_mb + ramp_mb_per_hour * hours + rng.gauss(0, noise_mb)
        hnd = 300 + handle_ramp_per_hour * hours + rng.gauss(0, 1)
        if sawtooth_mb and m % cycle_minutes == 0:
            rows.append(_row(m, "scan", cycle, priv + sawtooth_mb, hnd))
        rows.append(_row(m, "idle", cycle, priv, hnd))
    return rows


def _row(minute: float, phase: str, cycle: int, priv_mb: float, handles: float) -> dict:
    return {
        "elapsed_s": minute * 60.0,
        "phase": phase,
        "cycle": cycle,
        "rss": priv_mb * _MB,
        "private": priv_mb * _MB,
        "pagefile": priv_mb * _MB,
        "handles": handles,
        "threads": 40,
        "scan_running": 1 if phase == "scan" else 0,
        "procs": 1,
    }


def test_flat_with_noise_passes() -> None:
    result = soak.analyze(_series(noise_mb=1.5))
    assert result["verdict"] == "PASS", result["reasons"]
    assert abs(result["private_slope_mb_per_hour"]) < 5.0


def test_ramp_of_50_mb_per_hour_fails() -> None:
    result = soak.analyze(_series(ramp_mb_per_hour=50.0))
    assert result["verdict"] == "FAIL"
    assert result["private_slope_mb_per_hour"] > 40.0
    assert any("private-bytes slope" in r for r in result["reasons"])


def test_sawtooth_that_returns_each_cycle_passes() -> None:
    rows = _series(sawtooth_mb=300.0, noise_mb=1.0)
    assert any(r["phase"] == "scan" for r in rows)  # the spikes are really in the data
    result = soak.analyze(rows)
    assert result["verdict"] == "PASS", result["reasons"]


def test_slow_ramp_below_threshold_but_monotonic_cycle_floors_fails() -> None:
    # +4 MB/h is under the 5 MB/h slope limit, but steps of +0.7 MB per 10-min cycle floor rise
    # every cycle (well above the 0.5 MB tolerance) -> the monotonic test must catch it.
    rows = _series(ramp_mb_per_hour=4.2, noise_mb=0.0)
    result = soak.analyze(rows)
    assert result["private_slope_mb_per_hour"] < 5.0
    assert result["longest_rising_run_cycles"] >= 5
    assert result["verdict"] == "FAIL"


def test_warmup_is_excluded() -> None:
    # Huge growth in the first 15 min only, flat afterwards: must PASS with the default warm-up
    # and FAIL if the warm-up is disabled.
    rows = []
    for m in range(121):
        mb = 200.0 + (min(m, 15) * 20.0)
        rows.append(_row(m, "idle", m // 10 + 1, mb, 300))
    assert soak.analyze(rows)["verdict"] == "PASS"
    assert soak.analyze(rows, warmup_minutes=0.0)["verdict"] == "FAIL"


def test_handle_growth_detected() -> None:
    result = soak.analyze(_series(handle_ramp_per_hour=200.0))
    assert result["verdict"] == "FAIL"
    assert result["handle_slope_per_hour"] > 100.0
    assert any("handle" in r for r in result["reasons"])


def test_too_little_data_is_inconclusive_not_pass() -> None:
    result = soak.analyze(_series(minutes=20))  # 5 min of post-warm-up data
    assert result["verdict"] == "INCONCLUSIVE"


def test_longest_rising_run_boundaries() -> None:
    assert soak.longest_rising_run([], 0.5) == 0
    assert soak.longest_rising_run([1.0], 0.5) == 1
    assert soak.longest_rising_run([1, 2, 3, 4, 5], 0.5) == 5
    assert soak.longest_rising_run([1, 2, 3, 2, 3, 4], 0.5) == 3
    assert soak.longest_rising_run([1.0, 1.2, 1.4, 1.6], 0.5) == 1  # rises within tolerance


def test_linear_fit_and_bootstrap_are_deterministic() -> None:
    xs = [float(i) for i in range(30)]
    ys = [3.0 * x + 7.0 for x in xs]
    slope, intercept = soak.linear_fit(xs, ys)
    assert abs(slope - 3.0) < 1e-9 and abs(intercept - 7.0) < 1e-9
    assert soak.bootstrap_slope_ci(xs, ys) == soak.bootstrap_slope_ci(xs, ys)


def test_fixture_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    soak.build_fixture(a, files=300, seed=42)
    soak.build_fixture(b, files=300, seed=42)
    listing = lambda root: sorted(  # noqa: E731
        (p.relative_to(root).as_posix(), p.read_bytes()) for p in root.rglob("f*") if p.is_file()
    )
    assert listing(a) == listing(b)
    assert len(listing(a)) == 300


def test_svg_renders_both_series() -> None:
    svg = soak.render_svg(_series(minutes=30), 15.0)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<polyline") == 2
