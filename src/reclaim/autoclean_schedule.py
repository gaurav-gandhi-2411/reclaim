from __future__ import annotations

# ADR-0034: the per-account weekly Task Scheduler entry that runs `reclaim auto-clean --apply
# --notify --scheduled` (the regenerable safe tier ONLY -- see reclaim.regenerable).
#
# Mirrors the installer's disk-space task (packaging/reclaim.iss, RegisterDiskSpaceTask): the name
# embeds the Windows username so two accounts on one machine never collide, the task is registered
# with `schtasks /create /xml` (a plain `/tr` caps at 261 chars and has no WorkingDirectory) and
# that XML MUST be real UTF-16LE bytes with a BOM (live-reproduced in the AY1 audit: a UTF-8 file,
# with or without a BOM, is rejected by schtasks), InteractiveToken logon + no RunLevel element =
# per-user, least privilege, never elevated. Unlike the installer it is registered/unregistered at
# runtime by the Settings toggle, so every schtasks call here is an injectable seam and every call
# appends its exit code + captured output to the same `task_registration_diagnostic.log` the
# installer writes -- a failure is never swallowed.
import contextlib
import getpass
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import structlog

from reclaim.app_paths import compiled_exe_dir, data_root

logger = structlog.get_logger(__name__)

TASK_NAME_PREFIX = "Reclaim Weekly Auto-Clean"
TASK_ARGUMENTS = "auto-clean --apply --notify --scheduled"
TASK_DESCRIPTION = (
    "Reclaim: weekly clean of provably-regenerable caches only (package-manager caches, old "
    "temp files, crash dumps, closed-browser caches). Never touches documents, the Recycle Bin "
    "or the quarantine vault; see PRIVACY.md."
)
EXE_NAME = "reclaim.exe"
DIAGNOSTIC_LOG_NAME = "task_registration_diagnostic.log"

# Fixed Sunday in the past, like the disk task's fixed StartBoundary: Task Scheduler derives the
# next occurrence from the weekly pattern, and StartWhenAvailable catches up a missed run.
_START_BOUNDARY = "2026-01-04T10:00:00"


class AutoCleanScheduleError(RuntimeError):
    """Base class: the scheduled task could not be created/removed/queried."""


class NotAnInstalledBuildError(AutoCleanScheduleError):
    """Raised when the task would have to point at `reclaim.exe` but this is a source/dev run."""


class TaskRegistrationError(AutoCleanScheduleError):
    """schtasks returned a non-zero exit code for create/delete."""

    def __init__(self, message: str, *, returncode: int, output: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output


@dataclass(frozen=True, slots=True)
class SchtasksOutcome:
    returncode: int
    output: str


SchtasksRunner = Callable[[Sequence[str]], SchtasksOutcome]


@dataclass(frozen=True, slots=True)
class TaskStatus:
    task_name: str
    registered: bool
    # Raw text as schtasks printed it (locale-formatted); None when absent/"N/A".
    state: str | None = None
    last_run_time: str | None = None
    last_result: int | None = None
    next_run_time: str | None = None


def task_name(username: str | None = None) -> str:
    """`Reclaim Weekly Auto-Clean (<username>)`, same per-account shape as the disk task."""
    return f"{TASK_NAME_PREFIX} ({username if username is not None else getpass.getuser()})"


def build_task_xml(exe_path: str, workdir: str, task_description: str = TASK_DESCRIPTION) -> bytes:
    """The task definition as the exact bytes `schtasks /create /xml` accepts: UTF-16LE with a
    BOM. Every interpolated value is XML-escaped (a username/path may contain `&`)."""
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        "  <RegistrationInfo>\r\n"
        f"    <Description>{escape(task_description)}</Description>\r\n"
        "  </RegistrationInfo>\r\n"
        "  <Triggers>\r\n"
        "    <CalendarTrigger>\r\n"
        f"      <StartBoundary>{_START_BOUNDARY}</StartBoundary>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <ScheduleByWeek>\r\n"
        "        <DaysOfWeek><Sunday /></DaysOfWeek>\r\n"
        "        <WeeksInterval>1</WeeksInterval>\r\n"
        "      </ScheduleByWeek>\r\n"
        "    </CalendarTrigger>\r\n"
        "  </Triggers>\r\n"
        "  <Principals>\r\n"
        '    <Principal id="Author">\r\n'
        "      <LogonType>InteractiveToken</LogonType>\r\n"
        "    </Principal>\r\n"
        "  </Principals>\r\n"
        "  <Settings>\r\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\r\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n"
        "    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>\r\n"
        "  </Settings>\r\n"
        '  <Actions Context="Author">\r\n'
        "    <Exec>\r\n"
        f"      <Command>{escape(exe_path)}</Command>\r\n"
        f"      <Arguments>{escape(TASK_ARGUMENTS)}</Arguments>\r\n"
        f"      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>\r\n"
        "    </Exec>\r\n"
        "  </Actions>\r\n"
        "</Task>"
    )
    return b"\xff\xfe" + xml.encode("utf-16-le")


