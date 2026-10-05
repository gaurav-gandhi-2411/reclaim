from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reclaim import autoclean_state, cli, notifications, regenerable
from reclaim.api import service
from reclaim.autoclean_state import (
    AutoCleanState,
    ScheduledRunPlan,
    decide_scheduled_run,
    read_state,
    state_after_run,
    write_state,
)
from reclaim.regenerable import CommandResult, RegenerableEnv

# ADR-0034 addendum: the logon retry. Behavior-level: the real `reclaim auto-clean --apply
# --scheduled` entry point against a fake machine whose `uv`/`pip` commands are injected -- no real
# schtasks, no real uv. The state file path is redirected per test by tests/conftest.py.

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
KNOWN = [spec.key for spec in regenerable.NATIVE_TOOLS]
UV_LOCK_EXPIRED = CommandResult(
    2, "", "error: Timeout (1s) when waiting for lock on `C:\\cache` at `C:\\cache\\.lock`"
)


# --- state file ---------------------------------------------------------------------------------


def test_state_round_trips_and_is_schema_versioned(tmp_path: Path) -> None:
    path = tmp_path / "data" / "autoclean_state.json"  # parent is created on demand
    write_state(AutoCleanState(NOW, frozenset({"uv", "pip"})), path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {
        "schema_version": 1,
        "last_full_run_utc": NOW.isoformat(),
        "pending_tools": ["pip", "uv"],
    }
    assert read_state(path, known_tools=KNOWN) == AutoCleanState(NOW, frozenset({"uv", "pip"}))


def test_missing_state_reads_as_empty(tmp_path: Path) -> None:
    assert read_state(tmp_path / "nope.json", known_tools=KNOWN) == AutoCleanState()


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"schema_version": "one"}',
        '{"schema_version": 0}',
        '{"schema_version": true}',
        '{"last_full_run_utc": "yesterday"}',
        '{"last_full_run_utc": "2026-10-05T12:00:00"}',  # naive: ambiguous, rejected
        '{"last_full_run_utc": 5}',
        '{"pending_tools": "uv"}',
        '{"pending_tools": [1]}',
    ],
)
def test_corrupt_state_reads_as_empty_never_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(text, encoding="utf-8")

    assert read_state(path, known_tools=KNOWN) == AutoCleanState()


def test_unreadable_state_reads_as_empty(tmp_path: Path) -> None:
    # A directory where the file should be: read_text raises OSError (PermissionError on Windows).
    path = tmp_path / "s.json"
    path.mkdir()

    assert read_state(path, known_tools=KNOWN) == AutoCleanState()


def test_state_without_schema_version_is_version_1(tmp_path: Path) -> None:
    # ADR-0027: a pre-versioning file has no key and means v1.
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"last_full_run_utc": NOW.isoformat(), "pending_tools": ["uv"]}),
        encoding="utf-8",
    )

    assert read_state(path, known_tools=KNOWN) == AutoCleanState(NOW, frozenset({"uv"}))


def test_newer_schema_keeps_known_fields_and_ignores_unknown(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "last_full_run_utc": NOW.isoformat(),
                "pending_tools": ["uv"],
                "added_by_a_future_release": {"x": 1},
            }
        ),
        encoding="utf-8",
    )

    assert read_state(path, known_tools=KNOWN) == AutoCleanState(NOW, frozenset({"uv"}))


def test_unknown_pending_tool_keys_are_dropped(tmp_path: Path) -> None:
    # A tampered file cannot name anything outside the allow-list.
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"last_full_run_utc": NOW.isoformat(), "pending_tools": ["uv", "C:/Windows"]}),
        encoding="utf-8",
    )

    assert read_state(path, known_tools=KNOWN).pending_tools == frozenset({"uv"})


def test_write_is_atomic_failed_write_keeps_old_file_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.json"
    write_state(AutoCleanState(NOW, frozenset({"uv"})), path)
    before = path.read_bytes()

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(autoclean_state.json, "dump", boom)
    with pytest.raises(OSError, match="disk full"):
        write_state(AutoCleanState(NOW, frozenset()), path)

    assert path.read_bytes() == before, "a failed write must never tear the existing file"
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"], "no temp file left behind"


# --- policy matrix ------------------------------------------------------------------------------


def test_never_ran_runs_full() -> None:
    plan = decide_scheduled_run(AutoCleanState(), NOW)
    assert (plan.mode, plan.only_keys) == ("full", None)


def test_ran_one_day_ago_with_nothing_pending_is_a_noop() -> None:
    plan = decide_scheduled_run(AutoCleanState(NOW - timedelta(days=1)), NOW)
    assert plan.mode == "noop"


def test_ran_one_day_ago_with_pending_uv_retries_only_uv() -> None:
    plan = decide_scheduled_run(AutoCleanState(NOW - timedelta(days=1), frozenset({"uv"})), NOW)
    assert (plan.mode, plan.only_keys) == ("retry", frozenset({"uv"}))


