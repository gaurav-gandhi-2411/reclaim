"""Unit tests for packaging/detached_job.py's decision helpers: the start gate, the low-RAM abort,
disk floors, and what an abort may delete. The scheduled-task launch and the reaper-survival
behaviour are proven on the real machine (packaging/DETACHED_JOBS.md, "Proof")."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
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


def test_launch_refuses_a_start_floor_below_8_gb(capsys: pytest.CaptureFixture[str]) -> None:
    rc = dj.main(["launch", "--name", "x", "--start-ram-gb", "7.9", "--", "cmd", "/c", "exit"])
    assert rc == 2
    assert "8 GB build floor" in capsys.readouterr().err


def test_launch_refuses_cleanup_outside_the_build_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = dj.main(["launch", "--name", "x", "--cleanup", r"C:\Users", "--", "cmd", "/c", "exit"])
    assert rc == 2
    assert "must be strictly inside" in capsys.readouterr().err


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
        ({"wait_max_min": 60}, "wait_max_min"),
        ({"command": []}, "command"),
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
    assert dj.plan_cleanup(out, {"keep.txt"}, tmp_path)[0] == []


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
    r = _runner(tmp_path, command=[sys.executable, "-c", "print('hello from job')"])
    assert r.run() == 0  # type: ignore[attr-defined]
    assert _status(tmp_path)["state"] == "done"
    assert "hello from job" in (tmp_path / "jobs" / "t" / "output.log").read_text()
    assert _log(tmp_path).rstrip().splitlines()[-1].endswith("END state=done rc=0 reason=-")


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


_SLOW_JOB = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).mkdir(); time.sleep(60)"


def _abort_case(tmp_path: Path) -> tuple[Path, Path, Path, list[str]]:
    out = tmp_path / "build"
    out.mkdir()
    old = out / "old_output"
    old.write_text("from an earlier build")
    new = out / "partial"
    return out, old, new, [sys.executable, "-c", _SLOW_JOB, str(new)]


def _wait_for(path: Path) -> None:
    import time

    for _ in range(100):
        if path.exists():
            return
        time.sleep(0.05)


def test_runner_aborts_on_low_ram_and_cleans_only_new_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, old, new, cmd = _abort_case(tmp_path)
    ram = [9.0, 9.0, 9.0]

    def fake_ram() -> float:
        if ram:
            return ram.pop(0)
        _wait_for(new)  # abort only once the job has written partial output
        return 1.0

    monkeypatch.setattr(dj, "free_ram_gb", fake_ram)
    monkeypatch.setattr(dj, "disk_free_gb", lambda _drive: 100.0)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "aborted" and "free RAM" in str(st["reason"])
    assert old.exists() and not new.exists()
    assert "child tree ended" in _log(tmp_path)


def test_runner_aborts_on_a_disk_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, old, new, cmd = _abort_case(tmp_path)
    monkeypatch.setattr(dj, "free_ram_gb", lambda: 9.0)
    calls = [0]

    def fake_disk(_drive: str) -> float:
        calls[0] += 1
        if calls[0] <= 3:
            return 100.0
        _wait_for(new)
        return 0.5

    monkeypatch.setattr(dj, "disk_free_gb", fake_disk)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    assert "disk floor" in str(_status(tmp_path)["reason"])
    assert old.exists() and not new.exists()


def test_watchdog_crash_still_kills_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, old, new, cmd = _abort_case(tmp_path)
    _readings(monkeypatch, [9.0])

    def broken(*_a: object) -> int:
        _wait_for(new)
        raise RuntimeError("sampler broke")

    monkeypatch.setattr(dj, "low_ram_step", broken)
    r = _runner(tmp_path, command=cmd, cleanup=[str(out)])
    assert r.run() == 1  # type: ignore[attr-defined]
    st = _status(tmp_path)
    assert st["state"] == "aborted" and "watchdog failed" in str(st["reason"])
    assert old.exists() and not new.exists()
    assert "END state=aborted" in _log(tmp_path)


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
