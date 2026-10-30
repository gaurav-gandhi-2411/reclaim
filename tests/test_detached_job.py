"""Unit tests for packaging/detached_job.py's decision helpers: the start gate, the low-RAM abort,
disk floors, and what an abort may delete. The scheduled-task launch and the reaper-survival
behaviour are proven on the real machine (packaging/DETACHED_JOBS.md, "Proof")."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "detached_job.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("detached_job", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["detached_job"] = mod
    spec.loader.exec_module(mod)
    return mod


dj = _load()


@pytest.fixture(autouse=True)
def _no_real_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may register a real scheduled task or write to the real jobs dir, even when the
    guard under test is broken: a mutant that dropped the 8 GB check once let the launch-refusal
    test register and run a real ReclaimJob-x from the real D:\\reclaim-build\\jobs."""
    monkeypatch.setattr(dj, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(dj, "CLEANUP_ROOT", tmp_path)
    real_run = dj.subprocess.run

    def guarded(cmd: list[str], *a: object, **k: object) -> object:
        if cmd and cmd[0] == dj.SCHTASKS:
            pytest.fail(f"a test reached the real schtasks: {cmd}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(dj.subprocess, "run", guarded)
    # A test run started from Git Bash must not trip launch's Git Bash refusal by accident.
    for var in ("MSYSTEM", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL"):
        monkeypatch.delenv(var, raising=False)


def test_parse_disk_floors() -> None:
    assert dj.parse_disk_floors(["c:=20", "D:=50.5"]) == {"C:": 20.0, "D:": 50.5}


@pytest.mark.parametrize("bad", ["C20", "C:", "CC:=1", "1:=5", "C:=-1", "C:=x"])
def test_parse_disk_floors_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        dj.parse_disk_floors([bad])


def test_disk_breaches_counts_unreadable_drive_as_breach() -> None:
    floors = {"C:": 20.0, "D:": 50.0}
    assert dj.disk_breaches({"C:": 20.0, "D:": 51.0}, floors) == []
    assert dj.disk_breaches({"C:": 19.9, "D:": 51.0}, floors) == ["C:"]
    assert dj.disk_breaches({"C:": 30.0}, floors) == ["D:"]


def test_start_gate_needs_three_consecutive_good_samples() -> None:
    ok = 0
    for ram in (9, 9, 7.9, 8, 8):
        ok = dj.start_gate_step(ok, ram, 8.0, True)
    assert ok == 2
    assert dj.start_gate_step(ok, 8.0, 8.0, True) == 3
    assert dj.start_gate_step(2, 30.0, 8.0, False) == 0


def test_low_ram_resets_on_a_good_sample() -> None:
    low = 0
    for ram in (1.5, 3.0, 1.5):
        low = dj.low_ram_step(low, ram, 2.0)
    assert low == 1
    assert dj.low_ram_step(low, 1.9, 2.0) == 2


def test_plan_cleanup_deletes_only_new_entries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    out = root / "build"
    out.mkdir(parents=True)
    (out / "old.txt").write_text("keep")
    (out / "old_dir").mkdir()
    before = {"old.txt", "old_dir"}
    (out / "new.bin").write_bytes(b"x")
    (out / "new_dir").mkdir()
    (out / "new_dir" / "f").write_text("y")
    delete, notes = dj.plan_cleanup(out, before, root)
    assert sorted(p.name for p in delete) == ["new.bin", "new_dir"]
    assert notes == []


def test_plan_cleanup_refuses_outside_or_equal_to_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "x").write_text("x")
    assert dj.plan_cleanup(other, set(), root)[0] == []
    assert "REFUSED" in dj.plan_cleanup(other, set(), root)[1][0]
    (root / "y").write_text("y")
    assert dj.plan_cleanup(root, set(), root)[0] == []


def _junction(link: Path, target: Path) -> None:
    subprocess.run(  # noqa: S603 -- fixed command, test paths only
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_plan_cleanup_never_deletes_through_a_junction(tmp_path: Path) -> None:
    root = tmp_path / "root"
    out = root / "build"
    out.mkdir(parents=True)
    target = tmp_path / "precious"
    target.mkdir()
    (target / "only_copy.txt").write_text("data")
    partial = out / "partial"
    partial.mkdir()
    _junction(partial / "link", target)
    _junction(out / "toplink", target)
    delete, notes = dj.plan_cleanup(out, set(), root)
    assert delete == []
    assert len(notes) == 2 and all("REFUSED" in n for n in notes)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_cleanup_dir_reached_through_a_junction_is_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f").write_text("f")
    _junction(root / "via", elsewhere)
    assert not dj.is_under(root / "via", root)
    assert dj.plan_cleanup(root / "via", set(), root)[0] == []


def test_holds_reparse_point_false_for_plain_tree(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f").write_text("f")
    assert dj.holds_reparse_point(tmp_path / "a") is False
    assert dj.holds_reparse_point(tmp_path / "missing") is True


_OK = ["--expect-line", "ok"]


def test_launch_refuses_a_start_floor_below_8_gb(capsys: pytest.CaptureFixture[str]) -> None:
    rc = dj.main(
        ["launch", "--name", "x", *_OK, "--start-ram-gb", "7.9", "--", "cmd", "/c", "exit"]
    )
    assert rc == 2
    assert "8 GB build floor" in capsys.readouterr().err


def test_launch_refuses_cleanup_outside_the_build_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = dj.main(
        ["launch", "--name", "x", *_OK, "--cleanup", r"C:\Users", "--", "cmd", "/c", "exit"]
    )
    assert rc == 2
    assert "must be strictly inside" in capsys.readouterr().err


def test_launch_refuses_a_job_with_no_success_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert dj.main(["launch", "--name", "x", "--", "cmd", "/c", "exit"]) == 2
    assert "declare a success check" in capsys.readouterr().err
    assert not (dj.JOBS_ROOT / "x").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["cmd.exe", "C:/", "build.cmd"],  # /c
        ["python", "C:/Users/me/AppData/Local/Temp/x.py"],  # /tmp/x.py
        ["tool", "--out=D:/out"],  # --out=/d/out
        ["gcc", "-IC:/x"],  # -I/c/x
    ],
)
def test_launch_refuses_a_command_git_bash_rewrote(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert dj.main(["launch", "--name", "x", *_OK, "--", *args]) == 2
    assert "Git Bash path conversion" in capsys.readouterr().err
    assert not (dj.JOBS_ROOT / "x").exists()


def test_mangled_args_leaves_windows_paths_and_switches_alone() -> None:
    ok = ["cmd.exe", "/c", r"D:\b\x.cmd", "/VERYSILENT", r"/LOG=D:\l.log", "http://h/x", "a:b"]
    assert dj.mangled_args(ok) == []


# Arguments that each break a naive command-line round trip: spaces, quotes, a trailing
# backslash, cmd metacharacters, %VAR%, a switch that Git Bash would rewrite, non-ASCII.
_TRICKY = ["/c", r"D:\a b\c.cmd", 'say "hi"', "trail\\", "a&b|c<d>e^f", "%PATH%", "", "ünï"]


def _fake_schtasks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(cmd: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dj.subprocess, "run", fake)
    return calls


def test_the_stored_command_round_trips_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launch stores the argv it was given, byte for byte, and the runner hands that argv to
    the child unchanged: the child prints its own argv and it must equal what was launched."""
    calls = _fake_schtasks(monkeypatch)
    echo = "import json, sys; print('ARGV=' + json.dumps(sys.argv[1:]))"
    cmd = [sys.executable, "-c", echo, *_TRICKY]
    assert dj.main(["launch", "--name", "rt", "--cwd", str(tmp_path), *_OK, "--", *cmd]) == 0
    assert [c[1] for c in calls] == ["/Create", "/Run"]
    job_dir = dj.JOBS_ROOT / "rt"
    assert json.loads((job_dir / "spec.json").read_text(encoding="utf-8"))["command"] == cmd
    _readings(monkeypatch, [9.0])
    spec = json.loads((job_dir / "spec.json").read_text(encoding="utf-8"))
    spec["gate_seconds"], spec["expect_lines"] = 0.05, ["ARGV="]
    (job_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    assert dj.Runner(job_dir, cleanup_root=tmp_path).run() == 0
    out = (job_dir / "output.log").read_text(encoding="utf-8")
    line = next(x for x in out.splitlines() if x.startswith("ARGV="))
    assert json.loads(line[len("ARGV=") :]) == _TRICKY


def _git_bash() -> Path | None:
    """Git for Windows' bash (not WSL's System32\\bash.exe, which is a different program)."""
    import shutil

    git = shutil.which("git")
    if not git:
        return None
    # git.exe sits in Git\cmd, Git\bin or Git\mingw64\bin; bash.exe in Git\bin or Git\usr\bin.
    for root in Path(git).resolve().parents[:3]:
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.exists() and "system32" not in str(cand).lower():
                return cand
    return None


_DRIVER = """
import json, subprocess, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import detached_job as dj
root = Path(sys.argv[2])
dj.JOBS_ROOT, dj.CLEANUP_ROOT, dj.SCHTASKS = root / "jobs", root, str(root / "no-schtasks.exe")
dj.subprocess = types.SimpleNamespace(
    run=lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
raise SystemExit(dj.main(["launch", "--name", "gb", "--expect-line", "x", "--"] + sys.argv[3:]))
"""


@pytest.mark.skipif(_git_bash() is None, reason="needs Git for Windows' bash")
@pytest.mark.parametrize("no_conv", [True, False])
def test_a_launch_from_git_bash_round_trips_or_is_refused(tmp_path: Path, no_conv: bool) -> None:
    """Through a real Git Bash: with MSYS_NO_PATHCONV=1 the stored command is exactly what was
    typed; without it, bash turns /c into C:/ and launch refuses instead of storing that."""
    bash = _git_bash()
    assert bash is not None
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items() if k not in ("MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL")
    }
    if no_conv:
        env["MSYS_NO_PATHCONV"] = "1"
    py, script_dir = sys.executable.replace("\\", "/"), str(_SCRIPT.parent)
    line = f"'{py}' '{driver}' '{script_dir}' '{tmp_path}' cmd.exe /c 'D:\\b\\x.cmd' /tmp/y"
    r = subprocess.run(  # noqa: S603 -- Git's own bash, a script the test wrote
        [str(bash), "-c", line], capture_output=True, text=True, env=env, check=False
    )
    spec_path = tmp_path / "jobs" / "gb" / "spec.json"
    if no_conv:
        assert r.returncode == 0, r.stderr
        stored = json.loads(spec_path.read_text(encoding="utf-8"))["command"]
        assert stored == ["cmd.exe", "/c", r"D:\b\x.cmd", "/tmp/y"]
    else:
        assert r.returncode == 2 and "Git Bash with path conversion on" in r.stderr
        assert not spec_path.exists()


# ---- spec validation ----------------------------------------------------------------------------


def _spec(tmp_path: Path, **over: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": "t",
        "command": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "start_ram_gb": 8.0,
        "abort_ram_gb": 2.0,
        "disk_floors": {"C:": 1.0},
        "cleanup": [],
        "wait_max_min": 1.0,
        "gate_seconds": 0.05,
        "sample_seconds": 0.1,
        "limit_hours": 1,
    }
    spec.update(over)
    return spec


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"start_ram_gb": float("nan")}, "finite"),
        ({"start_ram_gb": 7.99}, "8 GB build floor"),
        ({"abort_ram_gb": 0}, "abort_ram_gb"),
        ({"abort_ram_gb": 8.0}, "abort_ram_gb"),
        ({"sample_seconds": 0}, "above 0"),
        ({"limit_hours": 0}, "limit_hours"),
        ({"limit_hours": 1.5}, "limit_hours"),
        ({"limit_hours": True}, "finite"),
        ({"abort_ram_gb": 0.4}, "abort_ram_gb"),
        ({"sample_seconds": 61}, "at most 60"),
        ({"gate_seconds": 1e6}, "at most 60"),
        ({"disk_floors": {"C:": "20"}}, "disk_floors"),
        ({"cwd": None}, "cwd"),
        ({"wait_max_min": 60}, "wait_max_min"),
        ({"command": []}, "command"),
        ({"command": "cmd /c x"}, "command"),
        ({"cleanup": ["relative\\out"]}, "absolute"),
    ],
)
def test_validate_spec_rejects(tmp_path: Path, over: dict[str, object], needle: str) -> None:
    errs = dj.validate_spec(_spec(tmp_path, **over), tmp_path / "jobs", tmp_path)
    assert any(needle in e for e in errs), errs


def test_validate_spec_accepts_the_floor_itself(tmp_path: Path) -> None:
    assert dj.validate_spec(_spec(tmp_path, start_ram_gb=8.0), tmp_path / "jobs", tmp_path) == []


def test_cleanup_dir_may_not_be_the_root_or_hold_the_jobs_dir(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    assert dj.cleanup_dir_problems(tmp_path, jobs, tmp_path)
    assert dj.cleanup_dir_problems(jobs, jobs, tmp_path)
    assert dj.cleanup_dir_problems(jobs / "other_job", jobs, tmp_path)
    assert dj.cleanup_dir_problems(tmp_path / "build", jobs, tmp_path) == []
    # With the jobs dir outside the root, only the root-equality check refuses the root itself.
    root = tmp_path / "root"
    assert "strictly inside" in " ".join(dj.cleanup_dir_problems(root, jobs, root))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_cleanup_dir_that_is_a_junction_inside_the_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    _junction(tmp_path / "alias", tmp_path / "real")
    problems = dj.cleanup_dir_problems(tmp_path / "alias", tmp_path / "jobs", tmp_path)
    assert problems and "junction" in problems[0]


def test_plan_cleanup_before_set_is_case_insensitive(tmp_path: Path) -> None:
    out = tmp_path / "build"
    out.mkdir()
    (out / "Keep.txt").write_text("k")
    (out / "other.TXT").write_text("o")
    assert dj.plan_cleanup(out, {"keep.txt", "OTHER.txt"}, tmp_path)[0] == []


def test_task_xml_is_well_formed_and_escapes(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    job_dir = tmp_path / 'a & "b" <c>'
    xml = dj.task_xml("n_1", r"C:\py\pythonw.exe", r"C:\x y\detached_job.py", job_dir, 3)
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))  # noqa: S314 -- our own XML
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    args = root.find(".//t:Arguments", ns)
    assert args is not None and args.text == f'"C:\\x y\\detached_job.py" run "{job_dir}"'
    assert root.findtext(".//t:ExecutionTimeLimit", namespaces=ns) == "PT3H"


# ---- the runner, with a real child and faked RAM/disk readings ---------------------------------


def _runner(tmp_path: Path, **over: object) -> object:
    job_dir = tmp_path / "jobs" / "t"
    job_dir.mkdir(parents=True)
    (job_dir / "spec.json").write_text(json.dumps(_spec(tmp_path, **over)), encoding="utf-8")
    return dj.Runner(job_dir, cleanup_root=tmp_path)


def _readings(monkeypatch: pytest.MonkeyPatch, ram: list[float], disk: float = 100.0) -> None:
    """free_ram_gb returns ram[i] on call i, then the last value forever."""
    it = iter(ram)
    last = [ram[-1]]

    def fake_ram() -> float:
        last[0] = next(it, last[0])
        return last[0]

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: disk)


def _status(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "jobs" / "t" / "status.json"
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _log(tmp_path: Path) -> str:
    return (tmp_path / "jobs" / "t" / "runner.log").read_text(encoding="utf-8")


def test_runner_done_and_output_captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readings(monkeypatch, [9.0])
    r = _runner(
        tmp_path,
        command=[sys.executable, "-c", "print('hello from job')"],
        expect_lines=["hello from job"],
    )
    assert r.run() == 0  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "done"
    assert "hello from job" in (tmp_path / "jobs" / "t" / "output.log").read_text()
    assert _log(tmp_path).rstrip().splitlines()[-1].endswith("END state=done rc=0 reason=-")


def test_rc_0_with_no_success_check_is_suspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build-12 try-1 shape: `cmd C:/` exited 0 having built nothing (a spec written before
    success checks existed, or hand-edited to drop them)."""
    _readings(monkeypatch, [9.0])
    r = _runner(tmp_path, command=[sys.executable, "-c", "pass"])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert (st["state"], st["rc"], st["reason"]) == ("suspect", 0, "no success check declared")
    assert "END state=suspect rc=0" in _log(tmp_path)
    assert dj.main(["status", "t"]) == 1
    assert '"suspect"' in capsys.readouterr().out


def test_rc_0_without_the_expected_line_is_suspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0])
    r = _runner(
        tmp_path, command=[sys.executable, "-c", "print('half done')"], expect_lines=["==> DONE."]
    )
    assert r.run() == 1  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "suspect"
    assert "expected line not in output: '==> DONE.'" in _log(tmp_path)


def test_an_expected_file_older_than_the_job_is_suspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale installer from an earlier build must not pass for this build's output."""
    _readings(monkeypatch, [9.0])
    old = tmp_path / "reclaim-setup.exe"
    old.write_bytes(b"old build")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    missing = tmp_path / "never-written.exe"
    r = _runner(
        tmp_path, command=[sys.executable, "-c", "pass"], expect_files=[str(old), str(missing)]
    )
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "suspect" and st["artifacts"] == []
    assert "unchanged since the job started" in str(st["reason"])
    assert "missing" in str(st["reason"])


@pytest.mark.parametrize("age_s", [0.0, 1.5, -3600.0])  # just written, inside the slack, future
def test_a_recent_file_the_job_did_not_touch_is_suspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age_s: float
) -> None:
    """Freshness alone is not proof (verifier finding on #223): a file written a moment before
    the job, or future-dated, passed a bare `mtime >= start - 2 s` check."""
    import time

    _readings(monkeypatch, [9.0])
    f = tmp_path / "reclaim-setup.exe"
    f.write_bytes(b"not this job's")
    os.utime(f, (time.time() - age_s, time.time() - age_s))
    r = _runner(tmp_path, command=[sys.executable, "-c", "pass"], expect_files=[str(f)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "suspect"


def test_a_copied_in_file_keeping_an_old_mtime_is_suspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that only copies an old installer into place (copy2 keeps its mtime) built nothing."""
    _readings(monkeypatch, [9.0])
    old, dest = tmp_path / "old.exe", tmp_path / "out" / "reclaim-setup.exe"
    old.write_bytes(b"old build")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    dest.parent.mkdir()
    job = f"import shutil; shutil.copy2(r'{old}', r'{dest}')"
    r = _runner(tmp_path, command=[sys.executable, "-c", job], expect_files=[str(dest)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert "mtime is outside the job's run" in str(_status(tmp_path)["reason"])


@pytest.mark.parametrize(
    ("env", "on"),
    [
        ({}, False),
        ({"MSYSTEM": "MINGW64"}, True),
        ({"MSYSTEM": "MINGW64", "MSYS_NO_PATHCONV": "1"}, False),
        ({"MSYSTEM": "MINGW64", "MSYS2_ARG_CONV_EXCL": "*"}, False),
        ({"MSYSTEM": "MINGW64", "MSYS2_ARG_CONV_EXCL": "--foo"}, True),
    ],
)
def test_git_bash_conversion_on(env: dict[str, str], on: bool) -> None:
    assert dj.git_bash_conversion_on(env) is on


def test_launch_from_git_bash_with_conversion_on_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Conversion also makes shapes no pattern can tell from real Windows values
    (/c/a:/c/b -> C:\\a;C:\\b), so the environment, not the argv, decides."""
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    assert dj.main(["launch", "--name", "x", *_OK, "--", "tool", r"C:\a;C:\b"]) == 2
    assert "Git Bash with path conversion on" in capsys.readouterr().err
    assert not (dj.JOBS_ROOT / "x").exists()


def test_a_fresh_expected_file_is_done_and_its_sha_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    _readings(monkeypatch, [9.0])
    out = tmp_path / "dist" / "reclaim-setup.exe"
    out.parent.mkdir()
    job = f"open(r'{out}', 'wb').write(b'new build'); print('==> DONE.')"
    r = _runner(
        tmp_path,
        command=[sys.executable, "-c", job],
        expect_files=[str(out)],
        expect_lines=["==> DONE."],
    )
    assert r.run() == 0  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "done"
    [art] = st["artifacts"]  # type: ignore[misc]
    assert art["sha256"] == hashlib.sha256(b"new build").hexdigest() and art["bytes"] == 9
    assert f"artifact {out} bytes=9" in _log(tmp_path)


def test_a_failed_rc_is_failed_even_when_the_checks_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0])
    job = "print('==> DONE.'); raise SystemExit(4)"
    r = _runner(tmp_path, command=[sys.executable, "-c", job], expect_lines=["==> DONE."])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert (_status(tmp_path)["state"], _status(tmp_path)["rc"]) == ("failed", 4)


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"expect_files": "x"}, "expect_files must be"),
        ({"expect_lines": [""]}, "expect_lines must be"),
        ({"expect_files": ["relative.exe"]}, "absolute"),
        ({"command": ["cmd.exe", "C:/", "x"]}, "Git Bash"),
        ({"cwd": "C:/Users/me"}, "Git Bash"),
        ({"expect_files": ["C:/Users/me/AppData/Local/Temp/x.exe"]}, "Git Bash"),  # /tmp/x.exe
    ],
)
def test_validate_spec_rejects_bad_checks_and_mangled_paths(
    tmp_path: Path, over: dict[str, object], needle: str
) -> None:
    errs = dj.validate_spec(_spec(tmp_path, **over), tmp_path / "jobs", tmp_path)
    assert any(needle in e for e in errs), errs


