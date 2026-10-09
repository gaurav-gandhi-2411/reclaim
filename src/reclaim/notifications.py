from __future__ import annotations

import json
import os
import shutil
import sys
import threading
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

# Anchored via reclaim.app_paths.data_root: CWD-independent when compiled -- the frozen build
# now anchors to the real exe's directory instead of an arbitrary launch CWD. Dev/test
# resolution is deliberately UNCHANGED (still lazily CWD-relative, exactly like the original
# bare `Path("data/...")` literal -- data_root()'s own docstring explains why eager `Path.cwd()`
# capture would silently break `monkeypatch.chdir(tmp_path)`-based test isolation).
#
# CONFIRMED reachable from a working-directory-less invocation today: packaging/reclaim.iss's
# Task Scheduler action invokes `reclaim.exe check-disk-space` with NO --state/--config override
# (WorkingDirectory covers it there), but the [Registry] `reclaim-notify:` protocol handler --
# which has no working-directory concept at all -- currently compensates by hardcoding an
# absolute --state path in the registry command itself; this fix means that compensation is no
# longer load-bearing, not that it's been removed (removing it is a separate, unnecessary change
# here).
DEFAULT_STATE_PATH = data_root() / "data" / "notification_state.json"

_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0

# The custom URI scheme the toast's Snooze button launches (via `ToastButton(launch=...)`'s
# OS-level protocol activation -- see this module's own docstring above for why protocol
# activation, not an in-process on_activated callback, is the right mechanism for a short-lived
# scheduled-task process). packaging/reclaim.iss registers this scheme at install time under
# HKCU\Software\Classes (no admin needed) to invoke `reclaim check-disk-space --apply-snooze`.
SNOOZE_LAUNCH_URI = "reclaim-notify:snooze-disk-alert"


# Toast sender identity. windows_toasts' InteractableWindowsToaster defaults to Command Prompt's
# AUMID, and on a machine where that sender is switched off Windows reports
# NotificationSetting.DISABLED_FOR_APPLICATION and fails every toast (HRESULT 0x803E0111) while
# `show_toast` itself returns normally -- the 80% alert was silently dropped on the owner's
# machine in source and frozen builds alike (2026-10-10 diagnosis; the registry counter for the
# cmd.exe sender stopped at 18 on 2026-08-26). A dedicated, registered AUMID makes Reclaim its own
# sender (own entry in Settings > Notifications) and independent of Command Prompt's switch.
TOAST_AUMID = "Reclaim.DiskCleanup"
_TOAST_FAILURE_WAIT_SECONDS = 0.75  # the failure callback arrived in ~20 ms when measured


def ensure_toast_aumid() -> bool:
    """Registers `TOAST_AUMID` under HKCU (no admin) so Windows accepts toasts from it. The
    installer does the same in [Registry]; this covers a source run or a portable copy.
    Idempotent, NEVER raises, and a no-op under pytest (tests must not write the real profile)."""
    if sys.platform != "win32" or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    try:
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{TOAST_AUMID}"
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Reclaim")
    except OSError:
        logger.info("notifications.aumid_register_failed", exc_info=True)
        return False
    return True


def _show_toast_checked(toaster: object, toast: object) -> bool:
    """Shows `toast` and waits briefly for Windows' asynchronous failure callback. `show_toast`
    returns normally even when Windows refuses the toast, so without this a refused toast looks
    like a delivered one. Returns False (and logs the HRESULT) when Windows reported a failure."""
    failed = threading.Event()
    codes: list[object] = []

    def _on_failed(event: object) -> None:
        codes.append(getattr(event, "error_code", None))
        failed.set()

    toast.on_failed = _on_failed  # type: ignore[attr-defined]
    toaster.show_toast(toast)  # type: ignore[attr-defined]
    if failed.wait(_TOAST_FAILURE_WAIT_SECONDS):
        logger.info("notifications.toast_refused", error_code=str(codes[0] if codes else None))
        return False
    return True


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
        logger.info("notifications.check_no_notify", reason="disabled")
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
        logger.info(
            "notifications.check_no_notify", reason="below_threshold", percent_used=percent_used
        )
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
        logger.info("notifications.check_no_notify", reason="snoozed", percent_used=percent_used)
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
        logger.info("notifications.check_no_notify", reason="debounced", percent_used=percent_used)
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
    itself didn't raise and Windows did not report a failure within a short wait -- this is NOT
    a confirmation the user actually saw or will see it (Focus Assist can still hold it).

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

        ensure_toast_aumid()
        toaster = InteractableWindowsToaster("Reclaim", TOAST_AUMID)
        toast = Toast(
            [
                "Disk space is running low",
                f"{result.percent_used:.0f}% used ({result.percent_free:.0f}% free) -- above "
                f"your {result.threshold_percent:.0f}% alert threshold.",
            ]
        )
        toast.AddAction(ToastButton("Snooze for a week", launch=SNOOZE_LAUNCH_URI))
        return _show_toast_checked(toaster, toast)
    except Exception:
        logger.info("notifications.toast_failed", exc_info=True)
        return False


def autoclean_toast_body(freed_bytes: int, percent_used: float | None) -> str:
    """`Freed X, C: now Y% used.` -- the second half is omitted when usage is unknown. Pure, so
    the exact user-facing text is unit-testable without any toast stack."""
    from reclaim.api.schemas import format_bytes  # deferred: keep this module's import light

    freed = f"Freed {format_bytes(freed_bytes)}"
    if percent_used is None:
        return f"{freed}."
    return f"{freed}, C: now {percent_used:.0f}% used."


def send_autoclean_toast(freed_bytes: int, percent_used: float | None, skipped_in_use: int) -> bool:
    """Fires the "weekly auto-clean finished" toast (ADR-0034). Same reliability posture as
    `send_disk_space_toast`: NEVER raises, `windows_toasts` imported lazily, `True` only means
    the toast call did not raise (not that the user saw it).

    Whether to call this at all is the caller's decision (`reclaim auto-clean --notify` only does
    so when something was freed or skipped as in-use -- a weekly "Freed 0 B" toast is noise).
    `skipped_in_use` is accepted so the signature matches that policy and is logged."""
    try:
        logger.info(
            "notifications.autoclean_toast", freed_bytes=freed_bytes, skipped_in_use=skipped_in_use
        )
        from windows_toasts import InteractableWindowsToaster, Toast

        ensure_toast_aumid()
        toaster = InteractableWindowsToaster("Reclaim", TOAST_AUMID)
        toast = Toast(
            ["Reclaim cleaned your disk", autoclean_toast_body(freed_bytes, percent_used)]
        )
        return _show_toast_checked(toaster, toast)
    except Exception:
        logger.info("notifications.autoclean_toast_failed", exc_info=True)
        return False
