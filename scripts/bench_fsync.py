from __future__ import annotations

# bench_fsync.py -- measures the real per-item cost of ADR-0026's crash-safe two-phase manifest
# (intent+fsync, action, done+fsync) against a synthetic batch shaped like the real dev_artifacts
# pycache apply referenced in executor.py's own ADR-0004 comment ("23,565 direct_delete entries
# alongside 7 vault ones"). See docs/architecture/adr/0026-crash-safe-two-phase-manifest.md's
# "Measured fsync cost" section for the numbers this script produced and how they were used.
#
# Usage: uv run python scripts/bench_fsync.py
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from reclaim.config import Config
from reclaim.executor import apply_batch
from reclaim.models import Candidate, Tier, Verdict
from reclaim.safety import SafetyValidator

N = 23000


def _make_candidates(src_dir: Path, n: int) -> list[Candidate]:
    src_dir.mkdir(parents=True, exist_ok=True)
    candidates = []
    for i in range(n):
        path = src_dir / f"file_{i:06d}.pyc"
        path.write_bytes(b"x" * 16)
        candidates.append(
            Candidate(
                path=path,
                is_dir=False,
                category="pycache",
                category_group="dev_artifacts",
                size_bytes=16,
                tier=Tier.A,
                rationale="bench",
                rebuild_instruction=None,
                safety_verdict=Verdict.ELIGIBLE,
                safety_reason_code="TEST",
                retention_days=30,
                size_guard_exempt=False,
                rebuildable=True,
            )
        )
    return candidates


def _run(root: Path, *, disable_fsync: bool) -> float:
    src_dir = root / "src"
    vault_dir = root / "vault"
    candidates = _make_candidates(src_dir, N)
    safety = SafetyValidator(Config())

    t0 = time.perf_counter()
    if disable_fsync:
        # Patches the shared `os` module directly (not `executor.os`) -- `executor.py` does
        # `import os` and calls `os.fsync(...)`, so this affects its calls identically without
        # mypy strict flagging a reach into another module's non-exported import.
        with mock.patch("os.fsync", lambda fd: None):
            report = apply_batch(
                candidates,
                safety=safety,
                apply=True,
                method="vault",
                vault_dir=vault_dir,
                manifest_path=vault_dir / "manifest.jsonl",
            )
    else:
        report = apply_batch(
            candidates,
            safety=safety,
            apply=True,
            method="vault",
            vault_dir=vault_dir,
            manifest_path=vault_dir / "manifest.jsonl",
        )
    elapsed = time.perf_counter() - t0
    if report.files_succeeded != N:
        raise RuntimeError(f"expected {N} succeeded, got {report.files_succeeded}")
    return elapsed


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="reclaim_bench_fsync_") as tmp:
        with_root = Path(tmp) / "with_fsync"
        t_with = _run(with_root, disable_fsync=False)
        shutil.rmtree(with_root, ignore_errors=True)
        print(
            f"WITH fsync:    {t_with:.2f}s total, {t_with / N * 1000:.3f} ms/item, "
            f"{2 * N} fsync calls"
        )

        without_root = Path(tmp) / "without_fsync"
        t_without = _run(without_root, disable_fsync=True)
        shutil.rmtree(without_root, ignore_errors=True)
        print(
            f"WITHOUT fsync: {t_without:.2f}s total, {t_without / N * 1000:.3f} ms/item "
            "(flush-only baseline)"
        )

        delta = t_with - t_without
        print(
            f"\nDelta attributable to fsync: {delta:.2f}s over {N} items "
            f"({delta / N * 1000:.3f} ms/item average, {2 * N} fsync calls)"
        )


if __name__ == "__main__":
    main()
