"""D5 timing harness: `generate_candidates` live vs index-trusted, on a synthetic tree.

Builds a throwaway tree under --workdir (must not exist yet; nothing outside it is touched, the
real profile and the real index are never opened), scans it into a fresh index there, then times
`generate_candidates(..., trust_index=False)` against `trust_index=True` and checks both return the
same candidates with the same reclaimable figures (a fresh index, so they must agree).

    python scripts/bench_warmup_index_trust.py --workdir D:/reclaim-build/tmp/d5/bench --files 50000

The caches are small next to a real profile (230,045 folders / 609,545 files in the 25 Oct
measurement), and the OS file cache is warm from creating the tree, so the live figures here are
a floor: the ratio is the point, not the absolute seconds.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from reclaim.config import (
    CategoriesConfig,
    Config,
    ExclusionsConfig,
    PackageCachesConfig,
    SafetyConfig,
)
from reclaim.detectors import generate_candidates
from reclaim.index import ScanIndex
from reclaim.models import Mode
from reclaim.safety import SafetyValidator
from reclaim.scanner import scan_tree

# Kept out of --workdir by construction; a project-name exclusion like the owner's real ones.
PROJECT_NAME = "zqprotected"
CACHES = 20
FILES_PER_DIR = 50


def build_tree(root: Path, files: int) -> list[Path]:
    """`CACHES` cache directories sharing `files` files; every 100th file is hard-linked to a path
    outside its cache, and two caches hold a folder matching the exclusion."""
    caches = [root / "caches" / f"cache{i:02d}" for i in range(CACHES)]
    outside = root / "elsewhere"
    outside.mkdir(parents=True)
    per_cache = files // CACHES
    for cache_no, cache in enumerate(caches):
        for n in range(per_cache):
            sub = cache / f"d{n // FILES_PER_DIR:04d}" / f"e{n % 7}"
            sub.mkdir(parents=True, exist_ok=True)
            target = sub / f"f{n}.bin"
            target.write_bytes(b"x" * (64 + n % 13))
            if n % 100 == 0:
                os.link(target, outside / f"alias_{cache_no}_{n}.bin")
        if cache_no in (3, 11):
            (cache / "d0000" / f"{PROJECT_NAME}-proj").mkdir(parents=True)
            (cache / "d0000" / f"{PROJECT_NAME}-proj" / "keep.bin").write_bytes(b"k")
    return caches


def timed(label: str, index: ScanIndex, config: Config, *, trust_index: bool) -> tuple[float, list]:
    safety = SafetyValidator(config)
    started = time.perf_counter()
    candidates = generate_candidates(index, config, safety, now=1.0, trust_index=trust_index)
    elapsed = time.perf_counter() - started
    print(f"  {label:<14} {elapsed:8.3f} s  ({len(candidates)} candidates)")
    return elapsed, candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--files", type=int, default=50_000)
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()
    workdir: Path = args.workdir
    if workdir.exists():
        sys.exit(f"refusing: {workdir} already exists (pass a fresh --workdir)")
    if PROJECT_NAME in str(workdir):
        sys.exit(f"refusing: --workdir must not contain {PROJECT_NAME!r}")
    world = workdir / "world"
    caches = build_tree(world, args.files)
    config = Config(
        safety=SafetyConfig(protected_roots=[]),
        exclusions=ExclusionsConfig(project_names=[PROJECT_NAME]),
        categories=CategoriesConfig(
            package_caches=PackageCachesConfig(enabled=True, paths=[c.as_posix() for c in caches])
        ),
        mode=Mode.POWER,
    )
    (workdir / "db").mkdir()
    with ScanIndex(workdir / "db" / "i.sqlite3") as index:
        scan_tree(world, index, incremental=False)
        print(f"{args.files} files in {CACHES} caches, {args.reps} reps each:")
        live_times, index_times = [], []
        for rep in range(args.reps):
            live_s, live = timed(f"live #{rep + 1}", index, config, trust_index=False)
            index_s, trusted = timed(f"index #{rep + 1}", index, config, trust_index=True)
            live_times.append(live_s)
            index_times.append(index_s)
        same_paths = [c.path for c in live] == [c.path for c in trusted]
        same_bytes = [c.reclaimable_bytes for c in live] == [c.reclaimable_bytes for c in trusted]
        print(f"same candidates: {same_paths}; same reclaimable bytes: {same_bytes}")
        live_best, index_best = min(live_times), min(index_times)
        print(
            f"best live {live_best:.3f} s -> best index {index_best:.3f} s "
            f"({live_best / index_best:.1f}x)"
        )
    # Only the workdir this run created; left in place for the caller to inspect and remove.
    print(f"scratch left at {workdir} (remove it yourself)")
    return 0 if same_paths and same_bytes else 1


if __name__ == "__main__":
    raise SystemExit(main())
