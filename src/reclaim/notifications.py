from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from reclaim.app_paths import data_root
from reclaim.config import NotificationsConfig

logger = structlog.get_logger(__name__)

# R5 (80%-threshold disk-space notification). This module is the CLI-callable core of a feature
# that must survive being invoked from a per-user Task Scheduler entry (packaging/reclaim.iss) a
# few times a day, with no interactive session guaranteed and no one watching a terminal --
# `update_check.py` is the reliability template this whole module copies: NEVER raise, short
# timeouts/cheap I/O only, and every failure mode degrades to "did nothing this run" rather than
# crashing the scheduled task (which Task Scheduler would otherwise start marking as failing,
# eventually surfacing as Windows Action Center noise unrelated to actual disk space).
#
# The Windows toast itself (`send_disk_space_toast`) is a separate, best-effort concern from the
# pure decision logic below (`check_disk_space`, state persistence, debounce, snooze) -- the pure
# logic is fully unit-testable without a real Windows desktop/notification session; the toast
# call is not (see this module's own test file for exactly which surface is/isn't covered).

# CWD-independent (see reclaim.app_paths.data_root's docstring). CONFIRMED reachable from a
# working-directory-less invocation today: packaging/reclaim.iss's Task Scheduler action invokes
# `reclaim.exe check-disk-space` with NO --state/--config override (WorkingDirectory covers it
# there), but the [Registry] `reclaim-notify:` protocol handler -- which has no working-directory
# concept at all -- currently compensates by hardcoding an absolute --state path in the registry
# command itself; this fix means that compensation is no longer load-bearing, not that it's been
# removed (removing it is a separate, unnecessary change here).
DEFAULT_STATE_PATH = data_root() / "data" / "notification_state.json"

_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0

# The custom URI scheme the toast's Snooze button launches (via `ToastButton(launch=...)`'s
# OS-level protocol activation -- see this module's own docstring above for why protocol
# activation, not an in-process on_activated callback, is the right mechanism for a short-lived
# scheduled-task process). packaging/reclaim.iss registers this scheme at install time under
# HKCU\Software\Classes (no admin needed) to invoke `reclaim check-disk-space --apply-snooze`.
SNOOZE_LAUNCH_URI = "reclaim-notify:snooze-disk-alert"


def _default_drive_anchor() -> Path:
    """Resolves the drive to measure free space on: the Windows system drive (where user-profile
    data and most real disk pressure accumulates), not necessarily wherever Reclaim itself is
    installed. `SystemDrive` is set by Windows for every process; falls back to the literal
    `C:\\` a CI runner or unusual environment missing that var would still need -- same
    `_win_path`-style fallback discipline `config.py`'s own default-path helpers use."""
    system_drive = os.environ.get("SYSTEMDRIVE", "C:")
    return Path(f"{system_drive}\\")


@dataclass(frozen=True)
class NotificationState:
    """Persisted debounce/snooze state, one JSON object at `data/notification_state.json` --
    matches `first_run.py`'s plain-marker-file convention (not a log like
    `mode_log.jsonl`/`manifest.jsonl`): each field is a one-way-updated latest value, so there is
    no history to fold, only the current value to read and overwrite."""

    last_notified_at: float | None = None
    snoozed_until: float | None = None


def load_state(path: Path | None = None) -> NotificationState:
    """Never raises -- a missing, empty, or corrupt state file is treated exactly like "no prior
    state" (both fields `None`), the same reliability posture `update_check.check_for_update`
    applies to a failed GitHub response. A corrupt/foreign-shaped file is logged, not surfaced to
    the caller: this is debounce/snooze bookkeeping, not anything safety-critical enough to block
    a check over."""
    resolved = path if path is not None else DEFAULT_STATE_PATH
    if not resolved.exists():
        return NotificationState()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        last_notified_at = raw.get("last_notified_at") if isinstance(raw, dict) else None
        snoozed_until = raw.get("snoozed_until") if isinstance(raw, dict) else None
        if not isinstance(last_notified_at, int | float):
            last_notified_at = None
        if not isinstance(snoozed_until, int | float):
            snoozed_until = None
        return NotificationState(last_notified_at=last_notified_at, snoozed_until=snoozed_until)
    except Exception:
        logger.info("notifications.state_load_failed", path=str(resolved), exc_info=True)
        return NotificationState()


