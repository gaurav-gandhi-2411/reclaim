from __future__ import annotations

import json
import time
from pathlib import Path

# Marker file, not a log — "acknowledged" is a one-way, one-time transition (spec: "First-run
# screen (shown once)"), so there is no history to fold, unlike mode_log.jsonl/manifest.jsonl.
DEFAULT_FIRST_RUN_STATE_PATH = Path("data/first_run_state.json")


def is_acknowledged(path: Path | None = None) -> bool:
    resolved = path if path is not None else DEFAULT_FIRST_RUN_STATE_PATH
    return resolved.exists()


def acknowledge(path: Path | None = None, *, now: float | None = None) -> float:
    """Records that the first-run screen was shown and acknowledged. Idempotent: acknowledging
    twice just overwrites the timestamp, never errors — the dashboard calls this once per real
    acknowledgment, but a caller retrying a dropped request must not be punished for it."""
    resolved = path if path is not None else DEFAULT_FIRST_RUN_STATE_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    acknowledged_at = now if now is not None else time.time()
    resolved.write_text(json.dumps({"acknowledged_at": acknowledged_at}), encoding="utf-8")
    return acknowledged_at