def test_runner_failed_keeps_rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readings(monkeypatch, [9.0])
    r = _runner(tmp_path, command=[sys.executable, "-c", "raise SystemExit(3)"])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert (_status(tmp_path)["state"], _status(tmp_path)["rc"]) == ("failed", 3)


def test_runner_gate_waits_then_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readings(monkeypatch, [9.0, 9.0, 7.9, 9.0, 9.0, 7.9])
    marker = tmp_path / "ran"
    r = _runner(
        tmp_path,
        wait_max_min=0.005,
        command=[sys.executable, "-c", f"open(r'{marker}', 'w').close()"],
    )
    assert r.run() == 1  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "gave_up"
    assert not marker.exists()


# The job creates its cleanup dir (a cleanup dir may not exist before the job starts), writes
# partial output there, and starts a grandchild whose PID it records outside the cleanup dir.
_SLOW_JOB = (
    "import pathlib, subprocess, sys, time\n"
    "p = pathlib.Path(sys.argv[1]); p.mkdir(parents=True)\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "pathlib.Path(sys.argv[2]).write_text(str(g.pid))\n"
    "time.sleep(60)\n"
)


def _abort_case(tmp_path: Path) -> tuple[Path, Path, Path, list[str]]:
    out = tmp_path / "build"
    new = out / "partial"
    gpid = tmp_path / "grandchild.pid"
    return out, new, gpid, [sys.executable, "-c", _SLOW_JOB, str(new), str(gpid)]


