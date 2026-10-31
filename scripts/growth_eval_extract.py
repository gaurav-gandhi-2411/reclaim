from __future__ import annotations

# Builds the committed fixtures of the disk-growth eval (evals/growth/) from the READ-ONLY build
# logs this engagement left in D:\reclaim-build. The sources are only ever opened for reading
# (and hashed); the only disk call besides that is shutil.disk_usage() for the two volume totals.
# Re-running it on the same sources reproduces the fixtures byte for byte.
#
# Usage: python scripts/growth_eval_extract.py [--build-dir D:\reclaim-build] [--out DIR]
import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

GIB = 2**30
# Timestamps are the machine's local wall clock (IST) with no offset: every source was written by
# this one machine, and the jobs' "+05:30" suffix is stripped to match the others.
TS_FMT = "%Y-%m-%dT%H:%M:%S"
# The feature collapses excluded projects into one aggregate and never names them; the fixtures do
# the same, with the same name patterns (`*name*`) the configured exclusions use.
EXCLUDED_NAMES = ("fr-en-transformer", "shipdoc-extract", "intent-router")
EXCLUDED_KEY = "Excluded projects"
FIXTURE_MAX_BYTES = 300_000
# The detector thins samples below 30 min apart anyway; dense logs are thinned here so the fixtures
# stay small. The census and the folder-size rows are never thinned.
VOLUME_GAP_S = {"attrib": 60, "watchdog": 60, "jobs": 300}
# attrib file -> calendar date of its HH:MM:SS timestamps (confirmed against file mtime below).
ATTRIB_DATES = {
    "c_attrib6.csv": "2026-10-13",
    "c_attrib7.csv": "2026-10-15",
    "c_attrib8.csv": "2026-10-15",
}
CENSUS_FILES = ("c_census.csv", "c_census_20261012.csv")
DROPPED_EXTRA = {"swap_used"}  # a memory figure, not a folder

Row = tuple[str, str, int]  # (iso timestamp, key, bytes)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime(TS_FMT)  # noqa: DTZ006 -- local wall clock


def _secs(ts: str) -> float:
    return datetime.strptime(ts, TS_FMT).timestamp()  # noqa: DTZ007 -- local wall clock


def thin(rows: list[Row], gap_s: float) -> list[Row]:
    """Keeps the first row of each key and every later row at least `gap_s` after the last kept."""
    last: dict[str, float] = {}
    out: list[Row] = []
    for ts, key, size in sorted(rows):
        t = _secs(ts)
        if key not in last or t - last[key] >= gap_s:
            last[key] = t
            out.append((ts, key, size))
    return out


def volume_rows(ts: str, c_free: int, d_free: int, totals: dict[str, int]) -> list[Row]:
    return [
        (ts, "volume:C:used", totals["C"] - c_free),
        (ts, "volume:D:used", totals["D"] - d_free),
    ]


def read_attrib(path: Path, date: str, totals: dict[str, int]) -> tuple[list[Row], list[Row]]:
    """`(volume rows, folder rows)` of one C: attribution window. Process columns are dropped."""
    vols: list[Row] = []
    folders: list[Row] = []
    with path.open(newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["c_free"].isdigit()]  # skips the END row
    # The date is not in the file: it must agree with the file's own last-write time.
    last_ts = f"{date}T{rows[-1]['ts']}"
    gap = _secs(mtime_iso(path)) - _secs(last_ts)
    if not -5 <= gap <= 600:
        raise SystemExit(
            f"{path.name}: mtime is {gap:.0f}s from the last row on {date}: wrong date"
        )
    for r in rows:
        ts = f"{date}T{r['ts']}"
        vols += volume_rows(ts, int(r["c_free"]), int(r["d_free"]), totals)
        for item in filter(None, r["extra"].split("|")):
            name, _, value = item.partition("=")
            if name in DROPPED_EXTRA or not value.endswith("G"):
                continue
            if name == "pagefile.sys" or name == "swapfile.sys":
                key = f"sys:{name}"
            elif name.startswith("T:"):
                key = f"temp:{name[2:]}"
            else:
                key = f"attrib:{name}"
            folders.append((ts, key, round(float(value[:-1]) * GIB)))
    return vols, folders


