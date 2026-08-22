"""R5 (80%-threshold disk-space notification).

Covers everything genuinely unit-testable without a real Windows desktop/notification session:
threshold crossing, debounce, snooze, state persistence, and `check_disk_space`'s orchestration
of all four. `send_disk_space_toast`'s tests stub `windows_toasts` via `sys.modules` injection
(same "never make the real external call in a test" discipline `test_update_check.py` applies to
`httpx` via `MockTransport`) -- this repo's own CI/dev sandbox has no guarantee of an interactive
session that could actually display a popup, so a test asserting a toast was *seen* is not
achievable here; what IS covered is that `send_disk_space_toast` calls the library correctly
(right toaster class, right button/launch URI) and never raises regardless of what the library
does.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from reclaim.config import NotificationsConfig
from reclaim.notifications import (
    SNOOZE_LAUNCH_URI,
    DiskSpaceCheckResult,
    NotificationState,
    apply_snooze,
    check_disk_space,
    evaluate_threshold,
    is_snoozed,
    load_state,
    record_notified,
    save_state,
    send_disk_space_toast,
)

_NOW = 1_700_000_000.0


def _config(**overrides: Any) -> NotificationsConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "disk_threshold_percent": 80.0,
        "renotify_after_hours": 24.0,
        "snooze_days": 7,
    }
    defaults.update(overrides)
    return NotificationsConfig(**defaults)


class _FakeDiskUsage:
    def __init__(self, total: int, used: int) -> None:
        self.total = total
        self.used = used
        self.free = total - used


# --- Pure threshold predicate --------------------------------------------------------------------


def test_evaluate_threshold_true_at_or_above() -> None:
    assert evaluate_threshold(80.0, 80.0) is True
    assert evaluate_threshold(85.0, 80.0) is True


def test_evaluate_threshold_false_below() -> None:
    assert evaluate_threshold(79.9, 80.0) is False


# --- State persistence ------------------------------------------------------------------------


def test_load_state_missing_file_returns_empty_state(tmp_path: Path) -> None:
    state = load_state(tmp_path / "does_not_exist.json")
    assert state == NotificationState()


def test_save_then_load_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    save_state(NotificationState(last_notified_at=123.0, snoozed_until=456.0), path)

    loaded = load_state(path)

    assert loaded == NotificationState(last_notified_at=123.0, snoozed_until=456.0)


def test_load_state_corrupt_json_returns_empty_state_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    path.write_text("not json at all {{{", encoding="utf-8")

    assert load_state(path) == NotificationState()


def test_load_state_unexpected_json_shape_returns_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert load_state(path) == NotificationState()


def test_save_state_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "notification_state.json"
    save_state(NotificationState(last_notified_at=1.0), path)

    assert path.exists()
    assert load_state(path).last_notified_at == 1.0


def test_record_notified_preserves_existing_snooze(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    apply_snooze(path, snooze_days=7, now=_NOW)

    updated = record_notified(path, now=_NOW + 10.0)

    assert updated.last_notified_at == _NOW + 10.0
    assert updated.snoozed_until == _NOW + (7 * 86400.0)


def test_apply_snooze_preserves_existing_last_notified(tmp_path: Path) -> None:
    path = tmp_path / "notification_state.json"
    record_notified(path, now=_NOW)

    updated = apply_snooze(path, snooze_days=3, now=_NOW + 5.0)

    assert updated.last_notified_at == _NOW
    assert updated.snoozed_until == _NOW + 5.0 + (3 * 86400.0)


# --- Snooze predicate -------------------------------------------------------------------------


def test_is_snoozed_true_before_snoozed_until() -> None:
    state = NotificationState(snoozed_until=_NOW + 100.0)
    assert is_snoozed(state, now=_NOW) is True


def test_is_snoozed_false_after_snoozed_until() -> None:
    state = NotificationState(snoozed_until=_NOW - 100.0)
    assert is_snoozed(state, now=_NOW) is False


def test_is_snoozed_false_when_never_snoozed() -> None:
    assert is_snoozed(NotificationState(), now=_NOW) is False


# --- check_disk_space: full orchestration -------------------------------------------------------


def test_check_disk_space_disabled_never_measures_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_anchor: Path) -> _FakeDiskUsage:  # pragma: no cover -- must never be called
        raise AssertionError("disk_usage must not be called when notifications are disabled")

    monkeypatch.setattr("reclaim.notifications.shutil.disk_usage", _boom)

    result = check_disk_space(
        _config(enabled=False), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert result == DiskSpaceCheckResult(
        status="ok",
        percent_used=None,
        percent_free=None,
        threshold_percent=80.0,
        crossed=False,
        should_notify=False,
        reason="disabled",
    )


def test_check_disk_space_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=50),
    )

    result = check_disk_space(
        _config(), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert result.status == "ok"
    assert result.percent_used == 50.0
    assert result.crossed is False
    assert result.should_notify is False
    assert result.reason == "below_threshold"


def test_check_disk_space_crossed_first_time_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=85),
    )

    result = check_disk_space(
        _config(), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert result.status == "ok"
    assert result.percent_used == 85.0
    assert result.percent_free == 15.0
    assert result.crossed is True
    assert result.should_notify is True
    assert result.reason == "would_notify"


def test_check_disk_space_crossed_but_snoozed_does_not_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    apply_snooze(state_path, snooze_days=7, now=_NOW - 10.0)

    result = check_disk_space(_config(), anchor=tmp_path, state_path=state_path, now=_NOW)

    assert result.crossed is True
    assert result.should_notify is False
    assert result.reason == "snoozed"


def test_check_disk_space_crossed_but_debounced_does_not_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    record_notified(state_path, now=_NOW - 3600.0)  # notified 1h ago, well within 24h debounce

    result = check_disk_space(
        _config(renotify_after_hours=24.0), anchor=tmp_path, state_path=state_path, now=_NOW
    )

    assert result.crossed is True
    assert result.should_notify is False
    assert result.reason == "debounced"


def test_check_disk_space_crossed_renotifies_after_debounce_window_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    record_notified(state_path, now=_NOW - (25 * 3600.0))  # 25h ago -- past the 24h window

    result = check_disk_space(
        _config(renotify_after_hours=24.0), anchor=tmp_path, state_path=state_path, now=_NOW
    )

    assert result.should_notify is True
    assert result.reason == "would_notify"


def test_check_disk_space_snooze_expired_renotifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    apply_snooze(state_path, snooze_days=1, now=_NOW - (2 * 86400.0))  # snooze already lapsed

    result = check_disk_space(_config(), anchor=tmp_path, state_path=state_path, now=_NOW)

    assert result.should_notify is True
    assert result.reason == "would_notify"


def test_check_disk_space_measurement_failure_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_oserror(_anchor: Path) -> _FakeDiskUsage:
        raise OSError("simulated disk_usage failure")

    monkeypatch.setattr("reclaim.notifications.shutil.disk_usage", _raise_oserror)

    result = check_disk_space(
        _config(), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert result.status == "unknown"
    assert result.percent_used is None
    assert result.should_notify is False
    assert result.reason == "measurement_failed"


class _RecordingLogger:
    """Minimal stand-in for `notifications.py`'s module-level `structlog` logger -- see the
    identical helper in `tests/test_mode.py` for why a hand-rolled recorder is used instead of
    `caplog` (this project's structlog isn't wired to stdlib logging)."""

    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.infos.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:  # pragma: no cover - unused here
        pass


# AI3: before this, a scheduled-task-triggered check that decided NOT to notify (disabled,
# below threshold, snoozed, debounced) wrote nothing to the structured log -- only a `print()`
# in cli.py's `_run_check_disk_space`, which Task Scheduler's no-console invocation discards.
# That made a real no-toast result ambiguous between "correctly decided not to notify" and
# "the process never got that far." These four tests prove each silent branch now leaves a
# `notifications.check_no_notify` log entry naming the exact reason.


def test_check_disk_space_disabled_logs_the_no_notify_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr("reclaim.notifications.logger", recorder)

    check_disk_space(
        _config(enabled=False), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert recorder.infos == [("notifications.check_no_notify", {"reason": "disabled"})]


def test_check_disk_space_below_threshold_logs_the_no_notify_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr("reclaim.notifications.logger", recorder)
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=50),
    )

    check_disk_space(_config(), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW)

    assert recorder.infos == [
        ("notifications.check_no_notify", {"reason": "below_threshold", "percent_used": 50.0})
    ]


def test_check_disk_space_snoozed_logs_the_no_notify_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr("reclaim.notifications.logger", recorder)
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    apply_snooze(state_path, snooze_days=7, now=_NOW - 10.0)

    check_disk_space(_config(), anchor=tmp_path, state_path=state_path, now=_NOW)

    assert recorder.infos == [
        ("notifications.check_no_notify", {"reason": "snoozed", "percent_used": 90.0})
    ]


def test_check_disk_space_debounced_logs_the_no_notify_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr("reclaim.notifications.logger", recorder)
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=100, used=90),
    )
    state_path = tmp_path / "state.json"
    record_notified(state_path, now=_NOW - 3600.0)

    check_disk_space(
        _config(renotify_after_hours=24.0), anchor=tmp_path, state_path=state_path, now=_NOW
    )

    assert recorder.infos == [
        ("notifications.check_no_notify", {"reason": "debounced", "percent_used": 90.0})
    ]


def test_check_disk_space_zero_total_bytes_does_not_divide_by_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degenerate `shutil.disk_usage` result (total=0) must never raise ZeroDivisionError --
    an edge case a real Windows drive should never produce, but this function must survive it
    regardless (never-raise is the module's hard invariant, not "never raise on plausible
    input")."""
    monkeypatch.setattr(
        "reclaim.notifications.shutil.disk_usage",
        lambda _anchor: _FakeDiskUsage(total=0, used=0),
    )

    result = check_disk_space(
        _config(), anchor=tmp_path, state_path=tmp_path / "state.json", now=_NOW
    )

    assert result.status == "ok"
    assert result.percent_used == 0.0
    assert result.crossed is False