def _wait_for(path: Path) -> None:
    import time

    for _ in range(200):
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    out = subprocess.run(  # noqa: S603 -- fixed system binary, a PID the test recorded
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return str(pid) in out


def test_runner_aborts_on_low_ram_ends_the_tree_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, new, gpid, cmd = _abort_case(tmp_path)
    ram = [9.0, 9.0, 9.0]

    def fake_ram() -> float:
        if ram:
            return ram.pop(0)
        _wait_for(gpid)  # abort only once the job has written output and started a grandchild
        return 1.0

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: 100.0)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "aborted" and "free RAM" in str(st["reason"])
    assert out.is_dir() and not new.exists()
    assert not _alive(int(gpid.read_text()))
    assert "child tree ended" in _log(tmp_path)


def test_runner_aborts_on_a_disk_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, new, gpid, cmd = _abort_case(tmp_path)
    monkeypatch.setattr(dj, "free_ram_gb", lambda: 9.0)
    calls = [0]

    def fake_disk(_drive: str) -> float:
        calls[0] += 1
        if calls[0] <= 3:
            return 100.0
        _wait_for(gpid)
        return 0.5

    monkeypatch.setattr(dj, "disk_free_gb", fake_disk)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert "disk floor" in str(_status(tmp_path)["reason"])
    assert not new.exists()


