"""Soak test for `reclaim serve`: run it for hours under a repeating scan + API workload, sample
the server process tree's memory/handles/threads every minute, and decide whether it leaks.

Why this exists: Windows Error Reporting filed two `RADAR_PRE_LEAK_64` reports against
reclaim.exe (2026-07-25, 2026-08-26) -- a memory-leak flag with no recorded cause (see
docs/crash-inventory-2026-10-01.md). Nothing in the repo measured long-running memory behaviour.

Stdlib-only (ctypes / urllib / csv / json / argparse / subprocess): it is meant to run on a
machine with no dev environment, against the FROZEN exe. Windows-only for sampling (ctypes
kernel32/psapi); the analysis functions are pure and importable anywhere.

Typical use (frozen build, ~2 h, isolated data dir, deterministic 20k-file fixture):

    python packaging\\smoke\\soak_serve.py --exe <path to reclaim.exe> --duration-minutes 120

Everything the run writes (isolated data dir, fixture, server logs, CSV, report, SVG) lives under
`--out-dir` (default: %TEMP%\\reclaim_soak\\<timestamp>). The server is launched with --db /
--vault-dir / --manifest / --mode-log / --first-run-state / --log-path all overridden into that
directory, so the installed app's index and vault are never touched.

Exit codes: 0 PASS, 1 FAIL (leak signal), 2 INCONCLUSIVE (too little post-warm-up data, or the
server died), 3 harness error.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

SEED = 42  # fixed: the fixture tree and the bootstrap must be reproducible run to run
FIXTURE_FILE_COUNT = 20_000
FIXTURE_DIR_COUNT = 200
_CSRF_META_RE = re.compile(r'name="reclaim-csrf-token"\s+content="([^"]+)"')
_HTTP_TIMEOUT_SECONDS = 600.0  # same ceiling as http_probe.py: /api/candidates scales with index
_SCAN_POLL_SECONDS = 1.0
_SCAN_TIMEOUT_SECONDS = 3600.0
_API_GET_ENDPOINTS = (
    "/api/summary",
    "/api/treemap",
    "/api/candidates",
    "/api/clean/one-click-summary",
    "/api/settings/notifications",
    "/api/diagnostics",
)
_REGENERABLE_PATH = "/api/clean/regenerable"

CSV_COLUMNS = (
    "elapsed_s",
    "phase",
    "cycle",
    "rss",
    "private",
    "pagefile",
    "handles",
    "threads",
    "scan_running",
    "procs",
)

# Verdict thresholds. Defaults are justified in SOAK.md; all overridable on the command line.
DEFAULT_WARMUP_MINUTES = 15.0  # allocator/caches/SQLite page cache settle; excluded from fits
DEFAULT_SLOPE_MB_PER_HOUR = 5.0  # over a 2 h run: a 5 MB/h creep is ~10 MB, above sampling noise
DEFAULT_MONOTONIC_CYCLES = 5  # >= 5 consecutive cycles whose post-scan floor rose each time
DEFAULT_RISE_TOLERANCE_MB = 0.5  # a cycle floor must rise by more than this to count as a rise
DEFAULT_HANDLE_SLOPE_PER_HOUR = 25.0  # handles; a real leak (one per request/scan) dwarfs this
DEFAULT_THREAD_SLOPE_PER_HOUR = 5.0  # threads; pools are bounded, so steady growth is a leak
DEFAULT_MIN_SPAN_MINUTES = 30.0  # post-warm-up window needed for a meaningful slope
DEFAULT_MIN_POINTS = 5
_MB = 1024.0 * 1024.0

# --------------------------------------------------------------------------------------------
# Analysis (pure python, no I/O) -- unit-tested in tests/test_soak_analysis.py
# --------------------------------------------------------------------------------------------


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares y = slope * x + intercept. Needs >= 2 distinct x values."""
    n = len(xs)
    if n < 2 or n != len(ys):
        raise ValueError("linear_fit needs >= 2 paired points")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("linear_fit needs >= 2 distinct x values")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    return slope, my - slope * mx


def bootstrap_slope_ci(
    xs: list[float],
    ys: list[float],
    *,
    iterations: int = 1000,
    confidence: float = 0.95,
    seed: int = SEED,
) -> tuple[float, float]:
    """Percentile bootstrap (resampling (x, y) pairs) CI for the OLS slope. Deterministic."""
    rng = random.Random(seed)  # noqa: S311 -- statistics, not security
    n = len(xs)
    slopes: list[float] = []
    for _ in range(iterations):
        idx = [rng.randrange(n) for _ in range(n)]
        bx = [xs[i] for i in idx]
        if max(bx) == min(bx):
            continue
        slopes.append(linear_fit(bx, [ys[i] for i in idx])[0])
    if not slopes:
        raise ValueError("bootstrap produced no valid resamples")
    slopes.sort()
    lo_i = int(((1 - confidence) / 2) * (len(slopes) - 1))
    hi_i = int((1 - (1 - confidence) / 2) * (len(slopes) - 1))
    return slopes[lo_i], slopes[hi_i]


