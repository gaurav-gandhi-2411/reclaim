"""Run a heavy job (build, full verify, soak, real-machine dry run) outside Claude Code's shells.

Why: Claude Code's low-memory reaper stops the process tree of a session's background shells when
the machine runs short of RAM. In the 2026-10 autonomous run it stopped every build and full verify
that way, even jobs still waiting for RAM. A job registered as a one-time per-user scheduled task
is started by the Task Scheduler service, so it has no ancestor in any Claude Code shell and a
reaper event (or the session ending) cannot reach it.

The job keeps its own watchdog instead:
- start gate: free RAM >= --start-ram-gb (default 8, the build floor; never lower it) for 3
  consecutive samples, and every disk floor met. Gives up after --wait-max-min.
- while running: every --sample-seconds it logs free RAM and disk space. It aborts if free RAM
  stays below --abort-ram-gb for 2 consecutive samples, or a disk floor is crossed.
- abort: the whole child tree is ended (a Job Object, with taskkill /T as a fallback). Then, in
  each --cleanup dir, only the top-level entries that did not exist when the job started are
  deleted. A cleanup dir outside CLEANUP_ROOT, or any new entry holding a reparse point
  (junction/symlink), is refused and left in place, so an abort can never delete through a link
  or touch anything the job did not create.

Usage (venv python; see packaging/DETACHED_JOBS.md):
  detached_job.py launch --name NAME [options] -- COMMAND [ARGS...]
  detached_job.py status NAME   # exit 0 done, 1 failed/aborted, 2 waiting/running, 3 stale
  detached_job.py forget NAME   # deletes the finished job's scheduled task (not its logs)
Each job writes to JOBS_ROOT\\NAME\\: spec.json, status.json (state + heartbeat), runner.log
(gate, samples, abort, END line), output.log (the command's stdout and stderr).
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

JOBS_ROOT = Path(r"D:\reclaim-build\jobs")
CLEANUP_ROOT = Path(r"D:\reclaim-build")
TASK_PREFIX = "ReclaimJob-"
TERMINAL = ("done", "failed", "aborted", "gave_up", "error")
ABORT_RAM_MIN_GB = 0.5  # below this the abort would fire only after the machine is starved
MAX_PERIOD_S = 60  # longest gap between two watchdog samples
# Absolute paths: a job must never run whatever "schtasks" happens to be first on PATH.
_SYSTEM32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
SCHTASKS = str(_SYSTEM32 / "schtasks.exe")
TASKKILL = str(_SYSTEM32 / "taskkill.exe")

# Windows constants (ctypes; no psutil in the venv).
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _now() -> dt.datetime:
    """Local time, timezone-aware (log lines and the task's boundaries are local)."""
    return dt.datetime.now(dt.UTC).astimezone().replace(microsecond=0)


def _say(msg: str, *, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


class _MemStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def free_ram_gb() -> float:
    m = _MemStatus()
    m.dwLength = ctypes.sizeof(_MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return float(m.ullAvailPhys) / 2**30


def disk_free_gb(drive: str) -> float:
    return shutil.disk_usage(drive.rstrip("\\") + "\\").free / 2**30


# ---- pure decision helpers (unit-tested) -------------------------------------------------------


def parse_disk_floors(items: list[str]) -> dict[str, float]:
    """['C:=20', 'D:=50'] -> {'C:': 20.0, 'D:': 50.0}. A malformed item is an error, not skipped."""
    floors: dict[str, float] = {}
    for item in items:
        drive, sep, gb = item.partition("=")
        drive = drive.strip().upper()
        if not sep or len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
            raise ValueError(f"disk floor must look like C:=20, got {item!r}")
        value = float(gb)
        if value < 0:
            raise ValueError(f"disk floor must be >= 0, got {item!r}")
        floors[drive] = value
    return floors


def disk_breaches(free: dict[str, float], floors: dict[str, float]) -> list[str]:
    """Drives below their floor. A drive whose free space could not be read counts as a breach."""
    out = []
    for drive, floor in floors.items():
        value = free.get(drive)
        if value is None or value < floor:
            out.append(drive)
    return out


def start_gate_step(ok_count: int, ram_gb: float, start_ram_gb: float, disks_ok: bool) -> int:
    """Consecutive good samples so far; the job starts at 3."""
    return ok_count + 1 if (ram_gb >= start_ram_gb and disks_ok) else 0


def low_ram_step(low_count: int, ram_gb: float, abort_ram_gb: float) -> int:
    """Consecutive low-RAM samples while running; the job aborts at 2."""
    return low_count + 1 if ram_gb < abort_ram_gb else 0


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def is_under(path: Path, root: Path) -> bool:
    return _real(path).is_relative_to(_real(root))


def holds_reparse_point(path: Path) -> bool:
    """True if path or anything below it is a junction/symlink. Unreadable counts as True."""
    try:
        if path.lstat().st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        if not path.is_dir():
            return False
        for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
            for name in dirnames + filenames:
                attrs = (Path(dirpath) / name).lstat().st_file_attributes
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
    except OSError:
        return True
    return False


def _raise(err: OSError) -> None:
    raise err


def plan_cleanup(cleanup_dir: Path, before: set[str], root: Path) -> tuple[list[Path], list[str]]:
    """Entries the job created in cleanup_dir (absent from `before`), and notes on refusals."""
    if not is_under(cleanup_dir, root) or _real(cleanup_dir) == _real(root):
        return [], [f"REFUSED {cleanup_dir}: not strictly inside {root}"]
    if not cleanup_dir.exists():
        return [], [f"nothing to clean: {cleanup_dir} does not exist"]
    notes: list[str] = []
    delete: list[Path] = []
    seen = {n.lower() for n in before}  # NTFS names are case-insensitive
    for entry in sorted(cleanup_dir.iterdir()):
        if entry.name.lower() in seen:
            continue
        if holds_reparse_point(entry):
            notes.append(f"REFUSED {entry}: holds a junction/symlink or is unreadable; kept")
            continue
        delete.append(entry)
    return delete, notes


# ---- job object ---------------------------------------------------------------------------------


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _ExtLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _job_for(process_handle: int) -> int | None:
    """A kill-on-close Job Object holding the process (and, from then on, its children)."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtLimit()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(
        ctypes.c_void_p(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(process_handle)):
        k32.CloseHandle(ctypes.c_void_p(job))
        return None
    return int(job)


# ---- spec validation (launch and run both check; a hand-edited spec cannot lower a floor) ------


def _finite(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def validate_spec(spec: object, jobs_root: Path, cleanup_root: Path) -> list[str]:
    """Problems with a job spec; empty means it may run. Types are checked before values, so a
    hand-edited spec.json fails closed instead of crashing the runner."""
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    nums = ("start_ram_gb", "abort_ram_gb", "wait_max_min", "gate_seconds", "sample_seconds")
    errs = [
        f"{key} must be a finite number, got {spec.get(key)!r}"
        for key in (*nums, "limit_hours")
        if not _finite(spec.get(key))
    ]
    cmd, cleanup, floors = spec.get("command"), spec.get("cleanup"), spec.get("disk_floors")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        errs.append("command must be a non-empty list of strings")
    if not isinstance(spec.get("cwd"), str):
        errs.append("cwd must be a string")
    if not isinstance(floors, dict) or not all(
        isinstance(k, str) and _finite(v) for k, v in floors.items()
    ):
        errs.append("disk_floors must map drives to finite numbers")
    if not isinstance(cleanup, list) or not all(isinstance(d, str) for d in cleanup):
        errs.append("cleanup must be a list of strings")
    if errs:
        return errs
    if spec["start_ram_gb"] < 8:
        errs.append("start_ram_gb may not be below the 8 GB build floor")
    if not ABORT_RAM_MIN_GB <= spec["abort_ram_gb"] < spec["start_ram_gb"]:
        errs.append(f"abort_ram_gb must be at least {ABORT_RAM_MIN_GB} and below start_ram_gb")
    # A longer period would leave the job effectively unwatched between samples.
    if (
        not 0 < spec["gate_seconds"] <= MAX_PERIOD_S
        or not 0 < spec["sample_seconds"] <= MAX_PERIOD_S
    ):
        errs.append(f"gate_seconds and sample_seconds must be above 0 and at most {MAX_PERIOD_S}")
    if not isinstance(spec["limit_hours"], int) or spec["limit_hours"] < 1:
        errs.append("limit_hours must be a whole number >= 1")
    elif not 0 < spec["wait_max_min"] < spec["limit_hours"] * 60:
        errs.append("wait_max_min must be above 0 and below limit_hours, or the task limit ends it")
    for d in spec["cleanup"]:  # a list of str, checked above
        errs.extend(cleanup_dir_problems(Path(d), jobs_root, cleanup_root))
    return errs


def cleanup_dir_problems(d: Path, jobs_root: Path, cleanup_root: Path) -> list[str]:
    """A cleanup dir must be absolute, strictly inside cleanup_root, not a link, and not the root,
    the jobs dir or an ancestor of either: an abort deletes whatever is new in it, so it must be
    a dir only this job writes to."""
    if not d.is_absolute():
        return [f"cleanup dir {d} must be an absolute path"]
    real = _real(d)
    if not is_under(d, cleanup_root) or real == _real(cleanup_root):
        return [f"cleanup dir {d} must be strictly inside {cleanup_root}"]
    if _real(jobs_root).is_relative_to(real) or real.is_relative_to(_real(jobs_root)):
        return [f"cleanup dir {d} may not contain or be inside {jobs_root}"]
    if d.exists() and d.lstat().st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return [f"cleanup dir {d} is a junction/symlink"]
    return []


def cleanup_dirs_already_there(dirs: list[str]) -> list[str]:
    """A cleanup dir must not exist before the job starts: the job creates it, so it is private
    to the job and an abort deleting what is new in it deletes only what this job made."""
    return [
        f"cleanup dir {d} already exists; use a new dir the job creates"
        for d in dirs
        if Path(d).exists()
    ]


# ---- runner (what the scheduled task executes) ---------------------------------------------------


class Runner:
    def __init__(self, job_dir: Path, cleanup_root: Path = CLEANUP_ROOT) -> None:
        self.dir = job_dir
        self.cleanup_root = cleanup_root
        self.spec: dict[str, Any] = {}
        self.status: dict[str, Any] = {"name": job_dir.name, "state": "starting"}

    def log(self, msg: str) -> None:
        with (
            contextlib.suppress(OSError),
            (self.dir / "runner.log").open("a", encoding="utf-8") as f,
        ):
            f.write(f"{_now().isoformat()} {msg}\n")

    def save(self, **fields: Any) -> None:
        """Never raises: a reader holding status.json open (Windows denies the replace) must not
        stop the watchdog. Retries, then logs and carries on; the heartbeat shows the gap."""
        self.status.update(fields, heartbeat=_now().isoformat())
        tmp = self.dir / "status.json.tmp"
        for attempt in range(10):
            try:
                tmp.write_text(json.dumps(self.status, indent=1), encoding="utf-8")
                tmp.replace(self.dir / "status.json")
            except OSError as e:
                if attempt == 9:
                    self.log(f"WARNING: status.json not written: {e}")
                time.sleep(0.2)
            else:
                return

    def sample(self) -> tuple[float, dict[str, float]]:
        free: dict[str, float] = {}
        for drive in self.spec["disk_floors"]:
            with contextlib.suppress(OSError):  # unread = missing = a breach (disk_breaches)
                free[drive] = disk_free_gb(drive)
        return free_ram_gb(), free

    def run(self) -> int:
        """Every path ends in finish(): an END line and a terminal state."""
        if (self.dir / "status.json").exists():  # a task re-run must not repeat the job
            self.log("REFUSED: this job already ran (status.json exists)")
            return 1
        try:
            self.spec = json.loads((self.dir / "spec.json").read_text(encoding="utf-8"))
            errs = validate_spec(self.spec, self.dir.parent, self.cleanup_root)
        except Exception as e:  # a bad spec ends in an END line, never a crash
            errs = [f"spec.json unusable: {type(e).__name__}: {e}"]
        if errs:
            return self.finish("error", None, "; ".join(errs))
        try:
            return self._run()
        except Exception as e:  # the END line and status must exist whatever happens
            return self.finish("error", None, f"{type(e).__name__}: {e}")

    def _run(self) -> int:
        s = self.spec
        floors: dict[str, float] = s["disk_floors"]
        self.log(f"runner pid={os.getpid()} spec={json.dumps(s)}")
        self.save(state="waiting", runner_pid=os.getpid())
        ok, start = 0, time.monotonic()
        while ok < 3:
            ram, free = self.sample()
            breaches = disk_breaches(free, floors)
            ok = start_gate_step(ok, ram, s["start_ram_gb"], not breaches)
            self.log(f"gate free_ram={ram:.2f}GB disks={_fmt(free)} breaches={breaches} ok={ok}")
            self.save(free_ram_gb=round(ram, 2))
            if ok >= 3:
                break
            if time.monotonic() - start > s["wait_max_min"] * 60:
                return self.finish("gave_up", None, "start gate never met")
            time.sleep(s["gate_seconds"])

        there = cleanup_dirs_already_there(s["cleanup"])
        if there:
            return self.finish("error", None, "; ".join(there))
        before: dict[str, list[str]] = {d: [] for d in s["cleanup"]}
        self.save(state="running")
        with (self.dir / "output.log").open("ab") as out:
            child = subprocess.Popen(  # noqa: S603 -- the command the launching session wrote
                s["command"],
                cwd=s["cwd"],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # A hidden console of its own, in its own process group: no Ctrl-C/close event
                # from any other console can reach the job.
                creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
            )
            job, rc, reason = None, -1, ""
            try:
                job = _job_for(int(child._handle))  # type: ignore[attr-defined]
                self.log(f"started child pid={child.pid} job_object={'yes' if job else 'NO'}")
                self.save(child_pid=child.pid, job_object=bool(job))
                if job is None:  # without it a dying runner would leave the job running unwatched
                    reason = "could not put the job in a Job Object"
                else:
                    rc, reason = self._watch(child, floors)
            except BaseException as e:
                reason = f"watchdog failed: {type(e).__name__}: {e}"
            if reason:
                self.log(f"ABORT: {reason}")
                dead = self.kill(child, job)
        if not reason:
            return self.finish("done" if rc == 0 else "failed", rc, "")
        if dead:
            try:
                self.cleanup({k: set(v) for k, v in before.items()})
            except Exception as e:  # still an abort: say so, with what failed
                self.log(f"cleanup FAILED: {type(e).__name__}: {e}")
        else:
            self.log("cleanup SKIPPED: the job is not confirmed dead, so its output may change")
        return self.finish("aborted", None, reason)

    def _watch(self, child: subprocess.Popen[bytes], floors: dict[str, float]) -> tuple[int, str]:
        """Sample until the child exits (its rc, "") or a floor is crossed (-1, the reason)."""
        s, low = self.spec, 0
        while True:
            with contextlib.suppress(subprocess.TimeoutExpired):
                return child.wait(timeout=s["sample_seconds"]), ""
            ram, free = self.sample()
            low = low_ram_step(low, ram, s["abort_ram_gb"])
            breaches = disk_breaches(free, floors)
            self.log(f"sample free_ram={ram:.2f}GB disks={_fmt(free)} low={low}")
            self.save(free_ram_gb=round(ram, 2))
            if low >= 2:
                return -1, f"free RAM {ram:.2f} GB < {s['abort_ram_gb']} GB for 2 samples"
            if breaches:
                return -1, f"disk floor crossed on {breaches} ({_fmt(free)})"

    def kill(self, child: subprocess.Popen[bytes], job: int | None) -> bool:
        """End the child tree; True once the child is confirmed gone."""
        if job:
            ctypes.windll.kernel32.TerminateJobObject(ctypes.c_void_p(job), 1)
        with contextlib.suppress(OSError):  # the wait below decides whether the kill worked
            subprocess.run(  # noqa: S603 -- fixed system binary, our own child's PID
                [TASKKILL, "/PID", str(child.pid), "/T", "/F"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.log("WARNING: child still alive 60 s after the kill")
            return False
        self.log(f"child tree ended (rc={child.returncode})")
        return True

    def cleanup(self, before: dict[str, set[str]]) -> None:
        for d, names in before.items():
            problems = cleanup_dir_problems(Path(d), self.dir.parent, self.cleanup_root)
            if problems:
                self.log(f"REFUSED cleanup of {d}: {problems}")
                continue
            delete, notes = plan_cleanup(Path(d), names, self.cleanup_root)
            for n in notes:
                self.log(n)
            self.log(f"partial output in {d}: {[p.name for p in delete]}")
            for p in delete:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                self.log(f"removed {p}" if not p.exists() else f"COULD NOT remove {p}")

    def finish(self, state: str, rc: int | None, reason: str) -> int:
        self.save(state=state, rc=rc, reason=reason, ended=_now().isoformat())
        self.log(f"END state={state} rc={rc} reason={reason or '-'}")
        return 0 if state == "done" else 1


def _fmt(free: dict[str, float]) -> str:
    return ",".join(f"{d}{v:.1f}GB" for d, v in sorted(free.items()))


# ---- launcher -----------------------------------------------------------------------------------

_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _x(value: str) -> str:
    return "".join(_ESC.get(c, c) for c in value)


def task_xml(name: str, python: str, script: str, job_dir: Path, limit_hours: int) -> str:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}".lstrip("\\")
    now = _now().replace(tzinfo=None)
    end = now + dt.timedelta(hours=limit_hours + 24)
    args = f'"{script}" run "{job_dir}"'
    desc = f"Reclaim detached job {name} (packaging/detached_job.py). Logs: {job_dir}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{_x(desc)}</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{now.isoformat()}</StartBoundary>
      <EndBoundary>{end.isoformat()}</EndBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_x(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <ExecutionTimeLimit>PT{limit_hours}H</ExecutionTimeLimit>
    <DeleteExpiredTaskAfter>P1D</DeleteExpiredTaskAfter>
    <Priority>6</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{_x(python)}</Command><Arguments>{_x(args)}</Arguments></Exec>
  </Actions>
</Task>
"""


def launch(a: argparse.Namespace) -> int:
    if not a.command:
        _say("error: give the command after --", err=True)
        return 2
    if not a.name.replace("-", "").replace("_", "").isalnum():
        _say("error: --name may hold only letters, digits, - and _", err=True)
        return 2
    spec = {
        "name": a.name,
        "command": a.command,
        "cwd": a.cwd,
        "start_ram_gb": a.start_ram_gb,
        "abort_ram_gb": a.abort_ram_gb,
        "disk_floors": {},
        "cleanup": a.cleanup,
        "wait_max_min": a.wait_max_min,
        "gate_seconds": a.gate_seconds,
        "sample_seconds": a.sample_seconds,
        "limit_hours": a.limit_hours,
    }
    try:
        spec["disk_floors"] = parse_disk_floors(a.disk)
    except ValueError as e:
        _say(f"error: {e}", err=True)
        return 2
    errs = validate_spec(spec, JOBS_ROOT, CLEANUP_ROOT) + cleanup_dirs_already_there(a.cleanup)
    if errs:
        for err in errs:
            _say(f"error: {err}", err=True)
        return 2
    job_dir = JOBS_ROOT / a.name
    if job_dir.exists():
        _say(f"error: {job_dir} exists; pick a new --name", err=True)
        return 2
    job_dir.mkdir(parents=True)
    (job_dir / "spec.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    # The base interpreter's pythonw.exe, not the venv's: the venv launcher starts a console
    # python.exe, and a console lets a Ctrl-C/close event end the runner (0xC000013A, seen in
    # the first proof run). The runner is stdlib-only, so it needs nothing from the venv.
    python = str(Path(sys.base_prefix) / "pythonw.exe")
    xml = task_xml(a.name, python, str(Path(__file__).resolve()), job_dir, a.limit_hours)
    fd, xml_path = tempfile.mkstemp(suffix=".xml", dir=job_dir)
    with os.fdopen(fd, "w", encoding="utf-16") as f:
        f.write(xml)
    task = TASK_PREFIX + a.name
    for cmd in (
        [SCHTASKS, "/Create", "/TN", task, "/XML", xml_path],
        [SCHTASKS, "/Run", "/TN", task],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if r.returncode != 0:
            _say(f"error: {' '.join(cmd)}: {r.stdout}{r.stderr}", err=True)
            return 1
    _say(f"launched task {task}; logs in {job_dir}")
    return 0


def status(a: argparse.Namespace) -> int:
    path = JOBS_ROOT / a.name / "status.json"
    if not path.exists():
        spec_path = JOBS_ROOT / a.name / "spec.json"
        if not spec_path.exists():
            _say(f"no job named {a.name}")
            return 1
        age = time.time() - spec_path.stat().st_mtime
        if age > stale_after_s(a.name):
            _say(f"never started: no status.json {round(age)} s after launch")
            return 3
        _say(f"no status yet ({path}); the task may not have started")
        return 2
    st = json.loads(path.read_text(encoding="utf-8"))
    age = (_now() - dt.datetime.fromisoformat(st["heartbeat"])).total_seconds()
    st["heartbeat_age_s"] = round(age)
    if st["state"] not in TERMINAL and age > stale_after_s(a.name):
        st["state"] = "stale"  # the runner stopped sampling: killed, logged off, rebooted
    _say(json.dumps(st, indent=1))
    if st["state"] in TERMINAL:
        return 0 if st["state"] == "done" else 1
    return 3 if st["state"] == "stale" else 2


def stale_after_s(name: str) -> float:
    """A live runner saves at least once per gate/sample period; allow 3 periods plus a minute."""
    spec = json.loads((JOBS_ROOT / name / "spec.json").read_text(encoding="utf-8"))
    return 3 * float(max(spec["gate_seconds"], spec["sample_seconds"])) + 60


def forget(a: argparse.Namespace) -> int:
    path = JOBS_ROOT / a.name / "status.json"
    st = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if st.get("state") not in TERMINAL:
        _say(f"refusing: job {a.name} is not finished (state={st.get('state')})", err=True)
        return 1
    r = subprocess.run(  # noqa: S603 -- fixed system binary, our own task name
        [SCHTASKS, "/Delete", "/TN", TASK_PREFIX + a.name, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    _say((r.stdout + r.stderr).strip())
    return r.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    la = sub.add_parser("launch")
    la.add_argument("--name", required=True)
    la.add_argument("--cwd", default=str(Path.cwd()))
    la.add_argument("--start-ram-gb", type=float, default=8.0)
    la.add_argument("--abort-ram-gb", type=float, default=2.0)
    la.add_argument("--disk", action="append", default=None, help="DRIVE=GB, repeatable")
    la.add_argument("--cleanup", action="append", default=[], help="dir under D:\\reclaim-build")
    la.add_argument("--wait-max-min", type=float, default=600)
    la.add_argument("--gate-seconds", type=float, default=20)
    la.add_argument("--sample-seconds", type=float, default=15)
    la.add_argument("--limit-hours", type=int, default=12)
    la.add_argument("command", nargs=argparse.REMAINDER)
    for name in ("status", "forget"):
        sub.add_parser(name).add_argument("name")
    sub.add_parser("run").add_argument("job_dir")
    a = p.parse_args(argv)
    if a.cmd == "launch":
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
        if a.disk is None:
            a.disk = ["C:=20", "D:=50"]
        return launch(a)
    if a.cmd == "status":
        return status(a)
    if a.cmd == "forget":
        return forget(a)
    return Runner(Path(a.job_dir)).run()


if __name__ == "__main__":
    sys.exit(main())
