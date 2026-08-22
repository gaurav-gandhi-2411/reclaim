from __future__ import annotations

import json
import time
from pathlib import Path

from reclaim.app_paths import data_root

# Marker file, not a log — "acknowledged" is a one-way, one-time transition (spec: "First-run
# screen (shown once)"), so there is no history to fold, unlike mode_log.jsonl/manifest.jsonl.
#
# Anchored via reclaim.app_paths.data_root (see PR #51 for the original confirmed-live crash
# this class of bug caused elsewhere): CWD-independent when compiled -- the frozen build now
# anchors to the real exe's directory instead of an arbitrary launch CWD. Dev/test resolution is
# deliberately UNCHANGED (still lazily CWD-relative, exactly like the original bare
# `Path("data/...")` literal -- data_root()'s own docstring explains why eager `Path.cwd()`
# capture would silently break `monkeypatch.chdir(tmp_path)`-based test isolation). Not yet
# reachable from any working-directory-less invocation today, but "not reachable today" is a
# property of today's call sites, not of the code.
DEFAULT_FIRST_RUN_STATE_PATH = data_root() / "data" / "first_run_state.json"


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