def longest_rising_run(values: list[float], tolerance: float) -> int:
    """Length (in cycles) of the longest chain where each value exceeds its predecessor by more
    than `tolerance`. A chain of 5 values = 5 consecutive cycles with 4 rises. Empty -> 0."""
    best = cur = 1 if values else 0
    for prev, nxt in pairwise(values):
        cur = cur + 1 if nxt - prev > tolerance else 1
        best = max(best, cur)
    return best


def select_post_warmup(
    rows: list[dict[str, float | str]], warmup_s: float, phase: str = "idle"
) -> list[dict[str, float | str]]:
    """Rows in `phase` (post-scan idle by default) taken at or after the warm-up cutoff."""
    return [r for r in rows if r["phase"] == phase and float(r["elapsed_s"]) >= warmup_s]


def cycle_minima(rows: list[dict[str, float | str]], field: str = "private") -> list[float]:
    """Per-cycle minimum of `field` over the given (already filtered) rows, in cycle order."""
    by_cycle: dict[int, float] = {}
    for r in rows:
        c = int(float(r["cycle"]))
        v = float(r[field])
        by_cycle[c] = min(v, by_cycle[c]) if c in by_cycle else v
    return [by_cycle[c] for c in sorted(by_cycle)]


def _slope_per_hour(rows: list[dict[str, float | str]], field: str, scale: float) -> float | None:
    if len(rows) < 2:
        return None
    xs = [float(r["elapsed_s"]) / 3600.0 for r in rows]
    if max(xs) == min(xs):
        return None
    ys = [float(r[field]) / scale for r in rows]
    return linear_fit(xs, ys)[0]


def analyze(
    rows: list[dict[str, float | str]],
    *,
    warmup_minutes: float = DEFAULT_WARMUP_MINUTES,
    slope_threshold_mb_per_hour: float = DEFAULT_SLOPE_MB_PER_HOUR,
    monotonic_cycles: int = DEFAULT_MONOTONIC_CYCLES,
    rise_tolerance_mb: float = DEFAULT_RISE_TOLERANCE_MB,
    handle_slope_per_hour: float = DEFAULT_HANDLE_SLOPE_PER_HOUR,
    thread_slope_per_hour: float = DEFAULT_THREAD_SLOPE_PER_HOUR,
    min_span_minutes: float = DEFAULT_MIN_SPAN_MINUTES,
    min_points: int = DEFAULT_MIN_POINTS,
) -> dict[str, Any]:
    """Leak verdict over sampled rows (dicts with the CSV columns, numeric fields as numbers).

    Uses only post-scan-idle rows (phase == "idle") after the warm-up cutoff, so the within-cycle
    sawtooth of a scan (working set balloons, then the GC/allocator gives it back) does not read
    as growth. Verdict:
      INCONCLUSIVE  fewer than `min_points` rows or a span below `min_span_minutes` after warm-up
      FAIL          private-bytes slope > threshold, OR cycle floors rose for >= monotonic_cycles
                    consecutive cycles, OR handle / thread slope above its threshold
      PASS          otherwise
    """
    warmup_s = warmup_minutes * 60.0
    post = select_post_warmup(rows, warmup_s)
    result: dict[str, Any] = {
        "warmup_minutes": warmup_minutes,
        "n_total": len(rows),
        "n_post_warmup_idle": len(post),
        "thresholds": {
            "private_slope_mb_per_hour": slope_threshold_mb_per_hour,
            "monotonic_cycles": monotonic_cycles,
            "rise_tolerance_mb": rise_tolerance_mb,
            "handle_slope_per_hour": handle_slope_per_hour,
            "thread_slope_per_hour": thread_slope_per_hour,
            "min_span_minutes": min_span_minutes,
            "min_points": min_points,
        },
        "reasons": [],
    }
    span_min = (
        (float(post[-1]["elapsed_s"]) - float(post[0]["elapsed_s"])) / 60.0 if len(post) >= 2 else 0
    )
    result["span_minutes"] = span_min
    if len(post) < min_points or span_min < min_span_minutes:
        result["verdict"] = "INCONCLUSIVE"
        result["reasons"].append(
            f"post-warm-up idle data too thin: {len(post)} points over {span_min:.1f} min "
            f"(need >= {min_points} points and >= {min_span_minutes:g} min)"
        )
        return result

    priv_slope = _slope_per_hour(post, "private", _MB)
    if priv_slope is None:  # unreachable: span > 0 was checked above; fail closed regardless
        result["verdict"] = "INCONCLUSIVE"
        result["reasons"].append("could not fit a slope to the post-warm-up samples")
        return result
    xs = [float(r["elapsed_s"]) / 3600.0 for r in post]
    ys = [float(r["private"]) / _MB for r in post]
    ci_lo, ci_hi = bootstrap_slope_ci(xs, ys)
    minima_mb = [m / _MB for m in cycle_minima(post)]
    run = longest_rising_run(minima_mb, rise_tolerance_mb)
    handle_slope = _slope_per_hour(post, "handles", 1.0)
    thread_slope = _slope_per_hour(post, "threads", 1.0)
    result.update(
        private_slope_mb_per_hour=priv_slope,
        private_slope_ci95_mb_per_hour=[ci_lo, ci_hi],
        cycle_minima_mb=minima_mb,
        longest_rising_run_cycles=run,
        handle_slope_per_hour=handle_slope,
        thread_slope_per_hour=thread_slope,
    )
    reasons: list[str] = result["reasons"]
    if priv_slope > slope_threshold_mb_per_hour:
        reasons.append(
            f"private-bytes slope {priv_slope:.2f} MB/h > {slope_threshold_mb_per_hour:g} MB/h "
            f"(95% bootstrap CI {ci_lo:.2f}..{ci_hi:.2f})"
        )
    if run >= monotonic_cycles:
        reasons.append(
            f"cycle floors rose >{rise_tolerance_mb:g} MB for {run} consecutive cycles "
            f"(limit {monotonic_cycles})"
        )
    if handle_slope is not None and handle_slope > handle_slope_per_hour:
        reasons.append(f"handle count slope {handle_slope:.1f}/h > {handle_slope_per_hour:g}/h")
    if thread_slope is not None and thread_slope > thread_slope_per_hour:
        reasons.append(f"thread count slope {thread_slope:.1f}/h > {thread_slope_per_hour:g}/h")
    result["verdict"] = "FAIL" if reasons else "PASS"
    return result


