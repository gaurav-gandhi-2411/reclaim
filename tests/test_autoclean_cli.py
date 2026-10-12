from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reclaim import cli, notifications, regenerable
from reclaim.api import service
from reclaim.regenerable import CommandResult, RegenerableEnv

# Teeth-proof tests for ADR-0034's scheduled path: they drive the real `reclaim auto-clean
# --apply --scheduled` entry point against a fake home directory and assert on what is really on
# disk afterwards. Each protection (browser-running, open-handle, allow-list, enabled flag, dry
# run) has a test that fails if that protection is removed.

NOW = 2_000_000_000.0
DAY = 86400.0

EDGE_CACHE_BYTES = 5000
OLD_TEMP_BYTES = 4096
CHROME_CACHE_BYTES = 3000
OPEN_TEMP_BYTES = 777


def _write(path: Path, size: int, *, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


@dataclass
class Machine:
    home: Path
    temp: Path
    running: set[str] = field(default_factory=set)
    open_paths: set[str] = field(default_factory=set)
    commands_run: list[list[str]] = field(default_factory=list)
    extra_temp_roots: tuple[Path, ...] = ()  # mutation-check hook: simulate an over-wide root
    # populated by `_build`
    aged_temp: Path = Path()
    fresh_temp: Path = Path()
    open_temp: Path = Path()
    chrome_cache: Path = Path()
    edge_cache: Path = Path()
    protected: list[Path] = field(default_factory=list)

    def env(self) -> RegenerableEnv:
        local = self.home / "AppData" / "Local"

        def run_command(argv: object, _timeout: float, _env: dict[str, str]) -> CommandResult:
            self.commands_run.append(list(argv))  # type: ignore[call-overload]
            return CommandResult(0, "", "")

        return RegenerableEnv(
            home=self.home,
            local_appdata=local,
            temp_roots=(self.temp, *self.extra_temp_roots),
            crash_dump_roots=(),
            now=lambda: NOW,
            running_process_names=lambda: frozenset(self.running),
            which=lambda _name: None,
            run_command=run_command,
            has_open_handle=lambda p: p in self.open_paths,
            disk_anchor=self.home,
        )


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    temp = local / "Temp"
    m = Machine(home=home, temp=temp)
    m.aged_temp = _write(temp / "old_build" / "artifact.bin", OLD_TEMP_BYTES, age_days=30)
    m.fresh_temp = _write(temp / "fresh.bin", 100, age_days=1)
    m.open_temp = _write(temp / "held_open.tmp", OPEN_TEMP_BYTES, age_days=30)
    m.open_paths.add(str(m.open_temp))
    m.chrome_cache = _write(
        local / "Google" / "Chrome" / "User Data" / "Default" / "Cache" / "data_0",
        CHROME_CACHE_BYTES,
        age_days=60,
    )
    m.edge_cache = _write(
        local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache" / "data_0",
        EDGE_CACHE_BYTES,
        age_days=60,
    )
    # Things that are old, big and tempting -- but not on the allow-list. They must NEVER move.
    m.protected = [
        _write(home / "Documents" / "thesis.docx", 9000, age_days=400),
        _write(home / "Downloads" / "installer.zip", 9000, age_days=400),
        _write(home / ".cache" / "huggingface" / "hub" / "model.bin", 9000, age_days=400),
        _write(home / "old_projects" / "notes.txt", 9000, age_days=400),
        _write(local / "SomeApp" / "state.db", 9000, age_days=400),
        _write(local / "Google" / "Chrome" / "User Data" / "Default" / "Cookies", 50, age_days=60),
        _write(
            local / "Microsoft" / "Edge" / "User Data" / "Default" / "Login Data", 50, age_days=60
        ),
    ]
    monkeypatch.setattr(service, "regenerable_clean_env", m.env)
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)
    return m


@pytest.fixture
def config_on(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[autoclean]\nenabled = true\n", encoding="utf-8")
    return path


@pytest.fixture
def config_off(tmp_path: Path) -> Path:
    path = tmp_path / "config_off.toml"
    path.write_text("[autoclean]\nenabled = false\n", encoding="utf-8")
    return path


class ToastRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float | None, int]] = []

    def __call__(self, freed_bytes: int, percent_used: float | None, skipped_in_use: int) -> bool:
        self.calls.append((freed_bytes, percent_used, skipped_in_use))
        return True


