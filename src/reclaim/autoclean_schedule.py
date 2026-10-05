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
import json
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
# ADR-0034 addendum: the second trigger. uv holds a SHARED lock on its cache for the whole life of
# any `uv run`, so on a busy workstation the Sunday run never gets the exclusive lock `uv cache
# prune` needs. Shortly after sign-in no session has started `uv` yet; the scheduled invocation
# is cheap there (see reclaim.autoclean_state: a no-op unless something is pending or overdue).
_LOGON_DELAY = "PT3M"


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


class TaskQueryError(AutoCleanScheduleError):
    """The typed Task Scheduler query failed or returned something unparseable."""


@dataclass(frozen=True, slots=True)
class SchtasksOutcome:
    returncode: int
    output: str


SchtasksRunner = Callable[[Sequence[str]], SchtasksOutcome]
# (powershell script, task name) -> outcome. The name travels out-of-band (environment
# variable), never inside the script text.
PowerShellRunner = Callable[[str, str], SchtasksOutcome]


@dataclass(frozen=True, slots=True)
class TaskStatus:
    task_name: str
    registered: bool
    # State is the ScheduledTasks TaskState enum NAME (Ready/Running/Disabled/...); times are
    # ISO-8601 with offset (None = never run / not scheduled); all locale-independent.
    state: str | None = None
    last_run_time: str | None = None
    last_result: int | None = None
    next_run_time: str | None = None


def task_name(username: str | None = None) -> str:
    """`Reclaim Weekly Auto-Clean (<username>)`, same per-account shape as the disk task."""
    return f"{TASK_NAME_PREFIX} ({username if username is not None else getpass.getuser()})"


def current_user_id() -> str:
    """`DOMAIN\\user` of the signed-in account (the LogonTrigger's UserId), or the bare username
    when no domain is set."""
    domain = os.environ.get("USERDOMAIN")
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def build_task_xml(
    exe_path: str,
    workdir: str,
    task_description: str = TASK_DESCRIPTION,
    user_id: str | None = None,
) -> bytes:
    """The task definition as the exact bytes `schtasks /create /xml` accepts: UTF-16LE with a
    BOM. Every interpolated value is XML-escaped (a username/path may contain `&`)."""
    logon_user = user_id if user_id is not None else current_user_id()
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
        "    <LogonTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        f"      <UserId>{escape(logon_user)}</UserId>\r\n"
        f"      <Delay>{_LOGON_DELAY}</Delay>\r\n"
        "    </LogonTrigger>\r\n"
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
        # 45 min: `uv cache prune` may wait up to 30 min for uv's cache lock before it runs.
        "    <ExecutionTimeLimit>PT45M</ExecutionTimeLimit>\r\n"
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


def run_schtasks(argv: Sequence[str]) -> SchtasksOutcome:
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