def save_state(state: NotificationState, path: Path | None = None) -> None:
    """Never raises -- a failed write (disk full, permission denied, a read-only vault path in a
    test fixture) degrades to "debounce/snooze state didn't persist this run", never crashes the
    caller. Matches this module's "never raise" posture end to end (see module docstring)."""
    resolved = path if path is not None else DEFAULT_STATE_PATH
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(
            json.dumps(
                {"last_notified_at": state.last_notified_at, "snoozed_until": state.snoozed_until}
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.info("notifications.state_save_failed", path=str(resolved), exc_info=True)


def record_notified(path: Path | None = None, *, now: float | None = None) -> NotificationState:
    """Updates `last_notified_at` to `now` (default: real time), preserving any existing
    `snoozed_until`. Idempotent: calling this twice just overwrites the timestamp, never errors
    -- same idempotency discipline as `first_run.acknowledge`."""
    resolved = path if path is not None else DEFAULT_STATE_PATH
    existing = load_state(resolved)
    updated = NotificationState(
        last_notified_at=now if now is not None else time.time(),
        snoozed_until=existing.snoozed_until,
    )
    save_state(updated, resolved)
    return updated


def apply_snooze(
    path: Path | None = None, *, snooze_days: float, now: float | None = None
) -> NotificationState:
    """Sets `snoozed_until` to `snooze_days` from `now` (default: real time), preserving
    `last_notified_at`. This is the write side of the toast's Snooze action button -- invoked via
    `reclaim check-disk-space --apply-snooze` (see cli.py), reached through the registered
    `reclaim-notify:` protocol handler, never by a user typing the command directly."""
    resolved = path if path is not None else DEFAULT_STATE_PATH
    existing = load_state(resolved)
    current = now if now is not None else time.time()
    updated = NotificationState(
        last_notified_at=existing.last_notified_at,
        snoozed_until=current + (snooze_days * _SECONDS_PER_DAY),
    )
    save_state(updated, resolved)
    return updated


def is_snoozed(state: NotificationState, *, now: float) -> bool:
    """True while `now` is still before a previously-recorded `snoozed_until` -- pure, so it's
    trivially testable without touching the filesystem."""
    return state.snoozed_until is not None and now < state.snoozed_until


def _should_renotify(state: NotificationState, *, now: float, renotify_after_hours: float) -> bool:
    """True once enough wall-clock time has passed since the last notification (or none has ever
    fired) to fire again for a still-crossed threshold. This is the debounce spec item 2 asks
    for: a scheduled task running several times a day must not re-fire the same
    threshold-crossing alert on every single run while the disk stays full."""
    if state.last_notified_at is None:
        return True
    return (now - state.last_notified_at) >= (renotify_after_hours * _SECONDS_PER_HOUR)


def evaluate_threshold(percent_used: float, threshold_percent: float) -> bool:
    """Pure crossing predicate -- True once usage is at or above `threshold_percent`. Split out
    from `check_disk_space` so the crossing rule itself is trivially unit-testable without any
    filesystem/state-file/config involvement."""
    return percent_used >= threshold_percent


@dataclass(frozen=True)
class DiskSpaceCheckResult:
    """Outcome of one `check_disk_space` call.

    `status` is `"ok"` when the feature ran to completion (even if it decided not to notify) and
    `"unknown"` only for a real measurement failure (`shutil.disk_usage` raising `OSError`) --
    mirrors `update_check.UpdateCheckResult`'s status vocabulary. `should_notify` is the final
    decision after enabled/threshold/snooze/debounce are all applied; `reason` explains why when
    `should_notify` is `False`, surfaced by the CLI for visibility into a background feature no
    one is otherwise watching run.
    """

    status: str  # "ok" | "unknown"
    percent_used: float | None
    percent_free: float | None
    threshold_percent: float
    crossed: bool
    should_notify: bool
    reason: str
    # "disabled" | "measurement_failed" | "below_threshold" | "snoozed" | "debounced" |
    # "would_notify"


def check_disk_space(
    config: NotificationsConfig,
    *,
    anchor: Path | None = None,
    state_path: Path | None = None,
    now: float | None = None,
) -> DiskSpaceCheckResult:
    """Computes current disk usage on `anchor` (default: the Windows system drive) and decides
    whether a threshold-crossing notification should fire, applying the config's `enabled` flag,
    the debounce window, and any active snooze. Does NOT itself fire a toast or write state --
    see `reclaim.cli`'s `check-disk-space` subcommand for the caller that acts on this result
    (`send_disk_space_toast` + `record_notified`). Keeping the decision pure from the actions
    means this function alone is what the unit tests below exercise for every
    threshold/debounce/snooze combination, with no real Windows toast/notification stack
    involved.

    NEVER raises -- mirrors `update_check.check_for_update`'s reliability posture exactly: a
    scheduled-task-triggered background check must never crash or hang the task, regardless of
    what's wrong with the disk, the config, or the state file.
    """
    resolved_anchor = anchor if anchor is not None else _default_drive_anchor()
    current = now if now is not None else time.time()
    threshold = config.disk_threshold_percent

    if not config.enabled:
        return DiskSpaceCheckResult(
            status="ok",
            percent_used=None,
            percent_free=None,
            threshold_percent=threshold,
            crossed=False,
            should_notify=False,
            reason="disabled",
        )

    try:
        usage = shutil.disk_usage(resolved_anchor)
    except OSError:
        logger.info("notifications.disk_usage_failed", anchor=str(resolved_anchor), exc_info=True)
        return DiskSpaceCheckResult(
            status="unknown",
            percent_used=None,
            percent_free=None,
            threshold_percent=threshold,
            crossed=False,
            should_notify=False,
            reason="measurement_failed",
        )

    percent_used = (usage.used / usage.total) * 100.0 if usage.total > 0 else 0.0
    percent_free = 100.0 - percent_used
    crossed = evaluate_threshold(percent_used, threshold)

    if not crossed:
        return DiskSpaceCheckResult(
            status="ok",
            percent_used=percent_used,
            percent_free=percent_free,
            threshold_percent=threshold,
            crossed=False,
            should_notify=False,
            reason="below_threshold",
        )

    state = load_state(state_path)
    if is_snoozed(state, now=current):
        return DiskSpaceCheckResult(
            status="ok",
            percent_used=percent_used,
            percent_free=percent_free,
            threshold_percent=threshold,
            crossed=True,
            should_notify=False,
            reason="snoozed",
        )
    if not _should_renotify(state, now=current, renotify_after_hours=config.renotify_after_hours):
        return DiskSpaceCheckResult(
            status="ok",
            percent_used=percent_used,
            percent_free=percent_free,
            threshold_percent=threshold,
            crossed=True,
            should_notify=False,
            reason="debounced",
        )

    return DiskSpaceCheckResult(
        status="ok",
        percent_used=percent_used,
        percent_free=percent_free,
        threshold_percent=threshold,
        crossed=True,
        should_notify=True,
        reason="would_notify",
    )


def send_disk_space_toast(result: DiskSpaceCheckResult) -> bool:
    """Fires the native Windows toast for a threshold-crossing disk-space check.

    NEVER raises -- any failure (an unavailable WinRT/COM notification stack, no interactive
    desktop session, a locked/logged-out session a scheduled task can still be triggered under)
    degrades to a logged no-op, the same posture as every other best-effort background feature in
    this codebase (`update_check.check_for_update`). Returns `True` only when the toast call
    itself didn't raise -- this is NOT a confirmation the user actually saw or will see it;
    Windows gives no such guarantee to the sending process.

    The `windows_toasts` import is deferred to inside this function (not at module load) so
    `reclaim.notifications`'s pure logic (`check_disk_space`, state persistence, debounce,
    snooze) stays importable and unit-testable even in an environment where the real WinRT toast
    stack can't be meaningfully exercised end-to-end (no guarantee of an interactive session
    receiving a popup) -- see this feature's test file and the PR description for exactly what
    was and wasn't integration-tested.

    Uses `InteractableWindowsToaster` (not the plain `WindowsToaster`) specifically because the
    Snooze button needs `ToastButton.launch` to render with `activationType="protocol"` cleanly,
    matching the library's own documented usage for toasts with actions (a plain `WindowsToaster`
    still renders the button correctly -- verified directly against this library's source -- but
    emits a spurious runtime warning since it assumes buttons imply an in-process
    `on_activated` callback, which this feature deliberately does not use; see the module
    docstring for why).
    """
    if result.percent_used is None or result.percent_free is None:
        return False
    try:
        from windows_toasts import InteractableWindowsToaster, Toast, ToastButton

        toaster = InteractableWindowsToaster("Reclaim")
        toast = Toast(
            [
                "Disk space is running low",
                f"{result.percent_used:.0f}% used ({result.percent_free:.0f}% free) -- above "
                f"your {result.threshold_percent:.0f}% alert threshold.",
            ]
        )
        toast.AddAction(ToastButton("Snooze for a week", launch=SNOOZE_LAUNCH_URI))
        toaster.show_toast(toast)
    except Exception:
        logger.info("notifications.toast_failed", exc_info=True)
        return False
    return True
