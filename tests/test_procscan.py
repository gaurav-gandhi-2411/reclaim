"""procscan: a live process using a directory is found by working directory and by command line,
in every spelling; anything that cannot be inspected fails closed."""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_fixtures import make_junction

from reclaim import procscan
from reclaim.procscan import ProcessSnapshot, fake_scan, normalize_path, scan_processes

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 process inspection")

BS = chr(92)  # a single backslash, spelled out so no tool or editor can swallow it


@contextlib.contextmanager
def _sleeper(cwd: Path, *args: str) -> Iterator[subprocess.Popen[bytes]]:
    """A child that sleeps in `cwd`. Yields only once a scan SEES it there: right after `Popen`
    returns the interpreter may not have set up its process parameters yet (a race that made
    these tests flaky), and the venv launcher stub in front of it reports the parent's cwd."""
    marker = f"--rc-marker={uuid.uuid4().hex}"
    proc = subprocess.Popen(  # noqa: S603 -- our own interpreter, fixed code
        [sys.executable, "-c", "import time; time.sleep(120)", marker, *args], cwd=cwd
    )
    try:
        wanted = normalize_path(cwd)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            scan = scan_processes()
            if any(
                s.cmdline is not None
                and marker in s.cmdline
                and s.cwd is not None
                and normalize_path(s.cwd) == wanted
                for s in scan.processes
            ):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                "the child process never became visible with its working directory"
            )
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=30)


def _reasons_for(pid: int, root: Path | str) -> list[str]:
    """Reasons `root` is in use. NOT filtered by `pid`: `sys.executable` here is a venv launcher
    stub, and the interpreter that actually holds the directory is its child with another pid."""
    del pid
    scan = scan_processes()
    assert scan.ok, scan.reason
    return [r for r in scan.in_use(root) if "cannot inspect" not in r]


def test_working_directory_inside_is_found(tmp_path: Path) -> None:
    wt = tmp_path / "wt-1"
    (wt / "sub").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with _sleeper(wt / "sub") as proc:
        assert _reasons_for(proc.pid, wt)  # cwd is a descendant of the root
        assert not _reasons_for(proc.pid, other)


def test_boundary_wt_1_is_not_wt_10(tmp_path: Path) -> None:
    wt10 = tmp_path / "wt-10"
    wt10.mkdir()
    with _sleeper(wt10) as proc:
        assert _reasons_for(proc.pid, tmp_path / "wt-10")
        assert not _reasons_for(proc.pid, tmp_path / "wt-1")


@pytest.mark.parametrize("spelling", ["plain", "forward", "upper", "extended", "quoted_args"])
def test_command_line_path_in_every_spelling(tmp_path: Path, spelling: str) -> None:
    wt = tmp_path / "wt-cmd"
    (wt / "src").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    target = str(wt / "src" / "app.js")
    shown = {
        "plain": target,
        "forward": target.replace(BS, "/"),
        "upper": target.upper(),
        "extended": BS + BS + "?" + BS + target,
        "quoted_args": "--dir=" + target,
    }[spelling]
    with _sleeper(other, shown) as proc:
        assert _reasons_for(proc.pid, wt), spelling


def test_boundary_in_command_line(tmp_path: Path) -> None:
    (tmp_path / "wt-10").mkdir()
    (tmp_path / "wt-1").mkdir()
    with _sleeper(tmp_path, str(tmp_path / "wt-10" / "x")) as proc:
        assert not _reasons_for(proc.pid, tmp_path / "wt-1")
        assert _reasons_for(proc.pid, tmp_path / "wt-10")


def test_short_8_3_spelling_is_expanded(tmp_path: Path) -> None:
    long_dir = tmp_path / "a long directory name"
    long_dir.mkdir()
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, len(buf))  # type: ignore[attr-defined]
    short = buf.value
    if not n or short.lower() == str(long_dir).lower():
        pytest.skip("8.3 name generation is disabled on this volume")
    other = tmp_path / "elsewhere"
    other.mkdir()
    with _sleeper(other, short + BS + "x.txt") as proc:
        assert _reasons_for(proc.pid, long_dir)


def test_normalize_path_is_case_slash_prefix_and_trailing_insensitive() -> None:
    a = normalize_path(BS + BS + "?" + BS + "C:" + BS + "Users" + BS + "X" + BS)
    b = normalize_path("c:/users/x")
    assert a == b == "c:" + BS + "users" + BS + "x"
    unc = normalize_path(BS + BS + "?" + BS + "UNC" + BS + "srv" + BS + "share")
    assert unc.startswith(BS + BS + "?" + BS + "unc")  # a UNC form keeps its prefix


