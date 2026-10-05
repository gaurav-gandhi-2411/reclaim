from __future__ import annotations

import json
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from reclaim import autoclean_schedule as sched
from reclaim.autoclean_schedule import (
    NotAnInstalledBuildError,
    SchtasksOutcome,
    TaskQueryError,
    TaskRegistrationError,
    build_task_xml,
    query_task,
    register_task,
    task_name,
    unregister_task,
)

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

_NEXT = "2026-10-04T10:00:00.0000000+05:30"
_NEVER = "1999-11-30T00:00:00.0000000+05:30"


def _ps_json(**overrides: object) -> str:
    """What QUERY_SCRIPT prints for a registered, never-run task, with fields overridden."""
    data: dict[str, object] = {
        "registered": True,
        "state": "Ready",
        "last_run_time": _NEVER,
        "next_run_time": _NEXT,
        "last_result": 267011,
    }
    data.update(overrides)
    return json.dumps(data)


class FakePowerShell:
    """Injectable PowerShell runner: records (script, name) and replays one outcome."""

    def __init__(self, outcome: SchtasksOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    def __call__(self, script: str, name: str) -> SchtasksOutcome:
        self.calls.append((script, name))
        return self.outcome


class FakeSchtasks:
    """Records argv and replays scripted outcomes keyed by the first argument."""

    def __init__(self, **outcomes: SchtasksOutcome) -> None:
        self.calls: list[list[str]] = []
        self.outcomes = outcomes
        self.xml_seen: bytes | None = None

    def __call__(self, argv: Sequence[str]) -> SchtasksOutcome:
        self.calls.append(list(argv))
        if argv[0] == "/create":
            self.xml_seen = Path(argv[argv.index("/xml") + 1]).read_bytes()
        return self.outcomes.get(argv[0].lstrip("/"), SchtasksOutcome(0, "SUCCESS"))


def test_task_name_embeds_username() -> None:
    assert task_name("bob") == "Reclaim Weekly Auto-Clean (bob)"


def test_task_xml_is_utf16le_with_bom_and_least_privilege() -> None:
    raw = build_task_xml(r"C:\Apps\reclaim.exe", r"C:\Apps")

    assert raw[:2] == b"\xff\xfe"
    text = raw.decode("utf-16")  # BOM-aware decode must succeed
    assert raw[2:].decode("utf-16-le") == text
    assert "<LogonType>InteractiveToken</LogonType>" in text
    assert "HighestAvailable" not in text and "<RunLevel>" not in text
    assert "<ScheduleByWeek>" in text and "<Sunday />" in text
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in text
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in text
    assert "<ExecutionTimeLimit>PT45M</ExecutionTimeLimit>" in text
    root = ET.fromstring(text.split("?>", 1)[1])  # noqa: S314 -- our own XML
    exe = root.find(".//t:Exec", _NS)
    assert exe is not None
    assert exe.findtext("t:Command", namespaces=_NS) == r"C:\Apps\reclaim.exe"
    assert exe.findtext("t:Arguments", namespaces=_NS) == "auto-clean --apply --notify --scheduled"
    assert exe.findtext("t:WorkingDirectory", namespaces=_NS) == r"C:\Apps"


def test_task_xml_has_weekly_and_delayed_logon_triggers_for_the_current_user() -> None:
    raw = build_task_xml(r"C:\Apps\reclaim.exe", r"C:\Apps", user_id=r"HOST\bob")

    # Still real UTF-16LE with a BOM and still well-formed (schtasks rejects anything else).
    assert raw[:2] == b"\xff\xfe"
    text = raw.decode("utf-16")
    assert raw[2:].decode("utf-16-le") == text
    root = ET.fromstring(text.split("?>", 1)[1])  # noqa: S314 -- our own XML
    triggers = root.find("t:Triggers", _NS)
    assert triggers is not None
    assert [child.tag.split("}")[1] for child in triggers] == ["CalendarTrigger", "LogonTrigger"]
    weekly = triggers.find("t:CalendarTrigger", _NS)
    assert weekly is not None and weekly.find(".//t:Sunday", _NS) is not None
    logon = triggers.find("t:LogonTrigger", _NS)
    assert logon is not None
    assert logon.findtext("t:Enabled", namespaces=_NS) == "true"
    assert logon.findtext("t:UserId", namespaces=_NS) == r"HOST\bob"
    assert logon.findtext("t:Delay", namespaces=_NS) == "PT3M"
    # Unchanged guarantees: catch-up, least privilege, the 45 min limit was NOT raised.
    assert root.findtext(".//t:StartWhenAvailable", namespaces=_NS) == "true"
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=_NS) == "PT45M"
    assert root.find(".//t:RunLevel", _NS) is None


def test_task_xml_escapes_the_logon_user_id() -> None:
    text = build_task_xml(r"C:\a.exe", "C:\\", user_id=r"R&D\o<b>").decode("utf-16")

    root = ET.fromstring(text.split("?>", 1)[1])  # noqa: S314 -- our own XML
    assert root.findtext(".//t:LogonTrigger/t:UserId", namespaces=_NS) == r"R&D\o<b>"


