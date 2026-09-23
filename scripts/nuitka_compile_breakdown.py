from __future__ import annotations

# nuitka_compile_breakdown.py -- per-package C-compile time for a Nuitka build directory.
#
# Why this exists: Nuitka's own `--report` XML records Python-level per-module optimization time,
# not the per-file gcc time where this project's multi-hour builds actually go (scipy/faiss C
# extensions compiled serially under --jobs=1). The only authoritative per-file record is the
# ccache log Nuitka's Scons backend writes into `<name>.build/ccache-<pid>.txt`: every gcc
# invocation is bracketed by a "STARTED"/"DONE" pair with microsecond timestamps and a
# `Result:` line saying whether it was a cache hit. A prior session's "~150 of ~197 min is scipy"
# figure was spot-sampled and never committed; this script replaces that with a reproducible
# measurement that can run after every build (build_installer.ps1 calls it) or against a
# partial build directory from a stopped run.
#
# Two sources, cross-checked:
#   1. ccache log (primary): exact per-invocation start/end, hit vs miss.
#   2. `.o` mtimes (fallback / cross-check): with --jobs=1, consecutive `.o` mtime gaps approximate
#      per-file wall time. Used alone if no ccache log exists.
#
# Usage: python scripts/nuitka_compile_breakdown.py packaging/build/entry_point.build \
#            --out-dir reports/build-timing/<label>
import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LINE_RE = re.compile(r"^\[(\S+)\s+(\d+)\s*\] (.*)$")
_HIT_RESULTS = frozenset({"direct_cache_hit", "preprocessed_cache_hit"})
_PRIMARY_RESULTS = _HIT_RESULTS | {"cache_miss"}
# Dotted-name components that mark test-only code. Used for REPORTING only -- this script never
# decides what gets excluded from a build (see packaging/nofollow_allowlist.txt for that).
_TEST_COMPONENTS = frozenset({"tests", "test", "testing", "conftest"})


@dataclass
class Invocation:
    source: str
    start: datetime
    end: datetime | None = None
    result: str = "unknown"
    gap_before_s: float = 0.0

    @property
    def ccache_s(self) -> float:
        return (self.end - self.start).total_seconds() if self.end else float("nan")


def package_of(c_name: str) -> str:
    """`module.scipy.io.foo.c` -> `scipy`; Nuitka runtime files (`__helpers.c`) -> `<nuitka>`."""
    stem = c_name.removesuffix(".c")
    if not stem.startswith("module."):
        return "<nuitka>"
    parts = stem.split(".")
    return parts[1] if len(parts) > 1 else "<nuitka>"


def is_test_module(c_name: str) -> bool:
    parts = c_name.removesuffix(".c").split(".")[1:]
    return any(p in _TEST_COMPONENTS or p.startswith("test_") for p in parts)


def parse_ccache_log(path: Path) -> tuple[list[Invocation], datetime | None]:
    open_by_pid: dict[str, Invocation] = {}
    done: list[Invocation] = []
    last_ts: datetime | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = _LINE_RE.match(raw.rstrip("\n"))
            if not m:
                continue
            ts, pid, msg = datetime.fromisoformat(m[1]), m[2], m[3]
            last_ts = ts
            if msg.startswith("=== CCACHE") and "STARTED" in msg:
                open_by_pid[pid] = Invocation(source="?", start=ts)
            elif pid not in open_by_pid:
                continue
            elif msg.startswith("Source file: "):
                open_by_pid[pid].source = msg.removeprefix("Source file: ").strip()
            elif msg.startswith("Result: "):
                res = msg.removeprefix("Result: ").strip()
                inv = open_by_pid[pid]
                if res in _PRIMARY_RESULTS and inv.result == "unknown":
                    inv.result = res
            elif msg.startswith("=== CCACHE DONE"):
                inv = open_by_pid.pop(pid)
                inv.end = ts
                done.append(inv)
    # Invocations still open when the log ends were interrupted (stopped build) -- keep them,
    # marked, so an in-flight monster file is not silently dropped from the breakdown.
    for inv in open_by_pid.values():
        if inv.source != "?":
            inv.result = "in_flight_at_stop"
            done.append(inv)
    done = [i for i in done if i.source != "?" and i.source.endswith(".c")]
    done.sort(key=lambda i: i.start)
    prev_end: datetime | None = None
    for inv in done:
        if prev_end is not None:
            inv.gap_before_s = max(0.0, (inv.start - prev_end).total_seconds())
        prev_end = inv.end or prev_end
    return done, last_ts