def test_unreadable_same_user_process_fails_closed() -> None:
    scan = fake_scan(blocked=[(4242, "elevated.exe", "OpenProcess failed (5)")])
    assert not scan.usable
    reasons = scan.in_use("C:" + BS + "anything")
    assert any("cannot inspect" in r and "4242" in r for r in reasons)


def test_unavailable_platform_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procscan, "_IS_WINDOWS", False)
    scan = scan_processes()
    assert not scan.ok and not scan.usable
    assert scan.in_use("C:" + BS + "x")[0].startswith("process inspection unavailable")


def test_fake_snapshots_match_by_cwd_and_command_line() -> None:
    root = "C:" + BS + "work" + BS + "wt-1"
    scan = fake_scan(
        [
            ProcessSnapshot(1, "node.exe", root + BS + "pkg", "node server.js"),
            ProcessSnapshot(
                2, "code.exe", "C:" + BS + "elsewhere", '"code" "' + root + BS + 'a.txt"'
            ),
            ProcessSnapshot(3, "other.exe", "C:" + BS + "work" + BS + "wt-10", "other"),
        ]
    )
    reasons = scan.in_use(root)
    assert any("pid 1" in r for r in reasons)
    assert any("pid 2" in r for r in reasons)
    assert not any("pid 3" in r for r in reasons)


def test_scan_covers_this_process_and_reads_its_own_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scan = scan_processes()
    assert scan.ok
    me = [p for p in scan.processes if p.pid == procscan.current_pid()]
    assert me and me[0].cwd is not None
    assert normalize_path(me[0].cwd) == normalize_path(tmp_path)


def test_vendor_service_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", "C:" + BS + "Windows")
    monkeypatch.setenv("ProgramFiles", "C:" + BS + "Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", "C:" + BS + "Program Files (x86)")
    monkeypatch.setenv("ProgramData", "C:" + BS + "ProgramData")
    svc = procscan.is_vendor_service
    system32 = "C:" + BS + "Windows" + BS + "System32" + BS
    assert svc(system32 + "ctfmon.exe", "ctfmon.exe")
    assert svc("C:" + BS + "Program Files" + BS + "NVIDIA" + BS + "nvsphelper64.exe", "x.exe")
    assert svc("C:" + BS + "ProgramData" + BS + "Defender" + BS + "helper.exe", "h.exe")
    # a tool that can host user work stays blocking even under Program Files / System32
    assert not svc("C:" + BS + "Program Files" + BS + "nodejs" + BS + "node.exe", "node.exe")
    assert not svc(system32 + "cmd.exe", "cmd.exe")
    assert not svc(system32 + "WindowsPowerShell" + BS + "powershell.exe", "powershell.exe")
    assert not svc(
        "C:" + BS + "Program Files" + BS + "Git" + BS + "cmd" + BS + "git.exe", "git.exe"
    )
    # the name in the process table is also checked, not only the image basename
    assert not svc("C:" + BS + "Program Files" + BS + "x" + BS + "tool.exe", "python.exe")
    # under the user's profile, on another drive, unknown or empty: blocking
    assert not svc("C:" + BS + "Users" + BS + "u" + BS + "AppData" + BS + "tool.exe", "tool.exe")
    assert not svc("D:" + BS + "stuff" + BS + "tool.exe", "tool.exe")
    assert not svc(None, "tool.exe")
    assert not svc("", "tool.exe")
    # a sibling directory that merely starts with the same text is not inside
    assert not svc("C:" + BS + "Windows.old" + BS + "tool.exe", "tool.exe")
    assert not svc("C:" + BS + "Program Files Evil" + BS + "tool.exe", "tool.exe")


def test_set_aside_processes_do_not_block() -> None:
    scan = procscan.ProcessScan(
        True, (), (), 3, "", ((1, "ctfmon.exe", "C:" + BS + "Windows" + BS + "ctfmon.exe"),)
    )
    assert scan.usable
    assert scan.in_use("C:" + BS + "anything") == []


# --- spellings of a path that string matching alone missed (independent review, finding F1) ------


