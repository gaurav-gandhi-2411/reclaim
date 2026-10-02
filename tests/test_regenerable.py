from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from reclaim import regenerable as rg
from reclaim.regenerable import (
    CommandResult,
    RegenerableEnv,
    RegenerableItemResult,
    run_regenerable_clean,
)

NOW = 2_000_000_000.0  # fixed clock: tests never depend on wall time
DAY = 86400.0


def _write(path: Path, size: int = 100, *, age_days: float = 30.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _age_dirs(root: Path, age_days: float) -> None:
    stamp = NOW - age_days * DAY
    for dirpath, dirnames, _files in os.walk(root, topdown=False):
        for d in dirnames:
            os.utime(Path(dirpath) / d, (stamp, stamp))
    os.utime(root, (stamp, stamp))


class FakeMachine:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.local = self.home / "AppData" / "Local"
        self.temp = self.local / "Temp"
        self.crash = self.local / "CrashDumps"
        for d in (self.home, self.local, self.temp, self.crash):
            d.mkdir(parents=True, exist_ok=True)
        self.running: frozenset[str] = frozenset()
        self.on_path: set[str] = set()
        self.locked: set[str] = set()
        self.commands: list[list[str]] = []
        self.call_meta: list[tuple[float, dict[str, str]]] = []
        self.command_effect: dict[str, CommandResult] = {}
        self.command_delete: dict[str, Path] = {}

    def env(self, **overrides: object) -> RegenerableEnv:
        def which(name: str) -> str | None:
            return f"C:/fake/{name}.exe" if name in self.on_path else None

        def run(argv: Sequence[str], timeout: float, cmd_env: dict[str, str]) -> CommandResult:
            self.commands.append(list(argv))
            self.call_meta.append((timeout, cmd_env))
            key = Path(argv[0]).stem
            target = self.command_delete.get(key)
            if target is not None and target.exists():
                for p in sorted(target.rglob("*"), reverse=True):
                    p.unlink() if p.is_file() else p.rmdir()
            return self.command_effect.get(key, CommandResult(0, "ok", ""))

        base: dict[str, object] = {
            "home": self.home,
            "local_appdata": self.local,
            "temp_roots": (self.temp,),
            "crash_dump_roots": (self.crash,),
            "now": lambda: NOW,
            "running_process_names": lambda: self.running,
            "which": which,
            "run_command": run,
            "has_open_handle": lambda p: p in self.locked,
        }
        base.update(overrides)
        return RegenerableEnv(**base)  # type: ignore[arg-type]


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


def _item(report: rg.RegenerableReport, key: str) -> RegenerableItemResult:
    return next(i for i in report.items if i.key == key)


# --- aged TEMP / crash dumps ----------------------------------------------------------------


def test_aged_temp_file_and_dir_are_cleaned_and_bytes_counted(machine: FakeMachine) -> None:
    old_file = _write(machine.temp / "old.log", 1000)
    _write(machine.temp / "olddir" / "a.bin", 2000)
    _write(machine.temp / "olddir" / "sub" / "b.bin", 3000)
    _age_dirs(machine.temp / "olddir", 30)

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    item = _item(report, "temp0")
    assert item.status == "cleaned"
    assert item.bytes_removed == 6000
    assert item.files_removed == 3
    assert not old_file.exists()
    assert not (machine.temp / "olddir").exists()
    assert machine.temp.is_dir(), "the temp root itself is never removed"


def test_active_temp_dir_is_skipped_even_if_its_own_mtime_is_old(machine: FakeMachine) -> None:
    # #110's rule: age a directory by its NEWEST content, not its own mtime.
    _write(machine.temp / "session" / "stale.txt", 10, age_days=30)
    fresh = _write(machine.temp / "session" / "live.txt", 10, age_days=0.5)
    _age_dirs(machine.temp / "session", 30)

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert fresh.exists()
    assert (machine.temp / "session" / "stale.txt").exists()
    assert _item(report, "temp0").files_removed == 0


def test_recent_temp_file_is_never_touched(machine: FakeMachine) -> None:
    recent = _write(machine.temp / "download.part", 500, age_days=2)
    run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    assert recent.exists()


def test_temp_entry_with_git_or_venv_is_left_for_review(machine: FakeMachine) -> None:
    _write(machine.temp / "proj" / ".git" / "HEAD", 10)
    _write(machine.temp / "proj" / "main.py", 10)
    _write(machine.temp / "envdir" / "pyvenv.cfg", 10)
    _age_dirs(machine.temp / "proj", 30)
    _age_dirs(machine.temp / "envdir", 30)

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert (machine.temp / "proj" / "main.py").exists()
    assert (machine.temp / "envdir" / "pyvenv.cfg").exists()
    assert "for review" in _item(report, "temp0").detail


def test_open_handle_file_is_skipped_and_reported_never_forced(machine: FakeMachine) -> None:
    locked = _write(machine.temp / "held" / "db.lock", 400)
    free = _write(machine.temp / "held" / "free.tmp", 100)
    _age_dirs(machine.temp / "held", 30)
    machine.locked.add(str(locked))

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    item = _item(report, "temp0")
    assert locked.exists()
    assert not free.exists()
    assert item.files_skipped_in_use == 1
    assert str(locked) in item.skipped_paths
    assert item.bytes_removed == 100
    assert (machine.temp / "held").is_dir(), "a directory holding a skipped file is kept"


def test_dry_run_deletes_nothing_but_reports_what_would_go(machine: FakeMachine) -> None:
    target = _write(machine.temp / "old.bin", 777)
    report = run_regenerable_clean(machine.env(), apply=False, audit_log_path=None)
    assert target.exists()
    item = _item(report, "temp0")
    assert item.status == "would_clean"
    assert item.bytes_removed == 777
    assert report.disk_free_before_bytes is None


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_inside_temp_is_unlinked_never_followed(
    machine: FakeMachine, tmp_path: Path
) -> None:
    outside = tmp_path / "precious"
    keep = _write(outside / "keep.txt", 50)
    junction = machine.temp / "link"
    subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    # A junction's own timestamps can't be back-dated portably, so move the clock forward instead.
    later = time.time() + 30 * DAY

    run_regenerable_clean(machine.env(now=lambda: later), apply=True, audit_log_path=None)

    assert keep.exists(), "the junction's target must never be deleted"
    assert not junction.exists(), "the junction entry itself is aged litter and is removed"


def test_crash_dumps_cleaned_by_age(machine: FakeMachine) -> None:
    old = _write(machine.crash / "reclaim.exe.1.dmp", 5000, age_days=40)
    new = _write(machine.crash / "reclaim.exe.2.dmp", 5000, age_days=1)
    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    assert not old.exists()
    assert new.exists()
    assert _item(report, "crash0").bytes_removed == 5000


# --- browsers ----------------------------------------------------------------------------------


def _chrome_profile(machine: FakeMachine) -> Path:
    return machine.local / "Google" / "Chrome" / "User Data" / "Default"


def test_closed_browser_cache_is_cleaned_but_cookies_survive(machine: FakeMachine) -> None:
    profile = _chrome_profile(machine)
    cache_file = _write(profile / "Cache" / "Cache_Data" / "f_000001", 4000, age_days=0.1)
    code_cache = _write(profile / "Code Cache" / "js" / "c1", 1000, age_days=0.1)
    cookies = _write(profile / "Network" / "Cookies", 300, age_days=0.1)
    history = _write(profile / "History", 300, age_days=0.1)

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert not cache_file.exists()
    assert not code_cache.exists()
    assert cookies.exists() and history.exists(), "only cache dirs are on the allow-list"
    assert (profile / "Cache").is_dir()
    assert _item(report, "chrome").bytes_removed == 5000


def test_running_browser_cache_is_left_completely_alone(machine: FakeMachine) -> None:
    cache_file = _write(
        _chrome_profile(machine) / "Cache" / "Cache_Data" / "f_1", 4000, age_days=60
    )
    machine.running = frozenset({"chrome.exe"})

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert cache_file.exists()
    assert _item(report, "chrome").status == "skipped_browser_running"


def test_other_browser_running_does_not_block_a_closed_one(machine: FakeMachine) -> None:
    edge_cache = _write(
        machine.local / "Microsoft" / "Edge" / "User Data" / "Default" / "Cache" / "x", 10
    )
    chrome_cache = _write(_chrome_profile(machine) / "Cache" / "y", 10)
    machine.running = frozenset({"chrome.exe", "msedgewebview2.exe"})

    run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert chrome_cache.exists()
    assert not edge_cache.exists(), "msedgewebview2 is a shared runtime, not Edge itself"


def test_process_enumeration_failure_fails_closed(machine: FakeMachine) -> None:
    cache_file = _write(_chrome_profile(machine) / "Cache" / "y", 10)

    def boom() -> frozenset[str]:
        raise OSError("snapshot failed")

    report = run_regenerable_clean(
        machine.env(running_process_names=boom), apply=True, audit_log_path=None
    )

    assert cache_file.exists()
    assert _item(report, "chrome").status == "skipped_browser_running"


# --- native commands ---------------------------------------------------------------------------


def test_native_tool_runs_its_own_prune_command_and_measures_shrinkage(
    machine: FakeMachine,
) -> None:
    cache = machine.local / "uv" / "cache"
    _write(cache / "archive-v0" / "a", 9000)
    machine.on_path.add("uv")
    machine.command_delete["uv"] = cache

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert machine.commands == [["C:/fake/uv.exe", "cache", "prune"]]
    item = _item(report, "uv")
    assert item.status == "cleaned"
    assert item.bytes_removed == 9000


def test_native_tool_never_uses_force_or_clean_all(machine: FakeMachine) -> None:
    for spec in rg.NATIVE_TOOLS:
        joined = " ".join(spec.argv_tail)
        assert "--force" not in joined or spec.key == "npm"  # npm requires it for `cache clean`
        assert "--all" not in joined and "--packages" not in joined
        assert joined != "cache clean" or spec.key == "yarn"
    uv_spec = next(s for s in rg.NATIVE_TOOLS if s.key == "uv")
    assert uv_spec.argv_tail == ("cache", "prune")


def test_pip_is_still_skipped_when_its_process_is_running(machine: FakeMachine) -> None:
    # pip has no cache lock this module can rely on, so a running pip stays the in-use signal.
    _write(machine.local / "pip" / "Cache" / "a", 100)
    machine.on_path.add("pip")
    machine.running = frozenset({"pip.exe"})

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert machine.commands == []
    assert _item(report, "pip").status == "skipped_in_use"


def test_uv_runs_even_while_uv_exe_is_running_and_waits_on_its_own_lock(
    machine: FakeMachine,
) -> None:
    cache = machine.local / "uv" / "cache"
    _write(cache / "a", 700)
    machine.on_path.add("uv")
    machine.running = frozenset({"uv.exe", "uvx.exe"})
    machine.command_delete["uv"] = cache

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert machine.commands == [["C:/fake/uv.exe", "cache", "prune"]]
    assert _item(report, "uv").status == "cleaned"
    timeout, cmd_env = machine.call_meta[0]
    assert cmd_env["UV_LOCK_TIMEOUT"] == str(int(rg.UV_LOCK_WAIT_SECONDS)) == "1800"
    assert timeout > rg.UV_LOCK_WAIT_SECONDS, "hard kill must come after uv's own timeout"


def test_uv_lock_wait_is_overridable_through_the_env(machine: FakeMachine) -> None:
    _write(machine.local / "uv" / "cache" / "a", 100)
    machine.on_path.add("uv")

    run_regenerable_clean(machine.env(uv_lock_wait_seconds=90.0), apply=True, audit_log_path=None)

    timeout, cmd_env = machine.call_meta[0]
    assert cmd_env["UV_LOCK_TIMEOUT"] == "90"
    assert timeout == 90.0 + rg._LOCK_WAIT_KILL_MARGIN_SECONDS


def test_uv_lock_timeout_is_skipped_in_use_naming_the_wait(machine: FakeMachine) -> None:
    # Real text captured from uv 0.11.14 with its cache `.lock` held by another process.
    _write(machine.local / "uv" / "cache" / "a", 100)
    machine.on_path.add("uv")
    machine.command_effect["uv"] = CommandResult(
        2,
        "",
        "Cache is currently in-use, waiting for other uv processes to finish (use `--force` to "
        "override)\nerror: Timeout (1800s) when waiting for lock on `C:\\c` at `C:\\c\\.lock`, "
        "is another uv process running? You can set `UV_LOCK_TIMEOUT` to increase the timeout.",
    )

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    item = _item(report, "uv")
    assert item.status == "skipped_in_use"
    assert item.detail == "waited 1800 s for uv's cache lock"


def test_uv_subprocess_wall_timeout_is_also_skipped_in_use(machine: FakeMachine) -> None:
    _write(machine.local / "uv" / "cache" / "a", 100)
    machine.on_path.add("uv")
    machine.command_effect["uv"] = CommandResult(-1, "", "timed out", timed_out=True)

    report = run_regenerable_clean(
        machine.env(uv_lock_wait_seconds=120.0), apply=True, audit_log_path=None
    )

    item = _item(report, "uv")
    assert item.status == "skipped_in_use"
    assert item.detail == "waited 120 s for uv's cache lock"


def test_uv_preview_never_runs_the_command_or_waits(machine: FakeMachine) -> None:
    _write(machine.local / "uv" / "cache" / "a", 100)
    machine.on_path.add("uv")
    machine.running = frozenset({"uv.exe"})

    report = run_regenerable_clean(machine.env(), apply=False, audit_log_path=None)

    assert machine.commands == []
    assert _item(report, "uv").status == "would_clean"


def test_no_command_in_the_allow_list_is_ever_run_with_force_for_uv(
    machine: FakeMachine,
) -> None:
    _write(machine.local / "uv" / "cache" / "a", 100)
    machine.on_path.add("uv")
    machine.command_effect["uv"] = CommandResult(2, "", "error: Timeout (1s) waiting for lock")

    run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert all("--force" not in argv for argv in machine.commands)


def test_uv_is_ordered_last_and_progress_callbacks_fire_in_order(machine: FakeMachine) -> None:
    _write(machine.local / "uv" / "cache" / "a", 100)
    _write(machine.local / "pip" / "Cache" / "a", 100)
    machine.on_path.update({"uv", "pip"})
    started: list[tuple[str, bool]] = []
    done: list[str] = []

    report = run_regenerable_clean(
        machine.env(),
        apply=True,
        audit_log_path=None,
        on_item_start=lambda key, _label, waits: started.append((key, waits)),
        on_item_done=lambda item: done.append(item.key),
    )

    keys = [i.key for i in report.items]
    assert keys[-1] == "uv"
    assert keys.index("pip") < keys.index("uv") and keys.index("temp0") < keys.index("uv")
    assert [k for k, _ in started] == keys == done
    assert started[-1] == ("uv", True) and not any(w for _k, w in started[:-1])


def test_a_raising_progress_callback_never_aborts_the_clean(machine: FakeMachine) -> None:
    def boom(*_a: object) -> None:
        raise RuntimeError("ui bug")

    report = run_regenerable_clean(
        machine.env(), apply=True, audit_log_path=None, on_item_start=boom, on_item_done=boom
    )

    assert len(report.items) == len(rg.NATIVE_TOOLS) + 1 + 1 + len(rg.BROWSERS)


def test_native_tool_lock_timeout_is_a_skip_not_a_failure(machine: FakeMachine) -> None:
    _write(machine.local / "pip" / "Cache" / "a", 100)
    machine.on_path.add("pip")
    machine.command_effect["pip"] = CommandResult(
        2, "", "error: Timeout (5s) when waiting for lock on cache dir"
    )

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert _item(report, "pip").status == "skipped_in_use"


def test_native_tool_missing_or_cache_absent_is_reported_not_run(machine: FakeMachine) -> None:
    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    assert _item(report, "uv").status == "skipped_tool_missing"
    machine.on_path.add("uv")
    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    assert _item(report, "uv").status == "skipped_not_present"
    assert machine.commands == []


def test_native_tool_nonzero_exit_is_failed_with_message(machine: FakeMachine) -> None:
    _write(machine.local / "npm-cache" / "a", 100)
    machine.on_path.add("npm")
    machine.command_effect["npm"] = CommandResult(1, "", "npm ERR! boom")
    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    item = _item(report, "npm")
    assert item.status == "failed"
    assert "boom" in item.detail


def test_pip_purge_with_a_held_file_is_a_skip_not_a_failure(machine: FakeMachine) -> None:
    # Real pip 25.1 output shape, measured 2026-10-01: purge removes the other files, then exits 2
    # with a PermissionError traceback when one cache file is open elsewhere. pip has no cache
    # lock to wait on, so the honest classification is "skipped (in use)", not "failed".
    _write(machine.local / "pip" / "Cache" / "a", 100)
    machine.on_path.add("pip")
    machine.command_effect["pip"] = CommandResult(
        2,
        "",
        "ERROR: Exception:\nPermissionError: [WinError 32] The process cannot access the file "
        "because it is being used by another process",
    )
    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)
    item = _item(report, "pip")
    assert item.status == "skipped_in_use"
    assert "open in another process" in item.detail