def test_register_task_sends_both_triggers_to_schtasks(tmp_path: Path) -> None:
    captured: list[bytes] = []

    def runner(argv: Sequence[str]) -> SchtasksOutcome:
        captured.append(Path(argv[list(argv).index("/xml") + 1]).read_bytes())
        return SchtasksOutcome(0, "SUCCESS")

    register_task(
        exe_path=Path(r"C:\Apps\reclaim.exe"),
        username="bob",
        runner=runner,
        diag_log_path=tmp_path / "d.log",
    )

    text = captured[0].decode("utf-16")
    assert "<CalendarTrigger>" in text and "<LogonTrigger>" in text


def test_task_xml_escapes_paths() -> None:
    text = build_task_xml(r"C:\R&D <x>\reclaim.exe", r"C:\R&D <x>").decode("utf-16")

    assert "R&amp;D &lt;x&gt;" in text
    root = ET.fromstring(text.split("?>", 1)[1])  # noqa: S314 -- our own XML
    assert root.findtext(".//t:Command", namespaces=_NS) == r"C:\R&D <x>\reclaim.exe"


def test_register_without_exe_in_dev_run_raises_actionable_error(tmp_path: Path) -> None:
    fake = FakeSchtasks()

    with pytest.raises(NotAnInstalledBuildError, match="installed Reclaim app"):
        register_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log")

    assert fake.calls == []


def test_register_runs_schtasks_create_and_logs_diagnostics(tmp_path: Path) -> None:
    fake = FakeSchtasks()
    diag = tmp_path / "diag.log"

    name = register_task(
        exe_path=Path(r"C:\Apps\reclaim.exe"), username="bob", runner=fake, diag_log_path=diag
    )

    assert name == "Reclaim Weekly Auto-Clean (bob)"
    argv = fake.calls[0]
    assert argv[:3] == ["/create", "/tn", name] and argv[-1] == "/f"
    assert fake.xml_seen is not None and fake.xml_seen[:2] == b"\xff\xfe"
    assert not Path(argv[argv.index("/xml") + 1]).exists(), "temp XML is cleaned up"
    logged = diag.read_text(encoding="utf-8")
    assert "EXIT CODE: 0" in logged and "ACTION: create" in logged


def test_register_failure_is_raised_and_logged_never_swallowed(tmp_path: Path) -> None:
    fake = FakeSchtasks(create=SchtasksOutcome(1, "ERROR: Access is denied."))
    diag = tmp_path / "diag.log"

    with pytest.raises(TaskRegistrationError, match="Access is denied") as info:
        register_task(
            exe_path=Path(r"C:\Apps\reclaim.exe"), username="bob", runner=fake, diag_log_path=diag
        )

    assert info.value.returncode == 1
    assert "EXIT CODE: 1" in diag.read_text(encoding="utf-8")


def test_unregister_is_idempotent_when_task_absent(tmp_path: Path) -> None:
    fake = FakeSchtasks(
        delete=SchtasksOutcome(1, "ERROR: The system cannot find the file specified."),
    )
    ps = FakePowerShell(SchtasksOutcome(0, '{"registered":false}'))

    assert (
        unregister_task(
            username="bob", runner=fake, query_runner=ps, diag_log_path=tmp_path / "d.log"
        )
        is False
    )


def test_unregister_raises_when_task_exists_but_delete_fails(tmp_path: Path) -> None:
    fake = FakeSchtasks(delete=SchtasksOutcome(1, "ERROR: Access is denied."))
    ps = FakePowerShell(SchtasksOutcome(0, _ps_json()))

    with pytest.raises(TaskRegistrationError):
        unregister_task(
            username="bob", runner=fake, query_runner=ps, diag_log_path=tmp_path / "d.log"
        )


def test_unregister_returns_true_when_deleted(tmp_path: Path) -> None:
    fake = FakeSchtasks()
    ps = FakePowerShell(SchtasksOutcome(1, "must not be queried"))

    assert (
        unregister_task(
            username="bob", runner=fake, query_runner=ps, diag_log_path=tmp_path / "d.log"
        )
        is True
    )
    assert ps.calls == []


def test_query_registered_ready_with_real_dates(tmp_path: Path) -> None:
    ps = FakePowerShell(
        SchtasksOutcome(0, _ps_json(last_run_time="2026-09-27T10:00:01.5+05:30", last_result=0))
    )

    status = query_task(username="bob", runner=ps, diag_log_path=tmp_path / "d.log")

    assert status.registered is True
    assert status.state == "Ready"
    assert status.last_run_time == "2026-09-27T10:00:01.5+05:30"
    assert status.last_result == 0
    assert status.next_run_time == _NEXT
    assert ps.calls[0][1] == "Reclaim Weekly Auto-Clean (bob)"


