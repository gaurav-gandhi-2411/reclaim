"""Read-only inspection of the current user's running processes: working directory and command
line of each, without `psutil` (spec docs/specs/agent-artifacts.md, W5/S4).

Used to answer one question: "is any live process using this directory?". The answer is a
POSITIVE finding of use, and anything that cannot be inspected fails closed:

- A process of ANOTHER user, or a system/protected process whose owner cannot be read, is out of
  scope (it is not an agent of this user); they are only counted.
- A process of the CURRENT user whose working directory or command line cannot be read (an
  elevated process of the same user, for example) goes into `blocked`: while that list is
  non-empty, no directory can be proven unused (`ProcessScan.in_use` reports it for every path).
- On a platform or Python build where the Win32 API is not usable, `ok` is False and the same
  rule applies.

The layered defence that does not depend on this module: moving a directory into the vault is a
same-volume `os.rename`, which Windows refuses while any process holds the directory (its
working directory) or a file inside it open without delete sharing.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# Access rights / constants (winnt.h, tlhelp32.h)
_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_PROCESS_BASIC_INFORMATION = 0
_PROCESS_WOW64_INFORMATION = 26
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_MAX_UNICODE_STRING_BYTES = 64 * 1024  # a command line cannot legally exceed 32767 UTF-16 units


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    pid: int
    name: str
    cwd: str | None
    cmdline: str | None


@dataclass(frozen=True, slots=True)
class ProcessScan:
    """`processes`: same-user processes that were fully read. `blocked`: same-user processes (or
    the whole scan, when `ok` is False) that could NOT be read -- each a `(pid, name, reason)`."""

    ok: bool
    processes: tuple[ProcessSnapshot, ...]
    blocked: tuple[tuple[int, str, str], ...]
    other_or_system: int
    reason: str = ""
    # Same-user processes that could not be read but whose image is a Windows / vendor service
    # binary (see `is_vendor_service`): reported, not blocking. `(pid, name, image path)`.
    set_aside: tuple[tuple[int, str, str], ...] = ()

    @property
    def usable(self) -> bool:
        return self.ok and not self.blocked

    def in_use(self, root: Path | str) -> list[str]:
        """Reasons `root` may be in use ([] = provably unused as far as this scan can see)."""
        reasons: list[str] = []
        if not self.ok:
            return [f"process inspection unavailable: {self.reason}"]
        for pid, name, why in self.blocked:
            reasons.append(f"cannot inspect same-user process {name} (pid {pid}): {why}")
        wanted = normalize_path(root)
        if not wanted:
            return ["empty path"]
        pattern = re.compile(re.escape(wanted) + r"(?=$|[\\\s\"';,)])")
        for proc in self.processes:
            if proc.cwd is not None and _inside(normalize_path(proc.cwd), wanted):
                reasons.append(f"{proc.name} (pid {proc.pid}) has its working directory inside")
                continue
            if proc.cmdline is None:
                continue
            cmd = normalize_path(proc.cmdline, expand_short=False)
            if pattern.search(cmd) is not None or any(
                _inside(normalize_path(tok), wanted) for tok in _path_tokens(proc.cmdline)
            ):
                reasons.append(f"{proc.name} (pid {proc.pid}) has the path in its command line")
        return reasons


# Image names that can host user work (shells, interpreters, runtimes, editors, VCS, agents).
# An UNREADABLE process with one of these names blocks even when it lives under Program Files.
_WORK_HOSTS = frozenset(
    {
        "cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "py.exe",
        "node.exe", "git.exe", "bash.exe", "sh.exe", "wsl.exe", "wslhost.exe", "java.exe",
        "javaw.exe", "dotnet.exe", "go.exe", "cargo.exe", "rustc.exe", "docker.exe", "npm.exe",
        "pnpm.exe", "yarn.exe", "code.exe", "cursor.exe", "claude.exe", "wt.exe",
        "windowsterminal.exe", "conhost.exe", "openconsole.exe", "msbuild.exe", "gcc.exe",
        "make.exe", "ruby.exe", "perl.exe", "php.exe", "deno.exe", "bun.exe", "uv.exe",
        "ssh.exe", "winpty-agent.exe", "reclaim.exe",
    }
)  # fmt: skip


def is_vendor_service(image_path: str | None, name: str) -> bool:
    """True for an unreadable same-user process that is a Windows or vendor service binary:
    its image lives under %SystemRoot%, Program Files (both) or ProgramData and its name is not a
    shell, interpreter or other tool that could be working inside a repository. Such a process
    (an OEM hotkey helper, `ctfmon`, a WMI host) did not start in an agent's worktree. Anything
    else unreadable -- an elevated terminal, a tool under the user's profile -- stays blocking."""
    if not image_path or name.lower() in _WORK_HOSTS:
        return False
    if os.path.basename(image_path).lower() in _WORK_HOSTS:  # noqa: PTH119 -- plain string
        return False
    roots = [
        os.environ.get(var, "")
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    ]
    image = normalize_path(image_path, expand_short=False)
    return any(root and _inside(image, normalize_path(root, expand_short=False)) for root in roots)