# --- send_disk_space_toast: library call correctness + never-raise, via sys.modules stub --------


def _crossed_result(*, percent_used: float = 85.0) -> DiskSpaceCheckResult:
    return DiskSpaceCheckResult(
        status="ok",
        percent_used=percent_used,
        percent_free=100.0 - percent_used,
        threshold_percent=80.0,
        crossed=True,
        should_notify=True,
        reason="would_notify",
    )


def test_send_disk_space_toast_returns_false_when_measurement_missing() -> None:
    result = DiskSpaceCheckResult(
        status="ok",
        percent_used=None,
        percent_free=None,
        threshold_percent=80.0,
        crossed=False,
        should_notify=False,
        reason="disabled",
    )
    assert send_disk_space_toast(result) is False


def _install_fake_windows_toasts(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stubs `windows_toasts` in `sys.modules` so `send_disk_space_toast`'s deferred `from
    windows_toasts import ...` resolves to fakes instead of the real WinRT-backed library --
    matches `test_update_check.py`'s `httpx.MockTransport` discipline of never making the real
    external call from a test. Returns a recorder the test can assert against."""
    recorder = SimpleNamespace(shown_toasts=[], added_actions=[], toaster_app_names=[])

    class FakeToastButton:
        def __init__(self, content: str, launch: str | None = None) -> None:
            self.content = content
            self.launch = launch

    class FakeToast:
        def __init__(self, text_fields: list[str]) -> None:
            self.text_fields = text_fields
            self.actions: list[FakeToastButton] = []

        def AddAction(self, action: FakeToastButton) -> None:  # matches real windows_toasts API
            self.actions.append(action)
            recorder.added_actions.append(action)

    class FakeInteractableWindowsToaster:
        def __init__(self, application_text: str) -> None:
            recorder.toaster_app_names.append(application_text)

        def show_toast(self, toast: FakeToast) -> None:
            recorder.shown_toasts.append(toast)

    fake_module = ModuleType("windows_toasts")
    fake_module.InteractableWindowsToaster = FakeInteractableWindowsToaster  # type: ignore[attr-defined]
    fake_module.Toast = FakeToast  # type: ignore[attr-defined]
    fake_module.ToastButton = FakeToastButton  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "windows_toasts", fake_module)
    return recorder


def test_send_disk_space_toast_calls_library_with_snooze_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install_fake_windows_toasts(monkeypatch)

    sent = send_disk_space_toast(_crossed_result(percent_used=85.0))

    assert sent is True
    assert recorder.toaster_app_names == ["Reclaim"]
    assert len(recorder.shown_toasts) == 1
    assert len(recorder.added_actions) == 1
    assert recorder.added_actions[0].launch == SNOOZE_LAUNCH_URI
    toast_text = " ".join(recorder.shown_toasts[0].text_fields)
    assert "85%" in toast_text
    assert "15%" in toast_text
    assert "80%" in toast_text


def test_send_disk_space_toast_never_raises_when_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "windows_toasts", None)  # forces ImportError on import

    result = send_disk_space_toast(_crossed_result())

    assert result is False


def test_send_disk_space_toast_never_raises_when_show_toast_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingToaster:
        def __init__(self, _application_text: str) -> None:
            pass

        def show_toast(self, _toast: object) -> None:
            raise RuntimeError("simulated WinRT/COM failure")

    fake_module = ModuleType("windows_toasts")
    fake_module.InteractableWindowsToaster = ExplodingToaster  # type: ignore[attr-defined]
    fake_module.Toast = lambda text_fields: SimpleNamespace(  # type: ignore[attr-defined]
        text_fields=text_fields, AddAction=lambda action: None
    )
    fake_module.ToastButton = lambda content, launch=None: SimpleNamespace(  # type: ignore[attr-defined]
        content=content, launch=launch
    )
    monkeypatch.setitem(sys.modules, "windows_toasts", fake_module)

    result = send_disk_space_toast(_crossed_result())

    assert result is False
