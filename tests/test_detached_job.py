"""Unit tests for packaging/detached_job.py's decision helpers: the start gate, the low-RAM abort,
disk floors, and what an abort may delete. The scheduled-task launch and the reaper-survival
behaviour are proven on the real machine (packaging/DETACHED_JOBS.md, "Proof")."""

from __future__ import annotations

import importlib.util
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
    assert "is not inside" in capsys.readouterr().err
