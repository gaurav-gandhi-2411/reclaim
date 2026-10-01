from __future__ import annotations

import os
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

from reclaim import autoclean_schedule as sched
from reclaim.autoclean_schedule import (
    NotAnInstalledBuildError,
    SchtasksOutcome,
    TaskRegistrationError,
    build_task_xml,
    query_task,
    register_task,
    task_name,
    unregister_task,
)

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

_QUERY_OUTPUT = """
Folder: \\
HostName:                             HOST
TaskName:                             \\Reclaim Weekly Auto-Clean (bob)
Next Run Time:                        10/4/2026 10:00:00 AM
Status:                               Ready
Last Run Time:                        N/A
Last Result:                          267011
Task To Run:                          C:\\Apps\\reclaim.exe auto-clean --apply
Scheduled Task State:                 Enabled
"""


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
    assert "<ExecutionTimeLimit>PT30M</ExecutionTimeLimit>" in text
    root = ET.fromstring(text.split("?>", 1)[1])  # noqa: S314 -- our own XML
    exe = root.find(".//t:Exec", _NS)
    assert exe is not None
    assert exe.findtext("t:Command", namespaces=_NS) == r"C:\Apps\reclaim.exe"
    assert exe.findtext("t:Arguments", namespaces=_NS) == "auto-clean --apply --notify --scheduled"
    assert exe.findtext("t:WorkingDirectory", namespaces=_NS) == r"C:\Apps"


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
        query=SchtasksOutcome(1, "ERROR: The system cannot find the file specified."),
    )

    assert unregister_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log") is False


def test_unregister_raises_when_task_exists_but_delete_fails(tmp_path: Path) -> None:
    fake = FakeSchtasks(
        delete=SchtasksOutcome(1, "ERROR: Access is denied."),
        query=SchtasksOutcome(0, _QUERY_OUTPUT),
    )

    with pytest.raises(TaskRegistrationError):
        unregister_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log")


def test_unregister_returns_true_when_deleted(tmp_path: Path) -> None:
    fake = FakeSchtasks()

    assert unregister_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log") is True


def test_query_parses_verbose_list_output(tmp_path: Path) -> None:
    fake = FakeSchtasks(query=SchtasksOutcome(0, _QUERY_OUTPUT))

    status = query_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log")

    assert status.registered is True
    assert status.state == "Ready"
    assert status.last_run_time is None  # "N/A"
    assert status.last_result == 267011
    assert status.next_run_time == "10/4/2026 10:00:00 AM"
    assert fake.calls[0] == [
        "/query",
        "/tn",
        "Reclaim Weekly Auto-Clean (bob)",
        "/v",
        "/fo",
        "list",
    ]


def test_query_reports_disabled_and_missing(tmp_path: Path) -> None:
    disabled = _QUERY_OUTPUT.replace(
        "Scheduled Task State:                 Enabled",
        "Scheduled Task State:                 Disabled",
    )
    fake = FakeSchtasks(query=SchtasksOutcome(0, disabled))
    assert query_task(username="bob", runner=fake, diag_log_path=tmp_path / "d.log").state == (
        "Disabled"
    )

    missing = FakeSchtasks(query=SchtasksOutcome(1, "ERROR: not found"))
    status = query_task(username="bob", runner=missing, diag_log_path=tmp_path / "d.log")
    assert status.registered is False and status.state is None


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
        assert status.next_run_time is not None
        assert unregister_task(username=user, diag_log_path=diag) is True
        assert query_task(username=user, diag_log_path=diag).registered is False
    finally:
        unregister_task(username=user, diag_log_path=diag)
    assert query_task(username=user, diag_log_path=diag).registered is False