def resolve_exe_path(exe_path: Path | None = None) -> Path:
    """The installed `reclaim.exe`, or the explicit path (tests). Source/dev runs have no exe to
    schedule, so they get an actionable error instead of a task pointing at nothing."""
    if exe_path is not None:
        return exe_path
    exe_dir = compiled_exe_dir()
    if exe_dir is None:
        raise NotAnInstalledBuildError(
            "Weekly auto-clean can only be scheduled from the installed Reclaim app "
            "(reclaim.exe). This is a source/development run with no executable to schedule -- "
            "install Reclaim, or run `reclaim auto-clean --apply` yourself."
        )
    return exe_dir / EXE_NAME


def _real_runner(argv: Sequence[str]) -> SchtasksOutcome:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed schtasks argv, shell=False
            ["schtasks.exe", *argv],  # noqa: S607 -- Windows system tool on PATH
            capture_output=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SchtasksOutcome(-1, f"{type(exc).__name__}: {exc}")
    # schtasks writes in the console's OEM code page; never let a decode error hide the result.
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("oem")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    return SchtasksOutcome(proc.returncode, text)


def default_diagnostic_log_path() -> Path:
    return data_root() / "data" / DIAGNOSTIC_LOG_NAME


def _run_logged(
    action: str, argv: Sequence[str], runner: SchtasksRunner, diag_log_path: Path
) -> SchtasksOutcome:
    outcome = runner(argv)
    logger.info("autoclean_schedule.schtasks", action=action, returncode=outcome.returncode)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        diag_log_path.parent.mkdir(parents=True, exist_ok=True)
        with diag_log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"Reclaim weekly auto-clean task -- {stamp}Z\n"
                f"ACTION: {action} (schtasks {' '.join(argv)})\n"
                f"EXIT CODE: {outcome.returncode}\n"
                f"Captured output:\n{outcome.output}\n\n"
            )
    except OSError:
        # The diagnostic file is secondary; the structured log still records the failure.
        logger.warning("autoclean_schedule.diag_write_failed", path=str(diag_log_path))
    return outcome


def register_task(
    *,
    exe_path: Path | None = None,
    username: str | None = None,
    runner: SchtasksRunner = _real_runner,
    diag_log_path: Path | None = None,
) -> str:
    """Creates (or overwrites, `/f`) this account's weekly task. Returns the task name.
    Raises `NotAnInstalledBuildError` (dev run, no explicit exe) or `TaskRegistrationError`."""
    exe = resolve_exe_path(exe_path)
    name = task_name(username)
    diag = diag_log_path if diag_log_path is not None else default_diagnostic_log_path()
    xml_bytes = build_task_xml(str(exe), str(exe.parent))
    fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="reclaim_autoclean_task_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(xml_bytes)
        outcome = _run_logged("create", ["/create", "/tn", name, "/xml", tmp, "/f"], runner, diag)
    finally:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
    if outcome.returncode != 0:
        raise TaskRegistrationError(
            f"Could not register the weekly auto-clean task (schtasks exit "
            f"{outcome.returncode}): {outcome.output.strip()[:300]}. Details: {diag}",
            returncode=outcome.returncode,
            output=outcome.output,
        )
    return name


def unregister_task(
    *,
    username: str | None = None,
    runner: SchtasksRunner = _real_runner,
    diag_log_path: Path | None = None,
) -> bool:
    """Deletes this account's task. Idempotent: returns False when there was nothing to delete,
    True when it was removed; raises `TaskRegistrationError` if it exists but cannot be removed."""
    name = task_name(username)
    diag = diag_log_path if diag_log_path is not None else default_diagnostic_log_path()
    outcome = _run_logged("delete", ["/delete", "/tn", name, "/f"], runner, diag)
    if outcome.returncode == 0:
        return True
    if not query_task(username=username, runner=runner, diag_log_path=diag).registered:
        return False
    raise TaskRegistrationError(
        f"Could not remove the weekly auto-clean task (schtasks exit {outcome.returncode}): "
        f"{outcome.output.strip()[:300]}. Details: {diag}",
        returncode=outcome.returncode,
        output=outcome.output,
    )


def _parse_list_output(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    return fields


def _none_if_na(value: str | None) -> str | None:
    return None if value is None or value in ("", "N/A") else value


def query_task(
    *,
    username: str | None = None,
    runner: SchtasksRunner = _real_runner,
    diag_log_path: Path | None = None,
) -> TaskStatus:
    """`schtasks /query /tn <name> /v /fo list`, parsed. A non-zero exit means "not registered".
    Field labels are the English ones schtasks prints; on another display language they parse to
    `None` rather than raising (the registered flag does not depend on them)."""
    name = task_name(username)
    diag = diag_log_path if diag_log_path is not None else default_diagnostic_log_path()
    outcome = _run_logged("query", ["/query", "/tn", name, "/v", "/fo", "list"], runner, diag)
    if outcome.returncode != 0:
        return TaskStatus(task_name=name, registered=False)
    fields = _parse_list_output(outcome.output)
    state = _none_if_na(fields.get("Status"))
    if fields.get("Scheduled Task State") == "Disabled":
        state = "Disabled"
    last_result: int | None
    try:
        last_result = int(fields["Last Result"])
    except (KeyError, ValueError):
        last_result = None
    return TaskStatus(
        task_name=name,
        registered=True,
        state=state,
        last_run_time=_none_if_na(fields.get("Last Run Time")),
        last_result=last_result,
        next_run_time=_none_if_na(fields.get("Next Run Time")),
    )