def _inside(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + "\\")


_EXTENDED_PREFIX = re.compile(r"\\\\\?\\(?!UNC\\)", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"(?:\\\\\?\\)?[A-Za-z]:[\\/][^\"'<>|?*\r\n]*")


def _path_tokens(cmdline: str) -> list[str]:
    """Drive-letter path-like fragments of a command line, each cut at a quote or `=`/`;`
    boundary variants so `--dir=C:\\x` and `"C:\\x y\\z"` both yield their path."""
    tokens: list[str] = []
    for match in _DRIVE_PATH.finditer(cmdline):
        text = match.group(0)
        tokens.append(text)
        tokens.extend(part for part in re.split(r"[;,\s]", text) if part and part != text)
    return tokens


def normalize_path(value: Path | str, *, expand_short: bool = True) -> str:
    """Case-folded, backslash-only, no `\\\\?\\` prefix, no trailing slash; 8.3 components
    expanded when the path exists (`GetLongPathNameW` works on a path, not on a raw substring)."""
    # Every extended-length prefix, wherever it sits (a command line can carry several), except
    # the `\\?\UNC\` form, which names a network path.
    text = _EXTENDED_PREFIX.sub("", str(value)).replace("/", "\\")
    if expand_short and _IS_WINDOWS and "~" in text:
        text = _long_path_name(text) or text
    return text.casefold().rstrip("\\")


def _long_path_name(text: str) -> str | None:
    """`GetLongPathNameW`, applied to the longest EXISTING prefix (it fails for a path whose last
    component does not exist, which is the usual case for a file named on a command line)."""
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    head, tail = text, ""
    while head:
        buf = ctypes.create_unicode_buffer(32768)
        n = kernel32.GetLongPathNameW(head, buf, len(buf))
        if 0 < n < len(buf):
            return buf.value + tail
        parent, sep, last = head.rpartition("\\")
        if not sep or not parent:
            return None
        head, tail = parent, "\\" + last + tail
    return None