def test_ran_seven_days_ago_runs_full_even_with_pending() -> None:
    plan = decide_scheduled_run(AutoCleanState(NOW - timedelta(days=7), frozenset({"uv"})), NOW)
    assert (plan.mode, plan.only_keys) == ("full", None)


def test_boundary_just_inside_six_days_is_still_recent() -> None:
    inside = AutoCleanState(NOW - timedelta(days=6))
    outside = AutoCleanState(NOW - timedelta(days=6, seconds=1))
    assert decide_scheduled_run(inside, NOW).mode == "noop"
    assert decide_scheduled_run(outside, NOW).mode == "full"


def test_last_run_in_the_future_runs_full() -> None:
    assert decide_scheduled_run(AutoCleanState(NOW + timedelta(days=2)), NOW).mode == "full"


# --- state transitions --------------------------------------------------------------------------

_FULL = ScheduledRunPlan("full", None, "")
_RETRY = ScheduledRunPlan("retry", frozenset({"uv"}), "")


def test_skipped_in_use_becomes_pending_and_full_run_stamps_time() -> None:
    new = state_after_run(
        AutoCleanState(), _FULL, [("uv", "skipped_in_use"), ("pip", "cleaned")], KNOWN, NOW
    )
    assert new == AutoCleanState(NOW, frozenset({"uv"}))


@pytest.mark.parametrize("status", ["cleaned", "nothing_to_clean"])
def test_pending_cleared_on_completion_and_retry_keeps_last_full_run(status: str) -> None:
    earlier = NOW - timedelta(days=2)
    new = state_after_run(
        AutoCleanState(earlier, frozenset({"uv"})), _RETRY, [("uv", status)], KNOWN, NOW
    )
    assert new == AutoCleanState(earlier, frozenset())


def test_retry_that_is_skipped_again_stays_pending() -> None:
    earlier = NOW - timedelta(days=2)
    new = state_after_run(
        AutoCleanState(earlier, frozenset({"uv"})), _RETRY, [("uv", "skipped_in_use")], KNOWN, NOW
    )
    assert new == AutoCleanState(earlier, frozenset({"uv"}))


def test_tool_that_did_not_run_keeps_its_pending_flag() -> None:
    earlier = NOW - timedelta(days=2)
    new = state_after_run(
        AutoCleanState(earlier, frozenset({"uv", "pip"})), _RETRY, [("uv", "cleaned")], KNOWN, NOW
    )
    assert new.pending_tools == frozenset({"pip"})


# --- the real CLI entry point on a fake machine ---------------------------------------------------


@dataclass
class Rig:
    home: Path
    uv_result: CommandResult = field(default_factory=lambda: CommandResult(0, "", ""))
    commands: list[list[str]] = field(default_factory=list)

    def env(self) -> RegenerableEnv:
        local = self.home / "AppData" / "Local"

        def run_command(argv: object, _t: float, _e: dict[str, str]) -> CommandResult:
            self.commands.append(list(argv))  # type: ignore[call-overload]
            return self.uv_result if argv[0] == "uv" else CommandResult(0, "", "")  # type: ignore[index]

        return RegenerableEnv(
            home=self.home,
            local_appdata=local,
            temp_roots=(local / "Temp",),
            crash_dump_roots=(),
            now=lambda: 2_000_000_000.0,
            running_process_names=lambda: frozenset(),
            which=lambda name: name if name in {"uv", "pip"} else None,
            run_command=run_command,
            has_open_handle=lambda _p: False,
            disk_anchor=self.home,
        )

    @property
    def native_commands(self) -> list[str]:
        return [c[0] for c in self.commands]


class ToastRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float | None, int]] = []

    def __call__(self, freed: int, pct: float | None, skipped: int) -> bool:
        self.calls.append((freed, pct, skipped))
        return True


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    for rel in ("uv/cache", "pip/Cache"):
        target = local / rel
        target.mkdir(parents=True)
        (target / "blob").write_bytes(b"x" * 100)
    (local / "Temp").mkdir(parents=True)
    r = Rig(home)
    monkeypatch.setattr(service, "regenerable_clean_env", r.env)
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)
    return r


@pytest.fixture
def toast(monkeypatch: pytest.MonkeyPatch) -> ToastRecorder:
    recorder = ToastRecorder()
    monkeypatch.setattr(notifications, "send_autoclean_toast", recorder)
    return recorder


@pytest.fixture
def config_on(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[autoclean]\nenabled = true\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autoclean_state, "utc_now", lambda: NOW)


def _scheduled(config: Path) -> int:
    return cli.main(["auto-clean", "--apply", "--notify", "--scheduled", "--config", str(config)])


def _state(known: list[str] = KNOWN) -> AutoCleanState:
    return read_state(autoclean_state.default_state_path(), known_tools=known)


