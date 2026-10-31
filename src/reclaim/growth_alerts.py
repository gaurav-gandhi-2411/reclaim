from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import structlog

from reclaim import growth
from reclaim.config import Config
from reclaim.index import ScanIndex
from reclaim.safety_env import refuse_real_side_effect_under_pytest

logger = structlog.get_logger(__name__)

# The scheduled side of disk growth attribution (spec §1.1 trigger, §1.5 toast; ADR-0046): what
# the 4-hourly `check-disk-space` task does with the growth store. Every entry point NEVER raises
# (a growth failure must not break the disk-space check) and, with `[growth] enabled = false`,
# does no growth IO at all: it does not even open the store.

SnapshotFn = Callable[..., growth.Snapshot]
EnvFactory = Callable[[Config], growth.Environment]


def scheduled_snapshot(
    config: Config,
    store_path: Path,
    *,
    index_path: Path | None = None,
    now: float | None = None,
    collect: SnapshotFn = growth.collect_snapshot,
    env_factory: EnvFactory = growth.environment_from_config,
) -> str:
    """Takes one snapshot when `[growth] enabled` and the last one is at least
    `min_interval_hours` old. Returns "disabled" | "not_due" | "taken" | "failed". The snapshot
    is bounded by `[growth] budget_seconds` / `budget_entries` (default 120 s / 3M entries).
    `collect`/`env_factory` are the seams tests replace with fakes."""
    if not config.growth.enabled:
        return "disabled"
    if collect is growth.collect_snapshot and env_factory is growth.environment_from_config:
        # A test must inject fakes: the real sampler would walk this machine's drives.
        refuse_real_side_effect_under_pytest("take a real disk-growth snapshot")
    current = time.time() if now is None else now
    index: ScanIndex | None = None
    try:
        with growth.GrowthStore(store_path) as store:
            if not growth.snapshot_due(store, config.growth.min_interval_hours, current):
                return "not_due"
            env = env_factory(config)
            if index_path is not None and index_path.is_file():
                index = ScanIndex(index_path)
            snap = collect(
                env,
                last_seen=store.last_seen(),
                index=index,
                budget_s=config.growth.budget_seconds,
                budget_entries=config.growth.budget_entries,
            )
            store.write(snap)
            return "taken"
    except Exception:  # a growth failure must never break the disk-space check
        logger.warning("growth.scheduled_snapshot_failed", exc_info=True)
        return "failed"
    finally:
        if index is not None:
            index.close()