def test_one_item_crashing_does_not_stop_the_others(machine: FakeMachine) -> None:
    target = _write(machine.temp / "old.bin", 10)
    machine.on_path.add("pip")
    _write(machine.local / "pip" / "Cache" / "a", 10)

    def explode(_a: Sequence[str], _t: float, _e: dict[str, str]) -> CommandResult:
        raise RuntimeError("tool runner bug")

    report = run_regenerable_clean(
        machine.env(run_command=explode), apply=True, audit_log_path=None
    )

    assert _item(report, "pip").status == "failed"
    assert not target.exists()


# --- allow-list is closed ----------------------------------------------------------------------


def test_non_allowlisted_paths_are_never_touched(machine: FakeMachine, tmp_path: Path) -> None:
    bystanders = [
        _write(machine.home / "Documents" / "thesis.docx", 10, age_days=900),
        _write(machine.home / "Downloads" / "installer.exe", 10, age_days=900),
        _write(machine.local / "Programs" / "App" / "app.exe", 10, age_days=900),
        _write(machine.local / "uv" / "python" / "cpython" / "python.exe", 10, age_days=900),
        _write(machine.home / ".cache" / "huggingface" / "hub" / "model.bin", 10, age_days=900),
        _write(machine.home / ".venv-like" / "lib" / "x.py", 10, age_days=900),
        _write(tmp_path / "elsewhere" / "file.txt", 10, age_days=900),
        _write(machine.local / "CrashDumpsNot" / "x.dmp", 10, age_days=900),
    ]
    machine.on_path.update({"uv", "pip", "npm", "conda", "yarn"})
    _write(machine.temp / "old.bin", 10)  # something IS eligible, so the run does real work

    run_regenerable_clean(machine.env(), apply=True, audit_log_path=None)

    assert all(p.exists() for p in bystanders)