def test_watchdog_crash_still_kills_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, new, gpid, cmd = _abort_case(tmp_path)
    _readings(monkeypatch, [9.0])

    def broken(*_a: object) -> int:
        _wait_for(gpid)
        raise RuntimeError("sampler broke")

    monkeypatch.setattr(dj, "low_ram_step", broken)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "aborted" and "watchdog failed" in str(st["reason"])
    assert not new.exists()
    assert "END state=aborted" in _log(tmp_path)


def test_two_low_samples_abort_even_if_ram_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0, 9.0, 9.0, 1.0, 1.0, 9.0])
    r = _runner(tmp_path, command=[sys.executable, "-c", "import time; time.sleep(5)"])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "aborted"


def test_ram_exactly_at_the_abort_floor_does_not_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0, 9.0, 9.0, 2.0])
    r = _runner(
        tmp_path,
        command=[sys.executable, "-c", "import time; time.sleep(0.6); print('slept')"],
        expect_lines=["slept"],
    )
    assert r.run() == 0  # type: ignore[attr-defined]


def test_cleanup_is_skipped_when_the_kill_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, new, gpid, cmd = _abort_case(tmp_path)
    ram = [9.0, 9.0, 9.0]

    def fake_ram() -> float:
        if ram:
            return ram.pop(0)
        _wait_for(gpid)
        return 1.0

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: 100.0)
    real_kill = dj.Runner.kill

    def unconfirmed(self: object, child: object, job: object) -> bool:
        real_kill(self, child, job)  # really end it, but report it as unconfirmed
        return False

    monkeypatch.setattr(dj.Runner, "kill", unconfirmed)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert new.exists()
    assert "cleanup SKIPPED" in _log(tmp_path)