def test_first_scheduled_run_is_full_and_uv_lock_expiry_becomes_pending(
    rig: Rig, config_on: Path, toast: ToastRecorder
) -> None:
    rig.uv_result = UV_LOCK_EXPIRED

    assert _scheduled(config_on) == 0

    assert rig.native_commands == ["pip", "uv"]
    assert _state() == AutoCleanState(NOW, frozenset({"uv"}))


def test_logon_run_with_nothing_pending_is_silent_noop(
    rig: Rig, config_on: Path, toast: ToastRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    write_state(AutoCleanState(NOW - timedelta(days=1)), autoclean_state.default_state_path())
    before = autoclean_state.default_state_path().read_bytes()

    assert _scheduled(config_on) == 0

    assert rig.commands == [], "no tool may run"
    assert toast.calls == [], "a no-op sign-in must not toast"
    assert "nothing to do" in capsys.readouterr().err
    assert autoclean_state.default_state_path().read_bytes() == before


def test_logon_retry_runs_only_pending_uv_and_clears_it_on_success(
    rig: Rig, config_on: Path, toast: ToastRecorder
) -> None:
    earlier = NOW - timedelta(days=1)
    write_state(AutoCleanState(earlier, frozenset({"uv"})), autoclean_state.default_state_path())
    # Something other than uv that a full run would clean: it must survive a retry-only run.
    old_temp = rig.home / "AppData" / "Local" / "Temp" / "old.bin"
    old_temp.write_bytes(b"y" * 10)
    os.utime(old_temp, (1_000_000_000, 1_000_000_000))

    assert _scheduled(config_on) == 0

    assert rig.native_commands == ["uv"], "pip must not run on a retry"
    assert old_temp.exists(), "temp/browser items are not part of a retry"
    assert _state() == AutoCleanState(earlier, frozenset()), "cleared; last full run untouched"


def test_logon_retry_still_locked_stays_pending(
    rig: Rig, config_on: Path, toast: ToastRecorder
) -> None:
    earlier = NOW - timedelta(days=1)
    write_state(AutoCleanState(earlier, frozenset({"uv"})), autoclean_state.default_state_path())
    rig.uv_result = UV_LOCK_EXPIRED

    assert _scheduled(config_on) == 0

    assert _state() == AutoCleanState(earlier, frozenset({"uv"}))


def test_week_old_state_runs_full_tier_again(
    rig: Rig, config_on: Path, toast: ToastRecorder
) -> None:
    write_state(AutoCleanState(NOW - timedelta(days=7)), autoclean_state.default_state_path())

    assert _scheduled(config_on) == 0

    assert rig.native_commands == ["pip", "uv"]
    assert _state().last_full_run_utc == NOW


def test_corrupt_state_runs_full_tier(rig: Rig, config_on: Path, toast: ToastRecorder) -> None:
    path = autoclean_state.default_state_path()
    path.write_text("{garbage", encoding="utf-8")

    assert _scheduled(config_on) == 0

    assert rig.native_commands == ["pip", "uv"]
    assert _state() == AutoCleanState(NOW, frozenset())


def test_unwritable_state_does_not_fail_the_run(
    rig: Rig, config_on: Path, toast: ToastRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(autoclean_state, "write_state", boom)

    assert _scheduled(config_on) == 0
    assert rig.native_commands == ["pip", "uv"]


def test_dry_run_and_manual_runs_never_touch_the_state(rig: Rig, config_on: Path) -> None:
    assert cli.main(["auto-clean", "--scheduled", "--config", str(config_on)]) == 0  # dry run
    assert cli.main(["auto-clean", "--apply", "--config", str(config_on)]) == 0  # manual
    assert not autoclean_state.default_state_path().exists()


def test_retry_respects_user_exclusions(rig: Rig, tmp_path: Path, toast: ToastRecorder) -> None:
    config = tmp_path / "excl.toml"
    config.write_text(
        '[autoclean]\nenabled = true\n[safety]\ndeny = ["**/uv/cache"]\n', encoding="utf-8"
    )
    write_state(
        AutoCleanState(NOW - timedelta(days=1), frozenset({"uv"})),
        autoclean_state.default_state_path(),
    )

    assert _scheduled(config) == 0

    assert rig.commands == [], "an excluded cache root is never pruned, retry or not"
    # skipped_excluded is not skipped_in_use: it leaves pending instead of retrying forever.
    assert _state().pending_tools == frozenset()


# --- only_keys can only narrow the allow-list ----------------------------------------------------


def test_only_keys_filters_the_plan_and_cannot_add_items(rig: Rig) -> None:
    env = rig.env()

    full = {p.key for p in regenerable._plan_items(env, apply=True)}
    narrowed = {
        p.key
        for p in regenerable._plan_items(env, apply=True, only_keys=frozenset({"uv", "C:/Windows"}))
    }

    assert narrowed == {"uv"}
    assert narrowed < full