def test_temp_root_level_symlink_escape_is_not_followed(
    machine: FakeMachine, tmp_path: Path
) -> None:
    # A top-level entry whose realpath is outside the root and which is NOT itself a reparse
    # point can only be a path-resolution trick; it is skipped by the containment check.
    outside = _write(tmp_path / "outside" / "x.txt", 10)
    assert rg._resolve_contained(outside, machine.temp) is False
    assert rg._resolve_contained(machine.temp / "child", machine.temp) is True
    assert rg._resolve_contained(machine.temp, machine.temp) is False


# --- audit log ---------------------------------------------------------------------------------


def test_every_item_and_a_summary_are_written_to_the_audit_log(
    machine: FakeMachine, tmp_path: Path
) -> None:
    _write(machine.temp / "old.bin", 123)
    audit = tmp_path / "data" / "regenerable_audit.jsonl"

    report = run_regenerable_clean(machine.env(), apply=True, audit_log_path=audit)

    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    summary = lines[-1]
    assert summary["summary"] is True
    assert summary["bytes_removed"] == report.bytes_removed == 123
    keys = {line.get("key") for line in lines[:-1]}
    assert {"uv", "pip", "npm", "conda", "yarn", "temp0", "crash0", "chrome", "edge"} <= keys
    assert all(line["run_id"] == report.run_id for line in lines)
    temp_line = next(line for line in lines if line.get("key") == "temp0")
    assert temp_line["files_removed"] == 1 and temp_line["apply"] is True