# --- Win32 plumbing ------------------------------------------------------------------------


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_void_p),
        ("BasePriority", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


def _read_memory(kernel32: ctypes.WinDLL, handle: int, address: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(
        ctypes.c_void_p(handle), ctypes.c_void_p(address), buf, size, ctypes.byref(got)
    ):
        raise OSError(f"ReadProcessMemory failed ({ctypes.get_last_error()})")
    return buf.raw[: got.value]


def _read_unicode_string(
    kernel32: ctypes.WinDLL, handle: int, params: int, offset: int, *, wow64: bool
) -> str:
    if wow64:  # UNICODE_STRING32: Length u16, MaximumLength u16, Buffer u32
        raw = _read_memory(kernel32, handle, params + offset, 8)
        length = int.from_bytes(raw[0:2], "little")
        buffer = int.from_bytes(raw[4:8], "little")
    else:  # UNICODE_STRING (x64): Length u16, MaximumLength u16, pad, Buffer u64
        raw = _read_memory(kernel32, handle, params + offset, 16)
        length = int.from_bytes(raw[0:2], "little")
        buffer = int.from_bytes(raw[8:16], "little")
    if length > _MAX_UNICODE_STRING_BYTES:
        raise OSError("implausible string length")
    if length == 0 or buffer == 0:
        return ""
    return _read_memory(kernel32, handle, buffer, length).decode("utf-16-le", errors="replace")


def _own_sid(kernel32: ctypes.WinDLL, advapi32: ctypes.WinDLL) -> bytes:
    handle = kernel32.GetCurrentProcess()
    return _token_sid(advapi32, kernel32, handle)


def _token_sid(advapi32: ctypes.WinDLL, kernel32: ctypes.WinDLL, process_handle: int) -> bytes:
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        ctypes.c_void_p(process_handle), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError("OpenProcessToken failed")
    try:
        size = ctypes.c_uint32()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
            raise OSError("GetTokenInformation failed")
        # TOKEN_USER = { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes } }, the SID follows.
        sid_ptr = int.from_bytes(buf.raw[: ctypes.sizeof(ctypes.c_void_p)], "little")
        sid_len = advapi32.GetLengthSid(ctypes.c_void_p(sid_ptr))
        return ctypes.string_at(sid_ptr, sid_len)
    finally:
        kernel32.CloseHandle(token)


def _read_process(
    kernel32: ctypes.WinDLL, ntdll: ctypes.WinDLL, pid: int
) -> tuple[str | None, str | None, str]:
    """`(cwd, cmdline, error)`; error is "" on success."""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None, None, f"OpenProcess failed ({ctypes.get_last_error()})"
    try:
        wow64_peb = ctypes.c_void_p()
        status = ntdll.NtQueryInformationProcess(
            ctypes.c_void_p(handle),
            _PROCESS_WOW64_INFORMATION,
            ctypes.byref(wow64_peb),
            ctypes.sizeof(wow64_peb),
            None,
        )
        wow64 = status == 0 and bool(wow64_peb.value)
        if wow64:
            peb = wow64_peb.value or 0
            params = int.from_bytes(_read_memory(kernel32, handle, peb + 0x10, 4), "little")
            cwd = _read_unicode_string(kernel32, handle, params, 0x24, wow64=True)
            cmdline = _read_unicode_string(kernel32, handle, params, 0x40, wow64=True)
        else:
            pbi = _ProcessBasicInformation()
            status = ntdll.NtQueryInformationProcess(
                ctypes.c_void_p(handle),
                _PROCESS_BASIC_INFORMATION,
                ctypes.byref(pbi),
                ctypes.sizeof(pbi),
                None,
            )
            if status != 0 or not pbi.PebBaseAddress:
                return None, None, f"NtQueryInformationProcess failed ({status})"
            params = int.from_bytes(
                _read_memory(kernel32, handle, pbi.PebBaseAddress + 0x20, 8), "little"
            )
            cwd = _read_unicode_string(kernel32, handle, params, 0x38, wow64=False)
            cmdline = _read_unicode_string(kernel32, handle, params, 0x70, wow64=False)
    except OSError as exc:
        return None, None, str(exc)
    else:
        return cwd, cmdline, ""
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def scan_processes() -> ProcessScan:
    """Snapshot of the current user's processes. Never raises: any failure is `ok=False`."""
    if not _IS_WINDOWS or ctypes.sizeof(ctypes.c_void_p) != 8:
        return ProcessScan(False, (), (), 0, "needs 64-bit Python on Windows")
    try:
        return _scan_windows()
    except Exception as exc:  # fail closed on anything unexpected
        return ProcessScan(False, (), (), 0, f"{type(exc).__name__}: {exc}")


def _scan_windows() -> ProcessScan:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    advapi32.GetLengthSid.restype = ctypes.c_uint32
    own = _own_sid(kernel32, advapi32)
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, _INVALID_HANDLE):
        raise OSError("CreateToolhelp32Snapshot failed")
    entries: list[tuple[int, str]] = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while more:
            entries.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            more = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))

    processes: list[ProcessSnapshot] = []
    blocked: list[tuple[int, str, str]] = []
    set_aside: list[tuple[int, str, str]] = []
    other = 0
    for pid, name in entries:
        if pid in (0, 4):  # System Idle Process / System: no user-visible working directory
            other += 1
            continue
        limited = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not limited:
            other += 1  # protected or other-user process: owner unreadable, out of scope
            continue
        try:
            try:
                sid = _token_sid(advapi32, kernel32, limited)
            except OSError:
                other += 1
                continue
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(limited))
        if sid != own:
            other += 1
            continue
        cwd, cmdline, error = _read_process(kernel32, ntdll, pid)
        if error:
            image = _image_path(kernel32, pid)
            if is_vendor_service(image, name):
                set_aside.append((pid, name, image or ""))
            else:
                blocked.append((pid, name, f"{error}; image {image or 'unknown'}"))
        else:
            processes.append(ProcessSnapshot(pid, name, cwd, cmdline))
    return ProcessScan(True, tuple(processes), tuple(blocked), other, "", tuple(set_aside))


def _image_path(kernel32: ctypes.WinDLL, pid: int) -> str | None:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)
        )
        return buf.value if ok else None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def fake_scan(
    snapshots: Sequence[ProcessSnapshot] = (), blocked: Sequence[tuple[int, str, str]] = ()
) -> ProcessScan:
    """A `ProcessScan` built by hand, for tests of the callers."""
    return ProcessScan(True, tuple(snapshots), tuple(blocked), 0)


def current_pid() -> int:
    return os.getpid()