# --------------------------------------------------------------------------------------------
# Reporting (CSV already written incrementally; SVG and markdown are hand-built strings)
# --------------------------------------------------------------------------------------------


def render_svg(rows: list[dict[str, float | str]], warmup_minutes: float) -> str:
    """Hand-written SVG: private MB (blue, left axis) and handle count (orange, right axis)."""
    w, h, ml, mr, mt, mb = 900, 420, 70, 70, 30, 50
    pw, ph = w - ml - mr, h - mt - mb
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#888"/>',
    ]
    if len(rows) < 2:
        out.append(f'<text x="{ml}" y="{mt + 20}">not enough samples</text></svg>')
        return "\n".join(out)
    ts = [float(r["elapsed_s"]) / 60.0 for r in rows]
    priv = [float(r["private"]) / _MB for r in rows]
    hnd = [float(r["handles"]) for r in rows]
    t0, t1 = min(ts), max(ts)
    t1 = t1 if t1 > t0 else t0 + 1.0

    def bounds(vals: list[float]) -> tuple[float, float]:
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.05 or 1.0
        return lo - pad, hi + pad

    p_lo, p_hi = bounds(priv)
    h_lo, h_hi = bounds(hnd)

    def px(t: float) -> float:
        return ml + (t - t0) / (t1 - t0) * pw

    def py(v: float, lo: float, hi: float) -> float:
        return mt + ph - (v - lo) / (hi - lo) * ph

    wu = warmup_minutes
    if t0 < wu < t1:
        out.append(
            f'<rect x="{ml}" y="{mt}" width="{px(wu) - ml:.1f}" height="{ph}" fill="#eee"/>'
            f'<text x="{ml + 4}" y="{mt + 12}" fill="#666">warm-up (excluded)</text>'
        )
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = mt + ph - frac * ph
        out.append(f'<line x1="{ml}" y1="{gy:.1f}" x2="{ml + pw}" y2="{gy:.1f}" stroke="#ddd"/>')
        out.append(
            f'<text x="{ml - 6}" y="{gy + 4:.1f}" text-anchor="end" fill="#1f77b4">'
            f"{p_lo + frac * (p_hi - p_lo):.0f}</text>"
        )
        out.append(
            f'<text x="{ml + pw + 6}" y="{gy + 4:.1f}" fill="#ff7f0e">'
            f"{h_lo + frac * (h_hi - h_lo):.0f}</text>"
        )
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gx = ml + frac * pw
        out.append(
            f'<text x="{gx:.1f}" y="{mt + ph + 16}" text-anchor="middle">'
            f"{t0 + frac * (t1 - t0):.0f}</text>"
        )
    pp = " ".join(f"{px(t):.1f},{py(v, p_lo, p_hi):.1f}" for t, v in zip(ts, priv, strict=True))
    hp = " ".join(f"{px(t):.1f},{py(v, h_lo, h_hi):.1f}" for t, v in zip(ts, hnd, strict=True))
    out.append(f'<polyline fill="none" stroke="#1f77b4" stroke-width="1.5" points="{pp}"/>')
    out.append(f'<polyline fill="none" stroke="#ff7f0e" stroke-width="1.5" points="{hp}"/>')
    out.append(
        f'<text x="{ml + pw / 2}" y="{h - 10}" text-anchor="middle">elapsed (minutes)</text>'
        f'<text x="{ml}" y="{mt - 10}" fill="#1f77b4">private bytes (MB, process tree)</text>'
        f'<text x="{ml + pw}" y="{mt - 10}" text-anchor="end" fill="#ff7f0e">'
        "handle count (right axis)</text>"
    )
    out.append("</svg>")
    return "\n".join(out)