def test_no_job_object_means_no_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readings(monkeypatch, [9.0])
    monkeypatch.setattr(dj, "_job_for", lambda _handle: None)
    r = _runner(tmp_path, command=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "aborted" and "Job Object" in str(st["reason"])


def test_kill_reports_an_unconfirmed_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Stubborn:
        pid = 0
        returncode = None

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("x", timeout)

    monkeypatch.setattr(dj.subprocess, "run", lambda *_a, **_k: None)  # no real taskkill
    r = _runner(tmp_path)
    assert r.kill(Stubborn(), None) is False  # type: ignore[attr-defined]
    assert "still alive" in _log(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_abort_never_cleans_a_cleanup_dir_the_job_made_a_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    precious = tmp_path / "precious"  # inside the cleanup root, so only the re-check guards it
    precious.mkdir()
    (precious / "only_copy.txt").write_text("data")
    out = tmp_path / "build"
    flag = tmp_path / "linked"
    job = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.run(['cmd', '/c', 'mklink', '/J', r'{out}', r'{precious}'], check=True)\n"
        f"pathlib.Path(r'{flag}').write_text('1')\n"
        "time.sleep(60)\n"
    )
    ram = [9.0, 9.0, 9.0]

    def fake_ram() -> float:
        if ram:
            return ram.pop(0)
        _wait_for(flag)
        return 1.0

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: 100.0)
    r = _runner(tmp_path, command=[sys.executable, "-c", job], cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert (precious / "only_copy.txt").read_text() == "data"
    assert "REFUSED cleanup" in _log(tmp_path)


def test_a_cleanup_dir_that_already_exists_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0])
    out = tmp_path / "shared"
    out.mkdir()
    marker = tmp_path / "ran"
    r = _runner(
        tmp_path,
        cleanup=[str(out)],
        command=[sys.executable, "-c", f"open(r'{marker}', 'w')"],
    )
    assert r.run() == 1  # type: ignore[attr-defined]
    assert "already exists" in str(_status(tmp_path)["reason"]) and not marker.exists()