@pytest.mark.parametrize(
    "sentinel", [_NEVER, "0001-01-01T00:00:00.0000000", "1601-01-01T00:00:00Z"]
)
def test_query_maps_never_run_sentinels_to_none(sentinel: str, tmp_path: Path) -> None:
    ps = FakePowerShell(
        SchtasksOutcome(0, _ps_json(last_run_time=sentinel, next_run_time=sentinel))
    )

    status = query_task(username="bob", runner=ps, diag_log_path=tmp_path / "d.log")

    assert status.registered is True
    assert status.last_run_time is None and status.next_run_time is None


def test_query_null_times_stay_none(tmp_path: Path) -> None:
    ps = FakePowerShell(SchtasksOutcome(0, _ps_json(last_run_time=None, next_run_time=None)))

    status = query_task(username="bob", runner=ps, diag_log_path=tmp_path / "d.log")

    assert status.last_run_time is None and status.next_run_time is None


def test_query_missing_task_is_registered_false_not_an_error(tmp_path: Path) -> None:
    ps = FakePowerShell(SchtasksOutcome(0, '{"registered":false}'))

    status = query_task(username="bob", runner=ps, diag_log_path=tmp_path / "d.log")

    assert status.registered is False and status.state is None


def test_query_reports_disabled_state(tmp_path: Path) -> None:
    ps = FakePowerShell(SchtasksOutcome(0, _ps_json(state="Disabled", next_run_time=None)))

    status = query_task(username="bob", runner=ps, diag_log_path=tmp_path / "d.log")

    assert status.state == "Disabled" and status.next_run_time is None


@pytest.mark.parametrize(
    "outcome",
    [
        SchtasksOutcome(1, "Get-ScheduledTask : Access is denied"),
        SchtasksOutcome(-1, "OSError: powershell.exe not found"),
        SchtasksOutcome(0, "this is not json"),
        SchtasksOutcome(0, ""),
        SchtasksOutcome(0, "[1, 2]"),
        SchtasksOutcome(0, '{"registered": "yes"}'),
        SchtasksOutcome(0, '{"registered": true}'),
        SchtasksOutcome(0, _ps_json(last_result="0")),
        SchtasksOutcome(0, _ps_json(next_run_time="not a date")),
    ],
)
def test_query_failure_or_garbage_is_a_typed_error(
    outcome: SchtasksOutcome, tmp_path: Path
) -> None:
    with pytest.raises(TaskQueryError):
        query_task(username="bob", runner=FakePowerShell(outcome), diag_log_path=tmp_path / "d.log")


def test_query_failure_is_written_to_the_diagnostic_log(tmp_path: Path) -> None:
    diag = tmp_path / "d.log"
    with pytest.raises(TaskQueryError):
        query_task(
            username="bob",
            runner=FakePowerShell(SchtasksOutcome(1, "Access is denied")),
            diag_log_path=diag,
        )

    assert "ACTION: query" in diag.read_text(encoding="utf-8")


def test_hostile_task_name_never_enters_the_script(tmp_path: Path) -> None:
    ps = FakePowerShell(SchtasksOutcome(0, '{"registered":false}'))
    hostile = 'O\'Brien $x `y` "z"'

    query_task(username=hostile, runner=ps, diag_log_path=tmp_path / "d.log")

    script, name = ps.calls[0]
    assert name == f"Reclaim Weekly Auto-Clean ({hostile})"
    assert hostile not in script and "O'Brien" not in script
    assert "$env:RECLAIM_TASK_NAME" in script


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler is Windows-only")
def test_real_query_of_hostile_unregistered_name_is_registered_false(tmp_path: Path) -> None:
    status = query_task(
        username="Reclaim-pytest (O'Brien $x) [a*]", diag_log_path=tmp_path / "d.log"
    )

    assert status.registered is False


def test_default_diag_path_is_under_data_root() -> None:
    assert sched.default_diagnostic_log_path().name == "task_registration_diagnostic.log"
    assert sched.default_diagnostic_log_path().parent.name == "data"


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler is Windows-only")
def test_real_task_scheduler_round_trip(tmp_path: Path) -> None:
    """Registers a throwaway task (unique name, harmless exe), queries it, removes it, and
    proves it is gone -- try/finally so a failure can never leave a task behind."""
    user = f"pytest-{uuid.uuid4().hex[:10]}"
    exe = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "whoami.exe"
    diag = tmp_path / "diag.log"
    try:
        name = register_task(exe_path=exe, username=user, diag_log_path=diag)
        assert name == f"Reclaim Weekly Auto-Clean ({user})"
        status = query_task(username=user, diag_log_path=diag)
        assert status.registered is True
        assert status.state == "Ready"
        assert status.last_run_time is None  # never ran: sentinel mapped to None
        assert status.next_run_time is not None
        assert datetime.fromisoformat(status.next_run_time) > datetime.now().astimezone()
        assert unregister_task(username=user, diag_log_path=diag) is True
        assert query_task(username=user, diag_log_path=diag).registered is False
    finally:
        unregister_task(username=user, diag_log_path=diag)
    assert query_task(username=user, diag_log_path=diag).registered is False