def render_report(
    analysis: dict[str, Any], meta: dict[str, Any], api_stats: dict[str, dict[str, int]]
) -> str:
    """Markdown report: verdict first, then the numbers behind it."""
    a = analysis
    lines = [
        "# reclaim serve soak report",
        "",
        f"**Verdict: {a['verdict']}**",
        "",
    ]
    for reason in a["reasons"]:
        lines.append(f"- {reason}")
    t = a["thresholds"]
    lines += [
        "",
        "## Run",
        "",
        f"- target: `{meta.get('target')}`",
        f"- scan root: `{meta.get('scan_root')}`",
        f"- duration: {meta.get('elapsed_minutes', 0):.1f} min "
        f"(requested {meta.get('duration_minutes')}), sample every {meta.get('sample_seconds')} s, "
        f"cycle every {meta.get('cycle_minutes')} min, "
        f"{meta.get('cycles_started', 0)} cycles started",
        f"- samples: {a['n_total']} total, {a['n_post_warmup_idle']} post-scan-idle after the "
        f"{a['warmup_minutes']:g} min warm-up (span {a['span_minutes']:.1f} min)",
        f"- server exit during run: {meta.get('server_exited_early')}",
        f"- finished: {meta.get('finished')}",
        "",
        "## Leak analysis (post-scan idle samples after warm-up)",
        "",
    ]
    if "private_slope_mb_per_hour" in a:
        lo, hi = a["private_slope_ci95_mb_per_hour"]
        lines += [
            "| metric | measured | threshold |",
            "|---|---|---|",
            f"| private bytes slope | {a['private_slope_mb_per_hour']:.2f} MB/h "
            f"(95% bootstrap CI {lo:.2f}..{hi:.2f}) | > {t['private_slope_mb_per_hour']:g} fails |",
            f"| longest rising cycle-floor run | {a['longest_rising_run_cycles']} cycles | "
            f">= {t['monotonic_cycles']} fails (rise > {t['rise_tolerance_mb']:g} MB) |",
            f"| handle slope | {a['handle_slope_per_hour']:.1f} /h | "
            f"> {t['handle_slope_per_hour']:g} fails |",
            f"| thread slope | {a['thread_slope_per_hour']:.1f} /h | "
            f"> {t['thread_slope_per_hour']:g} fails |",
            "",
            "Cycle floors (min private MB per post-warm-up cycle): "
            + ", ".join(f"{m:.1f}" for m in a["cycle_minima_mb"]),
        ]
    else:
        lines.append("Not computed (inconclusive: see reasons above).")
    lines += [
        "",
        "## API traffic",
        "",
        "| endpoint | ok | non-200 / error | skipped |",
        "|---|---|---|---|",
    ]
    for ep, st in sorted(api_stats.items()):
        lines.append(
            f"| `{ep}` | {st.get('ok', 0)} | {st.get('err', 0)} | {st.get('skipped', 0)} |"
        )
    lines += [
        "",
        "Files: `soak_samples.csv` (raw samples), `memory_curve.svg` (private MB + handles).",
        "",
        "Caveat: a PASS means no growth was detected at this sensitivity over this window and "
        "workload; it is not proof of absence (see SOAK.md).",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Windows process sampling (ctypes)
# --------------------------------------------------------------------------------------------

_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x1
_SYNCHRONIZE = 0x100000
_WAIT_TIMEOUT = 0x102


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = (
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
        ("PrivateUsage", ctypes.c_size_t),
    )


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


_kernel32: Any = None


def _k32() -> Any:
    """Lazily bind kernel32 with explicit 64-bit-safe prototypes (import stays portable)."""
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        k.Process32NextW.restype = wintypes.BOOL
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        k.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetProcessHandleCount.restype = wintypes.BOOL
        k.K32GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            wintypes.DWORD,
        )
        k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.TerminateProcess.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        _kernel32 = k
    return _kernel32