def run_powershell(script: str, name: str) -> SchtasksOutcome:
    """Runs `script` in the stock Windows PowerShell 5.1 (`powershell.exe`, present on every
    Windows 10/11 SKU; `pwsh` 7 is an optional install). The task name is passed in the
    RECLAIM_TASK_NAME environment variable, never in the command line."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, shell=False, name via env var
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
            capture_output=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "RECLAIM_TASK_NAME": name},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SchtasksOutcome(-1, f"{type(exc).__name__}: {exc}")
    # The script forces UTF-8 stdout; stderr (error text) is console-encoded, so decode leniently.
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return SchtasksOutcome(proc.returncode, text)


def default_diagnostic_log_path() -> Path:
    return data_root() / "data" / DIAGNOSTIC_LOG_NAME


def _run_logged(
    action: str, argv: Sequence[str], runner: SchtasksRunner, diag_log_path: Path
) -> SchtasksOutcome:
    outcome = runner(argv)
    logger.info("autoclean_schedule.schtasks", action=action, returncode=outcome.returncode)
    _append_diag(action, f"schtasks {' '.join(argv)}", outcome, diag_log_path)
    return outcome


def _append_diag(action: str, command: str, outcome: SchtasksOutcome, diag_log_path: Path) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        diag_log_path.parent.mkdir(parents=True, exist_ok=True)
        with diag_log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"Reclaim weekly auto-clean task -- {stamp}Z\n"
                f"ACTION: {action} ({command})\n"
                f"EXIT CODE: {outcome.returncode}\n"
                f"Captured output:\n{outcome.output}\n\n"
            )
    except OSError:
        # The diagnostic file is secondary; the structured log still records the failure.
        logger.warning("autoclean_schedule.diag_write_failed", path=str(diag_log_path))


def register_task(
    *,
    exe_path: Path | None = None,
    username: str | None = None,
    runner: SchtasksRunner = run_schtasks,
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
    runner: SchtasksRunner = run_schtasks,
    query_runner: PowerShellRunner = run_powershell,
    diag_log_path: Path | None = None,
) -> bool:
    """Deletes this account's task. Idempotent: returns False when there was nothing to delete,
    True when it was removed; raises `TaskRegistrationError` if it exists but cannot be removed."""
    name = task_name(username)
    diag = diag_log_path if diag_log_path is not None else default_diagnostic_log_path()
    outcome = _run_logged("delete", ["/delete", "/tn", name, "/f"], runner, diag)
    if outcome.returncode == 0:
        return True
    if not query_task(username=username, runner=query_runner, diag_log_path=diag).registered:
        return False
    raise TaskRegistrationError(
        f"Could not remove the weekly auto-clean task (schtasks exit {outcome.returncode}): "
        f"{outcome.output.strip()[:300]}. Details: {diag}",
        returncode=outcome.returncode,
        output=outcome.output,
    )


# Typed query (replaces parsing `schtasks /query /v` text, whose labels and date format are
# localized -- the same locale bug class as the dd-MM date slip). The ScheduledTasks module returns
# real objects: State is an enum (we emit its NAME), times are DateTime (we emit ISO-8601 "o",
# which is culture-invariant), LastTaskResult is an int. The task name arrives via the
# RECLAIM_TASK_NAME environment variable, never interpolated into the script, so a username with
# quotes/`$`/backticks cannot alter the command. `Where-Object -ceq` instead of `-TaskName` because
# -TaskName treats `[`/`*`/`?` as wildcards. Absent task = {"registered": false}, not an error.
QUERY_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$n = $env:RECLAIM_TASK_NAME; "
    "$t = Get-ScheduledTask -TaskPath '\\' | Where-Object { $_.TaskName -ceq $n } "
    "| Select-Object -First 1; "
    "if (-not $t) { '{\"registered\":false}'; exit 0 }; "
    "$i = Get-ScheduledTaskInfo -InputObject $t; "
    "function Iso($d) { if ($null -eq $d) { $null } else { ([datetime]$d).ToString('o') } }; "
    "[ordered]@{ registered = $true; state = [string]$t.State; "
    "last_run_time = (Iso $i.LastRunTime); next_run_time = (Iso $i.NextRunTime); "
    "last_result = [int]$i.LastTaskResult } | ConvertTo-Json -Compress"
)
# Windows reports "never ran" as 1999-11-30 (or 0001-01-01 / 1601); nothing real predates 2000.
_NEVER_RAN_BEFORE_YEAR = 2000


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"time field is not a string: {value!r}")
    parsed = datetime.fromisoformat(value)
    return None if parsed.year < _NEVER_RAN_BEFORE_YEAR else value


def _status_from_data(name: str, data: object) -> TaskStatus:
    if not isinstance(data, dict) or not isinstance(data.get("registered"), bool):
        raise TypeError("missing boolean 'registered'")
    if not data["registered"]:
        return TaskStatus(task_name=name, registered=False)
    state = data["state"]
    last_result = data["last_result"]
    if not isinstance(state, str) or isinstance(last_result, bool):
        raise TypeError("bad 'state'/'last_result' type")
    if not isinstance(last_result, int):
        raise TypeError("bad 'last_result' type")
    return TaskStatus(
        task_name=name,
        registered=True,
        state=state,
        last_run_time=_iso_or_none(data["last_run_time"]),
        last_result=last_result,
        next_run_time=_iso_or_none(data["next_run_time"]),
    )


def parse_query_json(name: str, text: str) -> TaskStatus:
    """The one JSON object `QUERY_SCRIPT` prints -> `TaskStatus`. Raises `TaskQueryError` on
    anything that is not exactly that shape (never a bare KeyError/JSONDecodeError)."""
    try:
        return _status_from_data(name, json.loads(text.strip().lstrip("\ufeff")))
    except (ValueError, TypeError, KeyError) as exc:  # JSONDecodeError is a ValueError
        raise TaskQueryError(
            f"Could not read the scheduled task's status ({type(exc).__name__}: {exc}); "
            f"output began: {text.strip()[:200]!r}"
        ) from exc


def query_task(
    *,
    username: str | None = None,
    runner: PowerShellRunner = run_powershell,
    diag_log_path: Path | None = None,
) -> TaskStatus:
    """Typed, locale-independent status via PowerShell's ScheduledTasks module. A task that is
    not registered is `registered=False`; a failed/garbled query raises `TaskQueryError`."""
    name = task_name(username)
    diag = diag_log_path if diag_log_path is not None else default_diagnostic_log_path()
    outcome = runner(QUERY_SCRIPT, name)
    logger.info("autoclean_schedule.query", returncode=outcome.returncode)
    if outcome.returncode != 0:
        _append_diag("query", "powershell Get-ScheduledTask", outcome, diag)
        raise TaskQueryError(
            f"Could not query the weekly auto-clean task (powershell exit "
            f"{outcome.returncode}): {outcome.output.strip()[:300]}. Details: {diag}"
        )
    return parse_query_json(name, outcome.output)
