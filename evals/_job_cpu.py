from __future__ import annotations

# CPU time of a whole process tree, for the cold-start budget eval (D4).
#
# Run as a script: `python _job_cpu.py TRIALS CMD [ARGS...]`. Prints one JSON object with each
# trial's `cpu_ms` and `wall_ms`. This process puts itself in a new Job Object, so every process
# it starts (and theirs) is accounted to the job. A console-script launcher such as the venv's
# `reclaim.exe` starts the real interpreter as a child, so `GetProcessTimes` on the direct child
# would see only the launcher (it read ~23 ms for a ~630 ms import graph).
#
# CPU time, unlike wall time, barely moves when other processes load the machine. Measured on the
# dev machine (D4): 16 busy-loop processes moved wall time 801 -> 5,361 ms (6.7x) and the tree's
# CPU time 633 -> 766 ms (1.21x).
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes

_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def main(argv: list[str]) -> int:
    trials, cmd = int(argv[0]), argv[1:]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    job = kernel32.CreateJobObjectW(None, None)
    if not job or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "could not put the measuring process in a job")

    def job_cpu_ms() -> float:
        info = _BasicAccounting()
        if not kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return (info.TotalUserTime + info.TotalKernelTime) / 10_000  # 100 ns units -> ms

    runs = []
    for _ in range(trials):
        before = job_cpu_ms()
        start = time.perf_counter()
        # DEVNULL, not capture_output: no reader threads in this process during the run, so the
        # job's CPU delta is the child tree's alone.
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)  # noqa: S603
        runs.append(
            {
                "wall_ms": (time.perf_counter() - start) * 1000,
                "cpu_ms": job_cpu_ms() - before,
            }
        )
    print(json.dumps({"runs": runs}))  # noqa: T201 -- the script's output
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