@pytest.mark.parametrize("spec_text", ["[]", "{}", '{"cleanup": null}', "not json"])
def test_a_malformed_spec_ends_with_an_end_line(tmp_path: Path, spec_text: str) -> None:
    job_dir = tmp_path / "jobs" / "t"
    job_dir.mkdir(parents=True)
    (job_dir / "spec.json").write_text(spec_text, encoding="utf-8")
    assert dj.Runner(job_dir, cleanup_root=tmp_path).run() == 1
    assert "END state=error" in _log(tmp_path)


def test_a_malformed_cleanup_entry_is_an_error(tmp_path: Path) -> None:
    errs = dj.validate_spec(_spec(tmp_path, cleanup=[5]), tmp_path / "jobs", tmp_path)
    assert any("cleanup must be a list of strings" in e for e in errs)


def test_a_rerun_of_the_task_does_not_repeat_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readings(monkeypatch, [9.0])
    count = tmp_path / "count"
    r = _runner(
        tmp_path,
        command=[sys.executable, "-c", f"open(r'{count}', 'a').write('x')"],
        expect_files=[str(count)],
    )
    assert r.run() == 0  # type: ignore[attr-defined]
    again = dj.Runner(tmp_path / "jobs" / "t", cleanup_root=tmp_path)
    assert again.run() == 1
    assert count.read_text() == "x" and _status(tmp_path)["state"] == "done"