def _process_table() -> dict[int, tuple[int, int]]:
    """pid -> (parent pid, thread count) for every process, from one Toolhelp snapshot."""
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    table: dict[int, tuple[int, int]] = {}
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError("CreateToolhelp32Snapshot failed")
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.cntThreads)
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return table


def process_tree(root_pid: int, table: dict[int, tuple[int, int]] | None = None) -> list[int]:
    """root_pid plus all live descendants (visited-set guards against pid-reuse cycles)."""
    table = table if table is not None else _process_table()
    if root_pid not in table:
        return []
    tree, seen, frontier = [root_pid], {root_pid}, [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _threads) in table.items():
            if ppid == parent and pid not in seen:
                seen.add(pid)
                tree.append(pid)
                frontier.append(pid)
    return tree


def sample_tree(root_pid: int) -> dict[str, int] | None:
    """Summed memory/handle/thread counters over the process tree; None if the root is gone."""
    k = _k32()
    table = _process_table()
    pids = process_tree(root_pid, table)
    if not pids:
        return None
    totals = {"rss": 0, "private": 0, "pagefile": 0, "handles": 0, "threads": 0, "procs": 0}
    for pid in pids:
        handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue  # exited between snapshot and open, or protected child
        try:
            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            if not k.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                continue
            count = wintypes.DWORD(0)
            k.GetProcessHandleCount(handle, ctypes.byref(count))
            totals["rss"] += counters.WorkingSetSize
            totals["private"] += counters.PrivateUsage
            totals["pagefile"] += counters.PagefileUsage
            totals["handles"] += count.value
            totals["threads"] += table[pid][1]
            totals["procs"] += 1
        finally:
            k.CloseHandle(handle)
    return totals if totals["procs"] else None