@pytest.fixture
def toast(monkeypatch: pytest.MonkeyPatch) -> ToastRecorder:
    recorder = ToastRecorder()
    monkeypatch.setattr(notifications, "send_autoclean_toast", recorder)
    return recorder


def _snapshot(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


# (1) active caches are skipped ---------------------------------------------------------------


def test_running_browser_cache_and_open_handle_temp_file_survive(
    machine: Machine, config_on: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.running.add("chrome.exe")

    code = cli.main(["auto-clean", "--apply", "--scheduled", "--json", "--config", str(config_on)])

    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert machine.chrome_cache.exists(), "a running browser's cache must be left alone"
    assert machine.open_temp.exists(), "a file with an open handle must never be deleted"
    items = {i["key"]: i for i in body["items"]}
    assert items["chrome"]["status"] == "skipped_browser_running"
    assert items["chrome"]["bytes_removed"] == 0
    assert items["temp0"]["files_skipped_in_use"] == 1
    assert items["temp0"]["skipped_paths"] == [str(machine.open_temp)]
    assert body["files_skipped_in_use"] == 1
    # ...while the genuinely stale things were still cleaned in the same run.
    assert not machine.aged_temp.exists() and not machine.edge_cache.exists()


# (2) aged caches are really cleaned ---------------------------------------------------------


def test_aged_temp_and_closed_browser_cache_are_deleted_and_total_is_exact(
    machine: Machine, config_on: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.open_paths.clear()  # nothing held open in this scenario

    code = cli.main(["auto-clean", "--apply", "--scheduled", "--json", "--config", str(config_on)])

    assert code == 0
    body = json.loads(capsys.readouterr().out)
    expected = OLD_TEMP_BYTES + OPEN_TEMP_BYTES + CHROME_CACHE_BYTES + EDGE_CACHE_BYTES
    assert not machine.aged_temp.exists(), "aged temp entry really deleted, not moved"
    assert not machine.open_temp.exists()
    assert not machine.chrome_cache.exists() and not machine.edge_cache.exists()
    assert machine.fresh_temp.exists(), "a file inside the age floor is kept"
    assert body["bytes_removed"] == expected
    assert sum(i["bytes_removed"] for i in body["items"]) == expected
    # Cookies / Login Data are not cache directories: untouched even though the browser is closed.
    assert all(p.exists() for p in machine.protected)


def test_text_output_reports_items_total_and_final_freed_line(
    machine: Machine, config_on: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(config_on)])

    out = capsys.readouterr().out
    assert "cleaned" in out and "Total freed:" in out
    assert "disk free before" in out and "delta" in out
    last = out.strip().splitlines()[-1]
    assert last.startswith("Freed ") and "C: now" in last and last.endswith("% used")


def test_cli_run_writes_the_audit_log(machine: Machine, config_on: Path, tmp_path: Path) -> None:
    cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(config_on)])

    lines = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text("utf-8").splitlines()]
    assert any(x.get("summary") for x in lines)
    assert any(x.get("key") == "temp0" and x["apply"] is True for x in lines)


# (3) non-allow-listed paths are untouched ----------------------------------------------------


def test_nothing_outside_the_allow_list_is_touched(machine: Machine, config_on: Path) -> None:
    before = _snapshot(machine.home)
    deleted_by_design = {
        machine.aged_temp,
        machine.open_temp,
        machine.chrome_cache,
        machine.edge_cache,
    }
    machine.open_paths.clear()

    cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(config_on)])

    after = _snapshot(machine.home)
    assert before - after == deleted_by_design, "only allow-listed files may disappear"
    for path in machine.protected:
        assert path.exists(), f"{path} is outside the allow-list and must survive"
    assert after - before == set()


# (4) --scheduled with the feature off does nothing at all -------------------------------------


