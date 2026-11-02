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
import fnmatch
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    # pid -> every absolute path the process mentions (cwd, path-like command-line arguments),
    # normalised AND resolved (`..`, junctions, symlinks, local admin shares): once per scan.
    _mentions: dict[int, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)

    @property
    def usable(self) -> bool:
        return self.ok and not self.blocked

    def mentions(self, proc: ProcessSnapshot) -> tuple[str, ...]:
        cached = self._mentions.get(proc.pid)
        if cached is not None:
            return cached
        found: list[str] = []
        if proc.cwd:
            found += [normalize_path(proc.cwd), resolve_path(proc.cwd)]
        if proc.cmdline:
            for token in _split_args(proc.cmdline):
                for piece in _pieces(token):
                    if "\\" in piece or "/" in piece or ":" in piece:
                        # A relative argument means "relative to the process's own directory".
                        found += [normalize_path(piece), resolve_path(piece, proc.cwd)]
        unique = tuple(dict.fromkeys(x for x in found if x))
        self._mentions[proc.pid] = unique
        return unique

    def in_use(self, root: Path | str) -> list[str]:
        """Reasons `root` may be in use ([] = provably unused as far as this scan can see)."""
        reasons: list[str] = []
        if not self.ok:
            return [f"process inspection unavailable: {self.reason}"]
        for pid, name, why in self.blocked:
            reasons.append(f"cannot inspect same-user process {name} (pid {pid}): {why}")
        wanted = {w for w in (normalize_path(root), resolve_path(root)) if w}
        if not wanted:
            return ["empty path"]
        patterns = [re.compile(re.escape(w) + r"(?=$|[\\\s\"';,)])") for w in wanted]
        own_pid = current_pid()
        for proc in self.processes:
            cwd_forms = (
                [normalize_path(proc.cwd), resolve_path(proc.cwd)] if proc.cwd is not None else []
            )
            if any(_inside(form, w) for form in cwd_forms for w in wanted):
                reasons.append(f"{proc.name} (pid {proc.pid}) has its working directory inside")
                continue
            # The process doing the check names its own target: `reclaim apply <worktree>` has
            # the path on its command line. Counting that as "in use" refused every path given
            # to apply as its root (found on build 13, 1 Nov: 0/0 eligible). Only the command
            # line is skipped for our own pid; a cwd inside the path still counts (above). A
            # wrapper parent such as `cmd /c reclaim apply <path>` still refuses: fail-closed.
            if proc.cmdline is None or proc.pid == own_pid:
                continue
            cmd = normalize_path(proc.cmdline, expand_short=False)
            if any(pat.search(cmd) is not None for pat in patterns) or any(
                _inside(mentioned, w) for mentioned in self.mentions(proc) for w in wanted
            ):
                reasons.append(f"{proc.name} (pid {proc.pid}) has the path in its command line")
        return reasons


# Image-name globs of programs that can host user work (shells, interpreters, runtimes, editors and
# IDEs, VCS, terminals, agents). An UNREADABLE process matching one blocks even when it lives under
# Program Files. A review showed an exact-name list is too easy to miss (`python3.12.exe`,
# `pycharm64.exe`), hence globs.
_WORK_HOST_GLOBS = (
    "cmd*", "powershell*", "pwsh*", "python*", "py.exe", "pip*", "conda*", "mamba*", "uv.exe",
    "node*", "npm*", "npx*", "pnpm*", "yarn*", "bun*", "deno*", "tsc*", "vite*", "next*",
    "webpack*",
    "git*", "bash*", "sh.exe", "zsh*", "wsl*", "mintty*", "tmux*", "ssh*", "winpty*",
    "java*", "kotlin*", "gradle*", "mvn*", "dotnet*", "msbuild*", "devenv*", "go.exe", "gofmt*",
    "cargo*", "rustc*", "rust-analyzer*", "gcc*", "g++*", "clang*", "make*", "cmake*", "ninja*",
    "ruby*", "perl*", "php*", "lua*", "julia*", "rscript*", "r.exe", "jupyter*", "ipython*",
    "docker*", "podman*", "kubectl*", "terraform*",
    "code*", "cursor*", "windsurf*", "zed*", "claude*", "codex*", "aider*", "copilot*",
    "pycharm*", "idea*", "webstorm*", "clion*", "rider*", "goland*", "phpstorm*", "rubymine*",
    "datagrip*", "studio*", "sublime*", "notepad++*", "atom*", "brackets*", "eclipse*", "netbeans*",
    "vim*", "nvim*", "gvim*", "emacs*",
    "wt.exe", "windowsterminal*", "conhost*", "openconsole*", "alacritty*", "wezterm*", "kitty*",
    "hyper*", "terminus*", "reclaim*",
)  # fmt: skip