def _pid_alive(pid: int) -> bool:
    k = _k32()
    handle = k.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return bool(k.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
    finally:
        k.CloseHandle(handle)


def kill_tree(root_pid: int) -> list[int]:
    """Terminate descendants (leaves first) then the root; returns pids still alive after."""
    k = _k32()
    try:
        pids = process_tree(root_pid)
    except OSError:
        pids = [root_pid]
    for pid in reversed(pids):
        handle = k.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
        if handle:
            try:
                k.TerminateProcess(handle, 1)
                k.WaitForSingleObject(handle, 10_000)
            finally:
                k.CloseHandle(handle)
    return [p for p in pids if _pid_alive(p)]


# --------------------------------------------------------------------------------------------
# Fixture, HTTP client, workload
# --------------------------------------------------------------------------------------------

_EXTENSIONS = ("txt", "log", "tmp", "json", "bin", "cache", "dat", "md")


def build_fixture(root: Path, *, files: int = FIXTURE_FILE_COUNT, seed: int = SEED) -> int:
    """Deterministic tree of `files` small files (64 B..4 KB) in FIXTURE_DIR_COUNT directories.
    ~10% reuse a content blob from a small pool so duplicate detection has real work. Same seed
    -> byte-identical tree. Idempotent: a matching marker file short-circuits regeneration."""
    marker = root / ".fixture_ok"
    expected = f"{files},{seed}"
    if marker.exists() and marker.read_text(encoding="utf-8") == expected:
        return files
    if root.exists():
        shutil.rmtree(root)
    rng = random.Random(seed)  # noqa: S311 -- deterministic fixture, not security
    pool = [rng.randbytes(rng.randint(512, 4096)) for _ in range(50)]
    per_dir = max(1, files // FIXTURE_DIR_COUNT)
    for i in range(files):
        d = root / f"d{(i // per_dir) % FIXTURE_DIR_COUNT:03d}"
        d.mkdir(parents=True, exist_ok=True)
        data = rng.choice(pool) if rng.random() < 0.10 else rng.randbytes(rng.randint(64, 4096))
        (d / f"f{i:05d}.{rng.choice(_EXTENSIONS)}").write_bytes(data)
    marker.write_text(expected, encoding="utf-8")
    return files


class Client:
    """Minimal HTTP client with the CSRF handshake (same contract as http_probe.Client)."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.csrf: str | None = None

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, str]:
        headers: dict[str, str] = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET" and self.csrf:
            headers["x-reclaim-csrf-token"] = self.csrf
        # base is always the loopback server this harness launched/was pointed at by the operator
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def fetch_csrf(self) -> bool:
        status, html = self.request("GET", "/")
        match = _CSRF_META_RE.search(html) if status == 200 else None
        if match:
            self.csrf = match.group(1)
        return match is not None


class Soak:
    """Shared state between the sampler thread and the workload loop."""

    def __init__(self, args: argparse.Namespace, root_pid: int, out_dir: Path) -> None:
        self.args = args
        self.root_pid = root_pid
        self.csv_path = out_dir / "soak_samples.csv"
        self.start = time.monotonic()
        self.phase = "baseline"
        self.cycle = 0
        self.scan_running = 0
        self.rows: list[dict[str, float | str]] = []
        self.api_stats: dict[str, dict[str, int]] = {}
        self.server_exited = threading.Event()
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._fh = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(CSV_COLUMNS)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def sample_now(self, phase: str | None = None) -> None:
        with self._lock:
            counters = sample_tree(self.root_pid)
            if counters is None:
                self.server_exited.set()
                return
            row: dict[str, float | str] = {
                "elapsed_s": round(self.elapsed(), 1),
                "phase": phase or self.phase,
                "cycle": self.cycle,
                "scan_running": self.scan_running,
                **counters,
            }
            self.rows.append(row)
            self._writer.writerow([row[c] for c in CSV_COLUMNS])
            self._fh.flush()

    def sampler(self) -> None:
        while not self.stop.wait(self.args.sample_seconds):
            self.sample_now()

    def stat(self, endpoint: str, key: str) -> None:
        self.api_stats.setdefault(endpoint, {})[key] = (
            self.api_stats.get(endpoint, {}).get(key, 0) + 1
        )

    def close(self) -> None:
        self._fh.close()


def _interruptible_sleep(soak: Soak, seconds: float) -> None:
    """Sleep up to `seconds`, waking early on stop or server death."""
    deadline = time.monotonic() + seconds
    while (
        time.monotonic() < deadline and not soak.stop.is_set() and not soak.server_exited.is_set()
    ):
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def run_scan(soak: Soak, client: Client, scan_root: str) -> bool:
    """POST /api/scan (minting the outside-home token first if the server demands one), then wait
    for a terminal status. Returns True on 'completed'."""
    status, body = client.request("POST", "/api/scan", {"path": scan_root})
    if status == 403:  # outside home: single-use confirmation token (routes.start_scan)
        s2, b2 = client.request("POST", "/api/scan/full-drive/confirm-intent", {})
        if s2 == 200:
            status, body = client.request(
                "POST", "/api/scan", {"path": scan_root, "token": json.loads(b2)["token"]}
            )
    if status != 202:
        soak.stat("POST /api/scan", "err")
        print(f"[soak] scan start failed: HTTP {status} {body[:200]}", file=sys.stderr)  # noqa: T201
        return False
    soak.stat("POST /api/scan", "ok")
    soak.scan_running = 1
    soak.phase = "scan"
    deadline = time.monotonic() + _SCAN_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and not soak.stop.is_set():
            s, b = client.request("GET", "/api/scan/status")
            if s == 200 and json.loads(b).get("status") in ("completed", "failed", "cancelled"):
                soak.stat("GET /api/scan/status", "ok")
                return bool(json.loads(b).get("status") == "completed")
            if s != 200:
                soak.stat("GET /api/scan/status", "err")
            if soak.server_exited.is_set():
                return False
            time.sleep(_SCAN_POLL_SECONDS)
        return False
    finally:
        soak.scan_running = 0


def api_round(soak: Soak, client: Client, *, regenerable: bool) -> None:
    for path in _API_GET_ENDPOINTS:
        if soak.stop.is_set() or soak.server_exited.is_set():
            return
        try:
            status, _ = client.request("GET", path)
        except OSError:
            soak.stat(f"GET {path}", "err")
            continue
        soak.stat(f"GET {path}", "ok" if status == 200 else "err")
    if regenerable and not soak.stop.is_set():
        key = f"POST {_REGENERABLE_PATH}"
        if soak.api_stats.get(key, {}).get("skipped"):
            return
        try:
            status, _ = client.request("POST", _REGENERABLE_PATH, {"apply": False})
        except OSError:
            soak.stat(key, "err")
            return
        if status == 404:  # endpoint only exists on builds with the regenerable-safe tier
            soak.stat(key, "skipped")
        else:
            soak.stat(key, "ok" if status == 200 else "err")


def workload(soak: Soak, client: Client, scan_root: str) -> None:
    """Cycle loop: scan -> API traffic -> settled idle samples -> wait for the next cycle."""
    args = soak.args
    cycle_s = args.cycle_minutes * 60.0
    duration_s = args.duration_minutes * 60.0
    soak.sample_now("baseline")
    cycle_start = soak.elapsed()
    while not soak.stop.is_set() and not soak.server_exited.is_set():
        soak.cycle += 1
        run_scan(soak, client, scan_root)
        soak.phase = "api"
        for _ in range(args.api_rounds):
            api_round(soak, client, regenerable=args.include_regenerable_preview)
        soak.phase = "idle"
        _interruptible_sleep(soak, min(5.0, args.sample_seconds))  # let the allocator settle
        soak.sample_now("idle")  # guarantees >= 1 post-scan idle point per cycle
        cycle_start += cycle_s
        wait = min(cycle_start, duration_s) - soak.elapsed()
        _interruptible_sleep(soak, wait)
        if soak.elapsed() >= duration_s:
            break
    soak.stop.set()


# --------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http(base: str, seconds: float, proc: subprocess.Popen[bytes] | None = None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # launched server already exited: no point waiting out the timeout
        try:
            if Client(base).request("GET", "/")[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--exe", help="Path to reclaim.exe (frozen) to launch as `<exe> serve ...`.")
    p.add_argument(
        "--exe-args",
        default="",
        help='Extra args placed before `serve`, e.g. "-m reclaim" when --exe is python.exe '
        "(dev-server self-test).",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra environment variable for the launched server (repeatable).",
    )
    p.add_argument("--pid", type=int, help="Attach: sample this existing server process tree.")
    p.add_argument("--url", help="Attach: base URL of the existing server (needs --pid).")
    p.add_argument("--scan-root", help="Directory to scan (default: generated 20k-file fixture).")
    p.add_argument(
        "--out-dir", type=Path, help="Output dir (default: %%TEMP%%\\reclaim_soak\\<ts>)."
    )
    p.add_argument("--port", type=int, help="Server port (default: a free loopback port).")
    p.add_argument("--duration-minutes", type=float, default=120.0)
    p.add_argument("--sample-seconds", type=float, default=60.0)
    p.add_argument("--cycle-minutes", type=float, default=10.0)
    p.add_argument("--api-rounds", type=int, default=5, help="GET rounds per cycle (default 5).")
    p.add_argument("--include-regenerable-preview", action="store_true")
    p.add_argument("--warmup-minutes", type=float, default=DEFAULT_WARMUP_MINUTES)
    p.add_argument("--slope-threshold-mb-per-hour", type=float, default=DEFAULT_SLOPE_MB_PER_HOUR)
    p.add_argument("--monotonic-cycles", type=int, default=DEFAULT_MONOTONIC_CYCLES)
    p.add_argument("--rise-tolerance-mb", type=float, default=DEFAULT_RISE_TOLERANCE_MB)
    p.add_argument("--handle-slope-per-hour", type=float, default=DEFAULT_HANDLE_SLOPE_PER_HOUR)
    p.add_argument("--thread-slope-per-hour", type=float, default=DEFAULT_THREAD_SLOPE_PER_HOUR)
    p.add_argument("--min-span-minutes", type=float, default=DEFAULT_MIN_SPAN_MINUTES)
    args = p.parse_args(argv)
    if bool(args.exe) == bool(args.pid):
        p.error("give exactly one of --exe (launch) or --pid with --url (attach)")
    if args.pid and not args.url:
        p.error("--pid requires --url")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if sys.platform != "win32":
        print("soak_serve.py samples via kernel32/psapi: Windows only", file=sys.stderr)  # noqa: T201
        return 3
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # noqa: DTZ005 -- local label only
    out_dir = args.out_dir or Path(tempfile.gettempdir()) / "reclaim_soak" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    data = out_dir / "data"
    (data / "quarantine").mkdir(parents=True, exist_ok=True)
    (data / "logs").mkdir(parents=True, exist_ok=True)

    scan_root = args.scan_root
    if not scan_root:
        scan_root = str(out_dir / "fixture")
        print(f"[soak] building fixture ({FIXTURE_FILE_COUNT} files, seed {SEED})...")  # noqa: T201
        build_fixture(Path(scan_root))
    elif not Path(scan_root).is_dir():
        print(f"[soak] --scan-root is not a directory: {scan_root}", file=sys.stderr)  # noqa: T201
        return 3

    proc: subprocess.Popen[bytes] | None = None
    logs: list[Any] = []
    if args.exe:
        port = args.port or _free_port()
        base = f"http://127.0.0.1:{port}"
        cmd = [args.exe, *args.exe_args.split(), "serve", "--port", str(port)]
        cmd += [
            "--db", str(data / "reclaim_index.sqlite3"),
            "--vault-dir", str(data / "quarantine"),
            "--manifest", str(data / "quarantine" / "manifest.jsonl"),
            "--mode-log", str(data / "mode_log.jsonl"),
            "--first-run-state", str(data / "first_run_state.json"),
            "--log-path", str(data / "logs" / "reclaim.log"),
        ]  # fmt: skip
        # Files, not pipes: an undrained pipe would block a chatty server during a multi-hour run.
        logs = [
            (out_dir / "server_stdout.log").open("wb"),
            (out_dir / "server_stderr.log").open("wb"),
        ]
        env = dict(os.environ)
        env.update(kv.split("=", 1) for kv in args.env)
        proc = subprocess.Popen(cmd, stdout=logs[0], stderr=logs[1], cwd=out_dir, env=env)  # noqa: S603
        root_pid = proc.pid
    else:
        base, root_pid = args.url, args.pid

    soak: Soak | None = None
    finished = "no"
    started = time.monotonic()
    try:
        if not _wait_http(base, 60.0, proc):
            print(f"[soak] server did not answer on {base} within 60 s", file=sys.stderr)  # noqa: T201
            return 3
        client = Client(base)
        if not client.fetch_csrf():
            print("[soak] could not scrape CSRF token from GET /", file=sys.stderr)  # noqa: T201
            return 3
        soak = Soak(args, root_pid, out_dir)
        print(f"[soak] serving at {base}, pid {root_pid}, out {out_dir}")  # noqa: T201
        sampler = threading.Thread(target=soak.sampler, daemon=True)
        sampler.start()
        worker = threading.Thread(target=workload, args=(soak, client, scan_root), daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                worker.join(1.0)
                if soak.server_exited.is_set():
                    soak.stop.set()
        except KeyboardInterrupt:
            print("[soak] Ctrl-C: stopping and cleaning up", file=sys.stderr)  # noqa: T201
            soak.stop.set()
            finished = "interrupted"
        soak.stop.set()
        worker.join(30.0)
        sampler.join(args.sample_seconds + 5)
        if finished == "no":
            finished = "server died" if soak.server_exited.is_set() else "yes"
    finally:
        leftover: list[int] = []
        if proc is not None:
            leftover = kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                leftover = leftover or [proc.pid]
        for fh in logs:
            fh.close()
        if soak is not None:
            soak.close()
        if leftover:
            print(f"[soak] WARNING: processes still alive after kill: {leftover}", file=sys.stderr)  # noqa: T201

    if soak is None:  # unreachable: every path that skips Soak() returned above
        return 3
    analysis = analyze(
        soak.rows,
        warmup_minutes=args.warmup_minutes,
        slope_threshold_mb_per_hour=args.slope_threshold_mb_per_hour,
        monotonic_cycles=args.monotonic_cycles,
        rise_tolerance_mb=args.rise_tolerance_mb,
        handle_slope_per_hour=args.handle_slope_per_hour,
        thread_slope_per_hour=args.thread_slope_per_hour,
        min_span_minutes=args.min_span_minutes,
    )
    if finished == "server died":
        analysis["reasons"].append("server process exited during the run")
        analysis["verdict"] = "FAIL" if analysis["verdict"] == "FAIL" else "INCONCLUSIVE"
    meta = {
        "target": args.exe or f"{args.url} (pid {args.pid})",
        "scan_root": scan_root,
        "elapsed_minutes": (time.monotonic() - started) / 60.0,
        "duration_minutes": args.duration_minutes,
        "sample_seconds": args.sample_seconds,
        "cycle_minutes": args.cycle_minutes,
        "cycles_started": soak.cycle,
        "server_exited_early": finished == "server died",
        "finished": finished,
    }
    (out_dir / "soak_report.md").write_text(
        render_report(analysis, meta, soak.api_stats), encoding="utf-8"
    )
    (out_dir / "memory_curve.svg").write_text(
        render_svg(soak.rows, args.warmup_minutes), encoding="utf-8"
    )
    (out_dir / "soak_analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    print(f"[soak] verdict: {analysis['verdict']}  (report: {out_dir / 'soak_report.md'})")  # noqa: T201
    for reason in analysis["reasons"]:
        print(f"[soak]   - {reason}")  # noqa: T201
    return {"PASS": 0, "FAIL": 1}.get(analysis["verdict"], 2)


if __name__ == "__main__":
    # Ensure a stray Ctrl-C during interpreter shutdown never leaves the harness half-reporting.
    sys.exit(main())
