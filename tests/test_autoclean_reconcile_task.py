from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

from reclaim import autoclean_schedule as sched
from reclaim import cli
from reclaim.autoclean_schedule import SchtasksOutcome

# ADR-0034 'Upgrade path': `reclaim auto-clean --reconcile-task` is what the installer runs on
# every install/upgrade. No test here touches the real Task Scheduler: schtasks is an injected fake.

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
_ISS = Path(__file__).resolve().parents[1] / "packaging" / "reclaim.iss"
_EXE = Path(r"C:\Users\someone\AppData\Local\Programs\Reclaim\reclaim.exe")

# The task as it was registered before ADR-0034's addendum: the weekly trigger only.
_OLD_SINGLE_TRIGGER_XML = (
    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>'
    "<CalendarTrigger><StartBoundary>2026-01-04T10:00:00</StartBoundary></CalendarTrigger>"
    "</Triggers></Task>"
)


class FakeTaskScheduler:
    """Holds one task's XML like Task Scheduler would; records every schtasks argv."""

    def __init__(
        self, existing_xml: str | None = None, create_result: SchtasksOutcome | None = None
    ) -> None:
        self.stored_xml = existing_xml
        self.calls: list[list[str]] = []
        self.created_xml: list[bytes] = []
        self.create_result = create_result or SchtasksOutcome(0, "SUCCESS")

    def __call__(self, argv: Sequence[str]) -> SchtasksOutcome:
        self.calls.append(list(argv))
        if argv[0] == "/create":
            raw = Path(argv[argv.index("/xml") + 1]).read_bytes()
            self.created_xml.append(raw)
            if self.create_result.returncode == 0:
                self.stored_xml = raw.decode("utf-16")
        return self.create_result


def _config(tmp_path: Path, *, enabled: bool) -> argparse.Namespace:
    path = tmp_path / "config.toml"
    path.write_text(f"[autoclean]\nenabled = {str(enabled).lower()}\n", encoding="utf-8")
    return argparse.Namespace(config=path)


def _run(
    args: argparse.Namespace, fake: FakeTaskScheduler, tmp_path: Path, *, exe: Path | None = _EXE
) -> int:
    return cli._run_reconcile_autoclean_task(
        args, exe_path=exe, runner=fake, diag_log_path=tmp_path / "diag.log"
    )


def _trigger_tags(raw: bytes) -> set[str]:
    root = ET.fromstring(raw.decode("utf-16"))  # noqa: S314 -- our own generated XML
    triggers = root.find("t:Triggers", _NS)
    assert triggers is not None
    return {child.tag.split("}")[1] for child in triggers}


def test_enabled_upgrades_old_single_trigger_task_to_both_triggers(tmp_path: Path) -> None:
    fake = FakeTaskScheduler(existing_xml=_OLD_SINGLE_TRIGGER_XML)
    assert "LogonTrigger" not in str(fake.stored_xml)

    code = _run(_config(tmp_path, enabled=True), fake, tmp_path)

    assert code == 0
    creates = [c for c in fake.calls if c[0] == "/create"]
    assert len(creates) == 1
    assert "/f" in creates[0]  # overwrite, so the old definition is replaced
    assert _trigger_tags(fake.created_xml[0]) == {"CalendarTrigger", "LogonTrigger"}
    assert "LogonTrigger" in str(fake.stored_xml)


def test_disabled_makes_no_schtasks_call_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTaskScheduler(existing_xml=_OLD_SINGLE_TRIGGER_XML)

    code = _run(_config(tmp_path, enabled=False), fake, tmp_path)

    assert code == 0
    assert fake.calls == []
    assert fake.stored_xml == _OLD_SINGLE_TRIGGER_XML  # an old task is left exactly as it was
    assert "nothing to do" in capsys.readouterr().out


def test_missing_config_counts_as_disabled(tmp_path: Path) -> None:
    fake = FakeTaskScheduler()
    args = argparse.Namespace(config=tmp_path / "does_not_exist.toml")

    assert _run(args, fake, tmp_path) == 0
    assert fake.calls == []


def test_not_an_installed_build_prints_message_and_does_not_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sched, "compiled_exe_dir", lambda: None)
    fake = FakeTaskScheduler()

    code = _run(_config(tmp_path, enabled=True), fake, tmp_path, exe=None)

    assert code == 0  # a source run has nothing to schedule: not a failure
    assert fake.calls == []
    assert "installed Reclaim app" in capsys.readouterr().err


def test_reconcile_twice_issues_identical_create_calls(tmp_path: Path) -> None:
    fake = FakeTaskScheduler()
    args = _config(tmp_path, enabled=True)

    assert _run(args, fake, tmp_path) == 0
    assert _run(args, fake, tmp_path) == 0

    assert len(fake.created_xml) == 2
    assert fake.created_xml[0] == fake.created_xml[1]
    creates = [c for c in fake.calls if c[0] == "/create"]
    # argv differs only in the temp XML path; everything else (name, /f) is identical.
    stripped = [[a for a in c if not a.endswith(".xml")] for c in creates]
    assert stripped[0] == stripped[1]


def test_registration_failure_exits_nonzero_with_actionable_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTaskScheduler(create_result=SchtasksOutcome(1, "ERROR: Access is denied."))

    code = _run(_config(tmp_path, enabled=True), fake, tmp_path)

    assert code == 1
    err = capsys.readouterr().err
    assert "could not update the weekly task" in err
    assert "Access is denied" in err
    assert str(tmp_path / "diag.log") in err  # points at the diagnostic log
    assert "ERROR: Access is denied." in (tmp_path / "diag.log").read_text(encoding="utf-8")


def test_main_routes_reconcile_flag_without_running_a_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTaskScheduler()
    monkeypatch.setattr(sched, "run_schtasks", fake)
    monkeypatch.setattr(sched, "compiled_exe_dir", lambda: _EXE.parent)
    monkeypatch.setattr(sched, "default_diagnostic_log_path", lambda: tmp_path / "diag.log")
    config = _config(tmp_path, enabled=True).config

    assert cli.main(["auto-clean", "--reconcile-task", "--config", str(config)]) == 0
    assert [c[0] for c in fake.calls] == ["/create"]


def test_reconcile_conflicts_with_apply() -> None:
    with pytest.raises(SystemExit):
        cli.main(["auto-clean", "--reconcile-task", "--apply"])


def _iss_run_entries() -> list[str]:
    text = _ISS.read_text(encoding="utf-8")
    section = re.search(r"^\[Run\]\s*$(.*?)(?=^\[)", text, re.MULTILINE | re.DOTALL)
    assert section is not None, "[Run] section missing from packaging/reclaim.iss"
    return [
        line
        for line in section.group(1).splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


def test_installer_run_entry_reconciles_task_as_original_user_and_ignores_failure() -> None:
    entries = [e for e in _iss_run_entries() if "--reconcile-task" in e]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.startswith('Filename: "{app}\\{#MyAppExeName}"')
    assert 'Parameters: "auto-clean --reconcile-task"' in entry
    assert 'WorkingDir: "{app}"' in entry
    flags = re.search(r"Flags:\s*([^;]+)", entry)
    assert flags is not None
    flag_set = set(flags.group(1).split())
    assert {"runasoriginaluser", "runhidden", "nowait", "skipifdoesntexist"} <= flag_set
    # Must run on every install/upgrade: not an opt-in checkbox, not skipped on silent installs.
    assert not flag_set & {"postinstall", "skipifsilent", "unchecked"}
