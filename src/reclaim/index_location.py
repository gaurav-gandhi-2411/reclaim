from __future__ import annotations

from pathlib import Path

import structlog

from reclaim.app_paths import data_root
from reclaim.config import load_config

logger = structlog.get_logger(__name__)

INDEX_FILE_NAME = "reclaim_index.sqlite3"


def default_index_path() -> Path:
    """The pre-`[storage]` location: the install's own `data/` folder (ADR-0041)."""
    return data_root() / "data" / INDEX_FILE_NAME


def default_config_path() -> Path:
    """Where a command with no `--config` option (scan, reconcile, index-prune) finds config.toml:
    next to the executable when frozen, the CWD otherwise -- the same anchor as the data folder."""
    return data_root() / "config.toml"


def resolve_index_db(explicit: Path | None, config_path: Path) -> Path:
    """The one place the scan-index location is decided (ADR-0041).

    Every entry point (serve, dashboard, mcp-serve, check-disk-space, auto-clean and so the
    scheduled tasks, scan, reconcile, index-prune, apply) goes through this, called once from
    `cli.main`, so they cannot disagree about where the index -- and, because
    `approvals.channel_file_path` is "next to the index", the approval channel -- lives.

    Order: an explicit `--db` wins; then `[storage] index_dir` from config.toml; then the legacy
    `data/` default. A config.toml that exists but cannot be loaded RAISES instead of falling back
    to the legacy path: a quiet fallback would let one entry point read an empty second index while
    another uses the real one.
    """
    if explicit is not None:
        return explicit
    legacy = default_index_path()
    config = load_config(config_path if config_path.exists() else None)
    index_dir = config.storage.index_dir
    if not index_dir:
        return legacy
    chosen = Path(index_dir) / INDEX_FILE_NAME
    if not chosen.exists() and legacy.exists():
        # The classic mistake: index_dir set but the index not moved yet. The scan would silently
        # start a fresh multi-hour index; say so loudly instead.
        logger.warning(
            "index_location.configured_dir_has_no_index",
            configured=str(chosen),
            legacy_index=str(legacy),
            hint="move the index (and its -wal/-shm) to index_dir, or clear [storage] index_dir",
        )
    return chosen