def test_unwritable_audit_log_does_not_fail_the_run(machine: FakeMachine, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    target = _write(machine.temp / "old.bin", 10)
    report = run_regenerable_clean(
        machine.env(), apply=True, audit_log_path=blocker / "sub" / "audit.jsonl"
    )
    assert not target.exists()
    assert report.bytes_removed == 10


# --- real Win32 probes (not faked) -------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 sharing semantics")
def test_real_open_handle_probe_detects_a_held_file(tmp_path: Path) -> None:
    held = tmp_path / "held.txt"
    held.write_text("data")
    free = tmp_path / "free.txt"
    free.write_text("data")
    assert rg.file_has_open_handle(str(free)) is False
    with held.open("rb"):
        assert rg.file_has_open_handle(str(held)) is True
    assert rg.file_has_open_handle(str(tmp_path / "gone.txt")) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp snapshot")
def test_real_process_enumeration_sees_this_interpreter() -> None:
    names = rg._real_running_process_names()
    assert Path(sys.executable).name.lower() in names


def test_time_module_not_used_for_age_in_tests() -> None:
    # Guards the fixture itself: NOW is a fixed epoch far from the wall clock, so a test that
    # passed only because of the real clock would fail here.
    assert abs(time.time() - NOW) > DAY


# --- ADR-0036: re-stat immediately before each unlink ------------------------------------------


def test_aged_file_appended_between_plan_and_delete_is_skipped(machine: FakeMachine) -> None:
    # The plan (walk) judged `busy.log` aged and idle; the open-handle probe (which runs right
    # before the unlink) is where a writer sneaks in. No handle is held at probe time, so only
    # the re-stat can save it.
    busy = _write(machine.temp / "busy.log", 100)
    other = _write(machine.temp / "idle.log", 100)

    def appender(path: str) -> bool:
        if path == str(busy):
            with busy.open("ab") as fh:
                fh.write(b"y" * 50)
        return False

    report = run_regenerable_clean(
        machine.env(has_open_handle=appender), apply=True, audit_log_path=None
    )

    item = _item(report, "temp0")
    assert busy.exists() and busy.read_bytes().endswith(b"y" * 50)
    assert not other.exists(), "an untouched aged file is still deleted"
    assert item.files_skipped_in_use == 1
    assert str(busy) in item.skipped_paths


def test_aged_file_inside_directory_touched_after_plan_is_skipped(machine: FakeMachine) -> None:
    inner = _write(machine.temp / "dir" / "a.bin", 10)
    sibling = _write(machine.temp / "dir" / "b.bin", 10)
    _age_dirs(machine.temp / "dir", 30)

    def toucher(path: str) -> bool:
        if path == str(inner):
            stamp = NOW - 60.0  # same size, mtime now inside the age floor
            os.utime(inner, (stamp, stamp))
        return False

    report = run_regenerable_clean(
        machine.env(has_open_handle=toucher), apply=True, audit_log_path=None
    )

    assert inner.exists()
    assert not sibling.exists()
    assert _item(report, "temp0").files_skipped_in_use == 1


def test_age_floor_is_rechecked_against_the_clock_at_delete_time(machine: FakeMachine) -> None:
    # mtime and size are UNCHANGED since the walk, so only the age-floor re-check can fire:
    # the injected clock says the file is old at plan time and young at delete time.
    victim = _write(machine.temp / "v.log", 100)
    probed = {"done": False}

    def clock() -> float:
        return NOW - 30 * DAY if probed["done"] else NOW

    def probe(_path: str) -> bool:
        probed["done"] = True  # the handle probe runs after the plan, right before the re-stat
        return False

    report = run_regenerable_clean(
        machine.env(now=clock, has_open_handle=probe), apply=True, audit_log_path=None
    )

    assert victim.exists()
    assert _item(report, "temp0").files_skipped_in_use == 1


def test_browser_cache_file_appended_between_walk_and_delete_is_skipped(
    machine: FakeMachine,
) -> None:
    profile = _chrome_profile(machine)
    busy = _write(profile / "Cache" / "Cache_Data" / "f_1", 100, age_days=0.1)
    idle = _write(profile / "Cache" / "Cache_Data" / "f_2", 100, age_days=0.1)

    def appender(path: str) -> bool:
        if path == str(busy):
            with busy.open("ab") as fh:
                fh.write(b"y" * 50)
        return False

    report = run_regenerable_clean(
        machine.env(has_open_handle=appender), apply=True, audit_log_path=None
    )

    assert busy.exists()
    assert not idle.exists(), "browser caches have no age floor; an untouched file still goes"
    assert _item(report, "chrome").files_skipped_in_use == 1