def _scan_of(cwd: Path | str, cmdline: str, name: str = "python.exe") -> procscan.ProcessScan:
    return fake_scan([ProcessSnapshot(7, name, str(cwd), cmdline)])


def test_relative_argument_is_resolved_against_the_process_directory(tmp_path: Path) -> None:
    wt = tmp_path / "proj-wt-feat"
    wt.mkdir()
    # the normal sibling layout: the process runs in the PARENT and names the worktree relatively
    scan = _scan_of(tmp_path, "python proj-wt-feat" + BS + "server.py")
    assert scan.in_use(wt), "relative argument not understood"
    scan_forward = _scan_of(tmp_path, "python proj-wt-feat/server.py")
    assert scan_forward.in_use(wt)
    # the sibling that merely shares a prefix is not the same directory
    (tmp_path / "proj-wt-feat2").mkdir()
    assert not _scan_of(tmp_path, "python proj-wt-feat2" + BS + "server.py").in_use(wt)


def test_dotdot_in_an_argument_is_collapsed(tmp_path: Path) -> None:
    wt = tmp_path / "proj-wt-feat"
    (tmp_path / "zz").mkdir()
    wt.mkdir()
    cmd = "python " + str(tmp_path / "zz" / ".." / "proj-wt-feat" / "server.py")
    assert _scan_of(tmp_path / "zz", cmd).in_use(wt)
    assert _scan_of(tmp_path, "python zz" + BS + ".." + BS + "proj-wt-feat" + BS + "x.py").in_use(
        wt
    )


def test_a_junction_alias_in_an_argument_or_as_the_directory_is_resolved(tmp_path: Path) -> None:
    wt = tmp_path / "proj-wt-feat"
    wt.mkdir()
    alias = tmp_path / "alias"
    make_junction(alias, wt)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _scan_of(elsewhere, "python " + str(alias / "server.py")).in_use(wt)  # argument
    assert _scan_of(alias, "python server.py").in_use(wt)  # working directory
    assert _scan_of(elsewhere, "python " + str(wt / "server.py")).in_use(alias)  # the other way


def test_local_administrative_share_spelling_is_the_same_directory(tmp_path: Path) -> None:
    wt = tmp_path / "proj-wt-feat"
    wt.mkdir()
    drive, rest = os.path.splitdrive(str(wt))
    unc = BS + BS + "localhost" + BS + drive[0] + "$" + rest
    assert _scan_of(tmp_path, "python " + unc + BS + "server.py").in_use(wt)
    unc_cwd = BS + BS + "127.0.0.1" + BS + drive[0] + "$" + rest
    assert _scan_of(unc_cwd, "python server.py").in_use(wt)
    # another machine's administrative share is NOT this directory
    other = BS + BS + "some-other-host" + BS + drive[0] + "$" + rest
    assert not _scan_of(tmp_path, "python " + other + BS + "server.py").in_use(wt)


def test_mentions_are_computed_once_per_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = tmp_path / "w"
    wt.mkdir()
    scan = _scan_of(tmp_path, "python w" + BS + "x.py")
    calls = {"n": 0}
    real = procscan.resolve_path

    def counting(*a: object, **k: object) -> str:
        calls["n"] += 1
        return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(procscan, "resolve_path", counting)
    scan.in_use(wt)
    first = calls["n"]
    scan.in_use(wt)
    # the per-process part is cached; only the root's own resolution repeats
    assert calls["n"] - first < first


@pytest.mark.parametrize(
    "name",
    ["python3.12.exe", "pycharm64.exe", "devenv.exe", "sublime_text.exe", "windsurf.exe",
     "Code.exe", "Cursor.exe", "node.exe", "bash.exe", "git-bash.exe", "idea64.exe", "nvim.exe"],
)  # fmt: skip
def test_work_hosts_are_never_set_aside(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ProgramFiles", "C:" + BS + "Program Files")
    monkeypatch.setenv("SystemRoot", "C:" + BS + "Windows")
    assert not procscan.is_vendor_service("C:" + BS + "Program Files" + BS + "x" + BS + name, name)


def test_a_drive_root_is_never_a_vendor_root(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ProgramData", "D:" + BS)
    assert not procscan.is_vendor_service("D:" + BS + "tool" + BS + "helper.exe", "helper.exe")
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        monkeypatch.delenv(var, raising=False)
    assert not procscan.is_vendor_service("C:" + BS + "Windows" + BS + "x.exe", "x.exe")  # none set
