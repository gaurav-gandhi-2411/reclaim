from __future__ import annotations

import os
import random
from dataclasses import dataclass
from pathlib import Path

_SECONDS_PER_DAY = 86400.0


def _random_bytes(n: int, seed: int) -> bytes:
    # Deterministic fixture content, not security-sensitive.
    return random.Random(seed).randbytes(n)  # noqa: S311


def _write(path: Path, content: bytes, *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


@dataclass(frozen=True, slots=True)
class ExecutorFixtureTree:
    """Key paths from the materialized Stage 5 executor e2e fixture tree."""

    root: Path
    package_json: Path
    node_modules_dir: Path
    node_modules_file_a: Path
    node_modules_file_b: Path
    old_log: Path
    kept_file: Path


def build_executor_fixture_tree(root: Path, *, now: float) -> ExecutorFixtureTree:
    """Materializes a real filesystem tree under `root`: one directory-shaped candidate
    (`node_modules`, rebuildable next to its adjacent `package.json`) and one file-shaped
    candidate (an old, stale log), plus a file that must never become a candidate (a negative
    control). Real byte content throughout — the e2e eval independently re-reads bytes off
    disk rather than trusting any pipeline-computed value.

    Never touches anything outside `root`.
    """
    package_json = root / "Project" / "package.json"
    _write(package_json, _random_bytes(64, seed=1), mtime=now)

    node_modules_dir = root / "Project" / "node_modules"
    node_modules_file_a = node_modules_dir / "pkg" / "index.js"
    node_modules_file_b = node_modules_dir / "pkg" / "lib" / "util.js"
    _write(node_modules_file_a, _random_bytes(5_000, seed=2), mtime=now)
    _write(node_modules_file_b, _random_bytes(3_000, seed=3), mtime=now)

    old_log = root / "Logs" / "old_big.log"
    _write(old_log, _random_bytes(2_000, seed=4), mtime=now - 45 * _SECONDS_PER_DAY)

    kept_file = root / "Documents" / "keep_me.txt"
    _write(kept_file, _random_bytes(256, seed=5), mtime=now)

    return ExecutorFixtureTree(
        root=root,
        package_json=package_json,
        node_modules_dir=node_modules_dir,
        node_modules_file_a=node_modules_file_a,
        node_modules_file_b=node_modules_file_b,
        old_log=old_log,
        kept_file=kept_file,
    )
