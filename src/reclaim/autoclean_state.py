from __future__ import annotations

# ADR-0034 addendum: durable state for the scheduled auto-clean's logon retry.
#
# The weekly task has two triggers (Sunday 10:00 and shortly after sign-in). Both run
# `auto-clean --apply --notify --scheduled`; this file is what lets the second one be cheap:
# `last_full_run_utc` says when the whole regenerable tier last ran, `pending_tools` names the
# native package-manager tools whose item ended `skipped_in_use` (uv's cache lock never freed
# up). Policy (`decide_scheduled_run`): never-ran / stale / unreadable state -> run the full tier;
# otherwise retry ONLY the pending tools; nothing pending -> do nothing.
#
# This module decides WHEN and WHICH TOOLS to run, nothing else. It cannot widen what is deleted:
# the keys it hands back only ever filter the existing allow-list (`regenerable.NATIVE_TOOLS`),
# and unknown keys read from a tampered file are dropped. Unreadable state fails toward "run the
# full tier", which only touches provably-regenerable caches (safe by ADR-0034).
import contextlib
import json
import os
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog

from reclaim.app_paths import data_root
from reclaim.safety_env import assert_not_real_profile_under_pytest

logger = structlog.get_logger(__name__)

# ADR-0027: a file with no `schema_version` is version 1 (the shape that introduced the field).
AUTOCLEAN_STATE_SCHEMA_VERSION = 1
STATE_FILE_NAME = "autoclean_state.json"
# The weekly trigger fires every 7 days; 6 leaves a day of slack so a logon run the day before
# the Sunday run does not trigger a second full run, while a missed week always does.
FULL_RUN_MAX_AGE = timedelta(days=6)

# Only this status re-queues a tool; every other outcome leaves nothing to retry.
_RETRY_STATUS = "skipped_in_use"

RunMode = Literal["full", "retry", "noop"]


@dataclass(frozen=True, slots=True)
class AutoCleanState:
    last_full_run_utc: datetime | None = None
    pending_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ScheduledRunPlan:
    mode: RunMode
    # None = no filter (the whole tier); a set = run only these native tools.
    only_keys: frozenset[str] | None
    reason: str


def default_state_path() -> Path:
    return data_root() / "data" / STATE_FILE_NAME


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_state(path: Path | None = None, *, known_tools: Collection[str]) -> AutoCleanState:
    """Never raises. Missing, unreadable, corrupt or wrong-shaped -> the empty state (which the
    policy turns into a full run). Unknown tool keys are dropped, never trusted."""
    state_path = path if path is not None else default_state_path()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AutoCleanState()
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError: a pathologically nested file ("[" * 100000) overflows the JSON decoder;
        # it is as corrupt as any other garbage. MemoryError is deliberately NOT swallowed.
        logger.warning("autoclean_state.unreadable", path=str(state_path), error=str(exc))
        return AutoCleanState()
    try:
        return _parse(data, known_tools)
    except (TypeError, ValueError) as exc:
        logger.warning("autoclean_state.corrupt", path=str(state_path), error=str(exc))
        return AutoCleanState()


def _parse(data: object, known_tools: Collection[str]) -> AutoCleanState:
    if not isinstance(data, dict):
        raise TypeError("state is not a JSON object")
    version = data.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TypeError(f"bad schema_version {version!r}")
    if version > AUTOCLEAN_STATE_SCHEMA_VERSION:
        # Forward compat (ADR-0027): read the fields we know, warn, never raise.
        logger.warning("autoclean_state.newer_schema", found=version)
    raw_last = data.get("last_full_run_utc")
    last: datetime | None = None
    if raw_last is not None:
        if not isinstance(raw_last, str):
            raise TypeError("last_full_run_utc is not a string")
        last = datetime.fromisoformat(raw_last)
        if last.tzinfo is None:
            raise ValueError("last_full_run_utc has no UTC offset")
    raw_pending = data.get("pending_tools", [])
    if not isinstance(raw_pending, list) or not all(isinstance(p, str) for p in raw_pending):
        raise TypeError("pending_tools is not a list of strings")
    return AutoCleanState(last, frozenset(p for p in raw_pending if p in known_tools))


def write_state(state: AutoCleanState, path: Path | None = None) -> None:
    """Atomic: temp file in the same directory + `Path.replace` (os.replace), so a crash or a
    concurrent reader sees either the old or the new file, never a torn one."""
    state_path = path if path is not None else default_state_path()
    assert_not_real_profile_under_pytest(state_path, operation="write the auto-clean state")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": AUTOCLEAN_STATE_SCHEMA_VERSION,
        "last_full_run_utc": state.last_full_run_utc.isoformat()
        if state.last_full_run_utc is not None
        else None,
        "pending_tools": sorted(state.pending_tools),
    }
    fd, tmp = tempfile.mkstemp(dir=state_path.parent, prefix=".autoclean_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        Path(tmp).replace(state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def decide_scheduled_run(state: AutoCleanState, now: datetime) -> ScheduledRunPlan:
    last = state.last_full_run_utc
    if last is None:
        return ScheduledRunPlan("full", None, "no full run recorded")
    age = now - last
    if age < timedelta(0) or age > FULL_RUN_MAX_AGE:
        # A future timestamp (clock change / tampering) is as untrustworthy as a stale one.
        return ScheduledRunPlan("full", None, f"last full run {last.isoformat()} is not recent")
    if state.pending_tools:
        tools = ", ".join(sorted(state.pending_tools))
        reason = f"retrying tools left in use: {tools}"
        return ScheduledRunPlan("retry", state.pending_tools, reason)
    return ScheduledRunPlan("noop", frozenset(), "full run is recent and nothing is pending")


def state_after_run(
    previous: AutoCleanState,
    plan: ScheduledRunPlan,
    native_results: Iterable[tuple[str, str]],
    all_native_keys: Collection[str],
    now: datetime,
) -> AutoCleanState:
    """`native_results` = (tool key, item status) for the native-command items that ran.
    A tool that ran and is not `skipped_in_use` leaves pending (completed, absent, excluded or
    failed -- the next full run re-examines it); one that is `skipped_in_use` joins pending. A tool
    that did not run keeps its previous pending flag."""
    results = list(native_results)
    ran = {key for key, _ in results}
    pending = (set(previous.pending_tools) - ran) | {
        key for key, status in results if status == _RETRY_STATUS
    }
    last = now if plan.mode == "full" else previous.last_full_run_utc
    return AutoCleanState(last, frozenset(pending & set(all_native_keys)))