def _is_work_host(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, glob) for glob in _WORK_HOST_GLOBS)


def is_vendor_service(image_path: str | None, name: str) -> bool:
    """True for an unreadable same-user process that is a Windows or vendor service binary:
    its image lives under %SystemRoot%, Program Files (both) or ProgramData and its name is not a
    shell, interpreter, IDE or other tool that could be working inside a repository. Such a process
    (an OEM hotkey helper, `ctfmon`, a WMI host) did not start in an agent's worktree. Anything
    else unreadable -- an elevated terminal, a tool under the user's profile -- stays blocking.
    The environment variables that name the trusted roots must all be set and absolute: if they are
    not, nothing is trusted."""
    if not image_path or _is_work_host(name):
        return False
    if _is_work_host(os.path.basename(image_path)):  # noqa: PTH119 -- plain string
        return False
    roots = [
        os.environ.get(var, "")
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    ]
    image = normalize_path(image_path, expand_short=False)
    return any(
        root
        and len(normalize_path(root, expand_short=False)) > 3  # a drive root is never a vendor root
        and _inside(image, normalize_path(root, expand_short=False))
        for root in roots
    )


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
    text = _local_admin_share(_EXTENDED_PREFIX.sub("", str(value)).replace("/", "\\"))
    if expand_short and _IS_WINDOWS and "~" in text:
        text = _long_path_name(text) or text
    return text.casefold().rstrip("\\")


_ADMIN_SHARE_PREFIX = re.compile(
    r"^\\\\(?:\?\\UNC\\)?([^\\]+)\\([A-Za-z])\$(?=\\|$)", re.IGNORECASE
)


def _local_admin_share(text: str) -> str:
    """`\\\\localhost\\D$\\x` (and this machine's own name, 127.0.0.1) is just `D:\\x`."""
    match = _ADMIN_SHARE_PREFIX.match(text)
    if match is None:
        return text
    host = match.group(1).lower()
    local = {"localhost", "127.0.0.1", "[::1]", os.environ.get("COMPUTERNAME", "\0").lower()}
    if host not in local:
        return text
    return match.group(2) + ":" + text[match.end() :]


def resolve_path(value: Path | str, base: str | None = None) -> str:
    """`normalize_path` of the REAL location of `value`: `..` collapsed, junctions and symlinks
    followed, a local administrative share mapped to its drive. A relative `value` is relative to
    `base` (a process's own working directory), never to ours."""
    text = _local_admin_share(_EXTENDED_PREFIX.sub("", str(value)).replace("/", "\\"))
    try:
        if base and not Path(text).is_absolute():
            text = os.path.join(base, text)  # noqa: PTH118 -- plain strings
        return normalize_path(os.path.realpath(text))
    except (OSError, ValueError):
        return normalize_path(text)


def _pieces(token: str) -> list[str]:
    """A command-line argument and the parts that can carry a path: `--root=X`, `a;b`, `a,b`."""
    parts = [token]
    if "=" in token:
        parts.append(token.split("=", 1)[1])
    for sep in (";", ","):
        for part in list(parts):
            if sep in part:
                parts.extend(piece for piece in part.split(sep) if piece)
    return [p.strip("\"'") for p in parts if p]


def _split_args(cmdline: str) -> list[str]:
    """The arguments of a Windows command line, split the way the OS does (`CommandLineToArgvW`);
    a quote-aware regex when that is unavailable."""
    if _IS_WINDOWS:
        try:
            shell32 = ctypes.windll.shell32
            shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            count = ctypes.c_int(0)
            pointer = shell32.CommandLineToArgvW(cmdline, ctypes.byref(count))
            if pointer:
                try:
                    return [str(pointer[i]) for i in range(count.value)]
                finally:
                    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                    ctypes.windll.kernel32.LocalFree(pointer)
        except (AttributeError, OSError):
            pass
    return re.findall(r'"[^"]*"|\S+', cmdline)


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