def o_mtime_gaps(build_dir: Path) -> dict[str, float]:
    objs = sorted(build_dir.glob("*.o"), key=lambda p: p.stat().st_mtime)
    gaps: dict[str, float] = {}
    prev: float | None = None
    for o in objs:
        t = o.stat().st_mtime
        gaps[o.stem + ".c"] = (t - prev) if prev is not None else 0.0
        prev = t
    return gaps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("build_dir", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--stop-time", help="ISO time a stopped build was killed (bounds in-flight)")
    args = ap.parse_args()
    build_dir: Path = args.build_dir
    out: Path = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    logs = sorted(build_dir.glob("ccache-*.txt"))
    invs: list[Invocation] = []
    last_ts: datetime | None = None
    for log in logs:
        got, ts = parse_ccache_log(log)
        invs.extend(got)
        last_ts = max(filter(None, [last_ts, ts]), default=None)
    stop = datetime.fromisoformat(args.stop_time) if args.stop_time else last_ts
    for inv in invs:
        if inv.end is None and stop is not None:
            inv.end = stop
    gaps = o_mtime_gaps(build_dir)
    c_sizes = {p.name: p.stat().st_size for p in build_dir.glob("*.c")}
    compiled = {i.source for i in invs if i.result != "in_flight_at_stop"}

    with (out / "per_file.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "source",
                "package",
                "is_test",
                "result",
                "start",
                "ccache_s",
                "gap_before_s",
                "wall_s",
                "o_mtime_gap_s",
                "c_bytes",
            ]
        )
        for i in invs:
            w.writerow(
                [
                    i.source,
                    package_of(i.source),
                    is_test_module(i.source),
                    i.result,
                    i.start.isoformat(),
                    f"{i.ccache_s:.3f}",
                    f"{i.gap_before_s:.3f}",
                    f"{i.ccache_s + i.gap_before_s:.3f}",
                    f"{gaps.get(i.source, float('nan')):.3f}",
                    c_sizes.get(i.source, 0),
                ]
            )

    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for i in invs:
        a = agg[package_of(i.source)]
        a["files"] += 1
        a["wall_s"] += i.ccache_s + i.gap_before_s
        a["ccache_s"] += i.ccache_s
        a["hits"] += i.result in _HIT_RESULTS
        a["misses"] += i.result == "cache_miss"
        a["in_flight"] += i.result == "in_flight_at_stop"
        a["o_gap_s"] += gaps.get(i.source, 0.0)
        if is_test_module(i.source):
            a["test_files"] += 1
            a["test_wall_s"] += i.ccache_s + i.gap_before_s
    remaining: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, size in c_sizes.items():
        if name not in compiled:
            r = remaining[package_of(name)]
            r[0] += 1
            r[1] += size
    total_wall = sum(a["wall_s"] for a in agg.values()) or 1.0

    lines = [
        "# Nuitka C-compile breakdown",
        "",
        f"Source: `{build_dir}` -- {len(logs)} ccache log(s), {len(invs)} invocations, "
        f"{len(gaps)} `.o` files, {len(c_sizes)} `.c` files.",
        f"First invocation: {invs[0].start.isoformat() if invs else '-'}; "
        f"last log timestamp: {last_ts.isoformat() if last_ts else '-'}.",
        "",
        "`wall_s` = ccache/gcc time + idle gap before the invocation (Scons scanning/scheduling) "
        "-- with --jobs=1 these sum to the C-compile stage wall-clock. `hits` = ccache hits "
        "(object restored, near-zero gcc time).",
        "",
        "| package | files | wall min | % | gcc min | hits | misses | in-flight | test files | "
        "test wall min | .o-gap min | not yet compiled (files / MB .c) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for pkg, a in sorted(agg.items(), key=lambda kv: -kv[1]["wall_s"]):
        r = remaining.get(pkg, [0, 0])
        lines.append(
            f"| {pkg} | {int(a['files'])} | {a['wall_s'] / 60:.1f} | "
            f"{100 * a['wall_s'] / total_wall:.1f} | {a['ccache_s'] / 60:.1f} | {int(a['hits'])} | "
            f"{int(a['misses'])} | {int(a['in_flight'])} | {int(a['test_files'])} | "
            f"{a['test_wall_s'] / 60:.1f} | {a['o_gap_s'] / 60:.1f} | {r[0]} / {r[1] / 1e6:.1f} |"
        )
    for pkg, r in sorted(remaining.items()):
        if pkg not in agg:
            lines.append(
                f"| {pkg} | 0 | 0.0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 0.0 | 0.0 | "
                f"{r[0]} / {r[1] / 1e6:.1f} |"
            )
    lines += [
        "",
        f"Total wall: {total_wall / 60:.1f} min.",
        "",
        "Top 25 files by wall time:",
        "",
        "| source | result | wall s |",
        "|---|---|---:|",
    ]
    for i in sorted(invs, key=lambda i: -(i.ccache_s + i.gap_before_s))[:25]:
        lines.append(f"| {i.source} | {i.result} | {i.ccache_s + i.gap_before_s:.0f} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