def test_scheduled_run_with_autoclean_disabled_does_nothing(
    machine: Machine, config_off: Path, toast: ToastRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.open_paths.clear()
    before = _snapshot(machine.home)

    code = cli.main(
        ["auto-clean", "--apply", "--notify", "--scheduled", "--config", str(config_off)]
    )

    assert code == 0
    assert _snapshot(machine.home) == before, "nothing may be deleted"
    assert machine.commands_run == [], "no tool-native command may run"
    assert toast.calls == []
    assert "turned off" in capsys.readouterr().out


def test_scheduled_run_with_missing_config_is_off_by_default(
    machine: Machine, tmp_path: Path
) -> None:
    before = _snapshot(machine.home)

    code = cli.main(["auto-clean", "--apply", "--scheduled", "--config", str(tmp_path / "no.toml")])

    assert code == 0 and _snapshot(machine.home) == before


# (5) default invocation is a dry run ---------------------------------------------------------


def test_default_invocation_without_apply_deletes_nothing(
    machine: Machine, config_on: Path, toast: ToastRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.open_paths.clear()
    before = _snapshot(machine.home)

    code = cli.main(["auto-clean", "--notify", "--config", str(config_on)])

    assert code == 0
    assert _snapshot(machine.home) == before
    assert machine.commands_run == []
    assert toast.calls == [], "a dry run never toasts"
    assert "Would free" in capsys.readouterr().out


def test_apply_and_dry_run_flags_are_mutually_exclusive(config_on: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["auto-clean", "--apply", "--dry-run", "--config", str(config_on)])


# (6) the toast -------------------------------------------------------------------------------


def test_toast_fires_once_with_freed_total_when_something_was_freed(
    machine: Machine, config_on: Path, toast: ToastRecorder
) -> None:
    machine.open_paths.clear()

    cli.main(["auto-clean", "--apply", "--notify", "--scheduled", "--config", str(config_on)])

    assert len(toast.calls) == 1
    freed, percent, skipped = toast.calls[0]
    assert freed == OLD_TEMP_BYTES + OPEN_TEMP_BYTES + CHROME_CACHE_BYTES + EDGE_CACHE_BYTES
    assert percent is not None and skipped == 0


def test_toast_fires_when_only_in_use_files_were_skipped(
    machine: Machine, config_on: Path, toast: ToastRecorder
) -> None:
    for path in (machine.aged_temp, machine.chrome_cache, machine.edge_cache):
        machine.open_paths.add(str(path))
    machine.running.add("chrome.exe")
    machine.running.add("msedge.exe")

    cli.main(["auto-clean", "--apply", "--notify", "--scheduled", "--config", str(config_on)])

    assert len(toast.calls) == 1
    assert toast.calls[0][0] == 0 and toast.calls[0][2] >= 1


def test_toast_not_fired_when_nothing_happened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_on: Path,
    toast: ToastRecorder,
) -> None:
    home = tmp_path / "empty_home"
    (home / "AppData" / "Local" / "Temp").mkdir(parents=True)
    empty = Machine(home=home, temp=home / "AppData" / "Local" / "Temp")
    monkeypatch.setattr(service, "regenerable_clean_env", empty.env)
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "DEFAULT_LOG_PATH", tmp_path / "reclaim.log")
    monkeypatch.setattr(cli, "assert_not_elevated", lambda: None)

    code = cli.main(
        ["auto-clean", "--apply", "--notify", "--scheduled", "--config", str(config_on)]
    )

    assert code == 0 and toast.calls == []


def _install_fake_windows_toasts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    shown: list[list[str]] = []

    class FakeToast:
        def __init__(self, text: list[str]) -> None:
            shown.append(text)

    class FakeToaster:
        def __init__(self, _name: str, _aumid: str | None = None) -> None:
            pass

        def show_toast(self, _toast: object) -> None:
            return None

    module = types.ModuleType("windows_toasts")
    module.Toast = FakeToast  # type: ignore[attr-defined]
    module.InteractableWindowsToaster = FakeToaster  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "windows_toasts", module)
    return shown


def test_send_autoclean_toast_has_exact_title_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = _install_fake_windows_toasts(monkeypatch)

    assert notifications.send_autoclean_toast(8_589_934_592, 61.4, 0) is True

    assert shown == [["Reclaim cleaned your disk", "Freed 8.0 GB, C: now 61% used."]]


def test_send_autoclean_toast_omits_usage_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    shown = _install_fake_windows_toasts(monkeypatch)

    notifications.send_autoclean_toast(2048, None, 3)

    assert shown == [["Reclaim cleaned your disk", "Freed 2.0 KB."]]


def test_send_autoclean_toast_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("windows_toasts")  # lacks the expected names -> ImportError
    monkeypatch.setitem(sys.modules, "windows_toasts", module)

    assert notifications.send_autoclean_toast(1, 1.0, 0) is False