def collapse_excluded(census: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Drops every row whose path names an excluded project and adds their top-most rows' sum as
    one `Excluded projects` row (ancestors that merely contain them keep their own totals)."""
    hits = [p for p, _ in census if any(n in p.lower() for n in EXCLUDED_NAMES)]
    low = [p.lower() for p in hits]
    top = {p for p in hits if not any(p.lower().startswith(q + "\\") for q in low)}
    kept = [(p, b) for p, b in census if p not in set(hits)]
    total = sum(b for p, b in census if p in top)
    return [*kept, (EXCLUDED_KEY, total)]


def read_census(path: Path) -> list[Row]:
    ts = mtime_iso(path)  # the walk ends when the file is last written
    with path.open(newline="") as fh:
        rows = [(r["path"], int(r["bytes"])) for r in csv.DictReader(fh)]
    return [(ts, p, b) for p, b in collapse_excluded(rows)]


def read_watchdog(paths: list[Path], totals: dict[str, int]) -> list[Row]:
    out: dict[str, list[Row]] = {}
    for path in paths:
        with path.open(newline="") as fh:
            for rec in csv.reader(fh):
                if (
                    rec and rec[0][:2] == "20" and len(rec) >= 3
                ):  # skips headers, END and ABORT lines
                    out[rec[0]] = volume_rows(rec[0], int(rec[1]), int(rec[2]), totals)
    return [r for rows in out.values() for r in rows]


def read_jobs(build_dir: Path, totals: dict[str, int]) -> tuple[list[Row], int, int]:
    """Samples of every `jobs/*/runner.log`, deduplicated by timestamp (jobs overlapped).
    Returns `(rows, samples read, conflicting duplicates)`."""
    seen: dict[str, tuple[float, float]] = {}
    read = conflicts = 0
    for log in sorted((build_dir / "jobs").glob("*/runner.log")):
        for line in log.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[1] != "sample":
                continue
            disks = dict(d.split(":", 1) for d in parts[3].removeprefix("disks=").split(","))
            ts = parts[0][:19]
            val = (float(disks["C"].removesuffix("GB")), float(disks["D"].removesuffix("GB")))
            read += 1
            if ts in seen and seen[ts] != val:
                conflicts += 1
            seen.setdefault(ts, val)
    rows: list[Row] = []
    for ts, (c_free, d_free) in seen.items():  # the log prints free space in GiB with 1 decimal
        rows += volume_rows(ts, round(c_free * GIB), round(d_free * GIB), totals)
    return rows, read, conflicts


def write_rows(path: Path, rows: list[Row]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ts", "key", "bytes"])
        w.writerows(sorted(rows))
    if path.stat().st_size >= FIXTURE_MAX_BYTES:
        raise SystemExit(f"{path.name} is {path.stat().st_size} bytes: over the fixture budget")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-dir", type=Path, default=Path("D:/reclaim-build"))
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "growth",
    )
    args = ap.parse_args(argv)
    build: Path = args.build_dir
    totals = {"C": shutil.disk_usage("C:\\").total, "D": shutil.disk_usage("D:\\").total}
    args.out.mkdir(parents=True, exist_ok=True)

    attrib: list[Row] = []
    for name, date in ATTRIB_DATES.items():
        vols, folders = read_attrib(build / name, date, totals)
        attrib += [*thin(vols, VOLUME_GAP_S["attrib"]), *folders]
    census = [row for name in CENSUS_FILES for row in read_census(build / name)]
    wd_paths = sorted(build.glob("watchdog*.csv"))
    watchdog = thin(read_watchdog(wd_paths, totals), VOLUME_GAP_S["watchdog"])
    jobs_all, jobs_read, jobs_conflicts = read_jobs(build, totals)
    jobs = thin(jobs_all, VOLUME_GAP_S["jobs"])

    outputs = {"attrib": attrib, "census": census, "watchdog": watchdog, "jobs": jobs}
    for name, rows in outputs.items():
        write_rows(args.out / f"{name}.csv", rows)
    sources = {
        "attrib": [build / n for n in ATTRIB_DATES],
        "census": [build / n for n in CENSUS_FILES],
        "watchdog": wd_paths,
        "jobs": sorted((build / "jobs").glob("*/runner.log")),
    }
    meta = {
        "schema": "ts (local ISO, no offset), key, bytes; volume series are `volume:<drive>:used`",
        "volume_totals_bytes": totals,
        "volume_totals_note": "read once from shutil.disk_usage at extraction; assumed constant",
        "thinning_min_gap_s": VOLUME_GAP_S,
        "dropped": "process names, per-process writes, c_delta, files/hardlinked_bytes, swap_used",
        "excluded_projects": f"rows naming an excluded project -> `{EXCLUDED_KEY}`",
        "attrib_dates": ATTRIB_DATES,
        "census_time_rule": "source file mtime (the walk ends when the file is last written)",
        "jobs": {"samples_read": jobs_read, "conflicting_duplicates": jobs_conflicts},
        "rows": {name: len(rows) for name, rows in outputs.items()},
        "sources": {
            name: [
                {
                    "file": p.name if name != "jobs" else f"{p.parent.name}/{p.name}",
                    "sha256": sha256_of(p),
                    "bytes": p.stat().st_size,
                    "mtime": mtime_iso(p),
                }
                for p in paths
            ]
            for name, paths in sources.items()
        },
    }
    (args.out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    for name, rows in outputs.items():
        print(f"{name}: {len(rows)} rows, {(args.out / f'{name}.csv').stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