def test_save_outlasts_a_reader_holding_status_open(tmp_path: Path) -> None:
    import threading

    r = _runner(tmp_path)
    status = tmp_path / "jobs" / "t" / "status.json"
    status.write_text("{}")
    f = status.open()  # Windows denies replacing a file another handle has open
    threading.Timer(0.6, f.close).start()
    r.save(state="waiting")  # type: ignore[attr-defined]
    assert json.loads(status.read_text())["state"] == "waiting"


def test_runner_refuses_a_hand_edited_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _readings(monkeypatch, [9.0])
    marker = tmp_path / "ran"
    r = _runner(
        tmp_path, start_ram_gb=4.0, command=[sys.executable, "-c", f"open(r'{marker}','w')"]
    )
    assert r.run() == 1  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "error" and not marker.exists()
    assert "END state=error" in _log(tmp_path)


def test_status_reports_a_runner_that_stopped_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dj, "JOBS_ROOT", tmp_path / "jobs")
    job = tmp_path / "jobs" / "t"
    job.mkdir(parents=True)
    (job / "spec.json").write_text(json.dumps(_spec(tmp_path)), encoding="utf-8")
    old = (dj._now() - dt.timedelta(minutes=10)).isoformat()
    (job / "status.json").write_text(json.dumps({"state": "running", "heartbeat": old}))
    assert dj.main(["status", "t"]) == 3
    assert '"stale"' in capsys.readouterr().out
    fresh = dj._now().isoformat()
    (job / "status.json").write_text(json.dumps({"state": "running", "heartbeat": fresh}))
    assert dj.main(["status", "t"]) == 2
    # gate_seconds 20: a runner may be silent up to 3 x 20 + 60 = 120 s before it is stale.
    (job / "spec.json").write_text(json.dumps(_spec(tmp_path, gate_seconds=20)), encoding="utf-8")
    quiet = (dj._now() - dt.timedelta(seconds=90)).isoformat()
    (job / "status.json").write_text(json.dumps({"state": "running", "heartbeat": quiet}))
    assert dj.main(["status", "t"]) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_abort_never_cleans_through_an_ancestor_the_job_made_a_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Data-safety pass 3: the cleanup dir a\b is new at start; the job then turns a into a
    # junction to victim, so a\b resolves to victim\b, which holds files the job never made.
    victim = tmp_path / "victim"
    (victim / "b").mkdir(parents=True)
    (victim / "b" / "precious.txt").write_text("data")
    flag = tmp_path / "linked"
    ancestor = tmp_path / "a"
    job = (
        "import pathlib, subprocess, time\n"
        f"subprocess.run(['cmd', '/c', 'mklink', '/J', r'{ancestor}', r'{victim}'], check=True)\n"
        f"pathlib.Path(r'{flag}').write_text('1')\n"
        "time.sleep(60)\n"
    )
    ram = [9.0, 9.0, 9.0]

    def fake_ram() -> float:
        if ram:
            return ram.pop(0)
        _wait_for(flag)
        return 1.0

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: 100.0)
    r = _runner(tmp_path, command=[sys.executable, "-c", job], cleanup=[str(tmp_path / "a" / "b")])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert (victim / "b" / "precious.txt").read_text() == "data"
    assert "REFUSED cleanup" in _log(tmp_path)


def test_save_never_writes_through_a_link_left_at_the_tmp_name(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("DATA")
    os.link(victim, tmp_path / "jobs" / "t" / "status.json.tmp")
    r.save(state="waiting")  # type: ignore[attr-defined]
    assert victim.read_text() == "DATA"
    assert _status(tmp_path)["state"] == "waiting"


@pytest.mark.parametrize("cmd", ["status", "forget"])
def test_status_and_forget_refuse_a_name_that_is_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    monkeypatch.setattr(dj, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(dj.subprocess, "run", lambda *_a, **_k: pytest.fail("ran schtasks"))
    assert dj.main([cmd, r"..\outside"]) == 2
