from __future__ import annotations

# ruff: noqa: PTH106, PTH111, PTH118
# These tests exercise str/os.path based code paths on purpose.
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path

import pytest

from reclaim import autoclean_schedule, executor, notifications, regenerable, safety_env
from reclaim.regenerable import CommandResult, RegenerableEnv
from reclaim.safety_env import RealProfileAccessError

# Hermetic-test guard (CLAUDE.md rule 7). Every "real profile" target used below is a path that
# does NOT exist (a uuid-named child of a real root), and every filesystem primitive is replaced
# by a recording no-op where a broken guard could reach it, so even with the guard removed
# nothing real is deleted -- the tests simply fail.


def _real_local_appdata() -> str:
    roots = safety_env.real_profile_roots()
    for root in roots:
        if root.endswith(os.path.join("appdata", "local")):
            return root
    return roots[0]


def _ghost_real_path(*parts: str) -> str:
    """A path inside the REAL profile that does not exist."""
    return os.path.join(_real_local_appdata(), f"reclaim-hermetic-ghost-{uuid.uuid4().hex}", *parts)


def _within(path: str | Path, root: str | Path) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r + os.sep)


class _Spy:
    """Replaces os.unlink/os.rmdir/shutil.rmtree/os.makedirs with recording no-ops."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str]] = []
        monkeypatch.setattr(os, "unlink", lambda p, *a, **k: self.calls.append(("unlink", str(p))))
        monkeypatch.setattr(os, "rmdir", lambda p, *a, **k: self.calls.append(("rmdir", str(p))))
        monkeypatch.setattr(
            shutil, "rmtree", lambda p, *a, **k: self.calls.append(("rmtree", str(p)))
        )
        monkeypatch.setattr(
            os, "makedirs", lambda p, *a, **k: self.calls.append(("makedirs", str(p)))
        )


def _fake_env(local: Path, run_log: list[object]) -> RegenerableEnv:
    def _no_command(argv: object, timeout: float, env: dict[str, str]) -> CommandResult:
        run_log.append(argv)
        return CommandResult(0, "", "")

    return RegenerableEnv(
        home=local.parent,
        local_appdata=local,
        temp_roots=(local / "Temp",),
        crash_dump_roots=(local / "CrashDumps",),
        which=lambda _name: None,
        run_command=_no_command,
        has_open_handle=lambda _p: False,
        running_process_names=lambda: frozenset(),
    )


# --- (a) from_os_environment inside the test session is hermetic ------------------------------


def test_from_os_environment_roots_are_not_the_real_profile(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp()
    env = RegenerableEnv.from_os_environment()
    redirected = [
        env.home,
        env.local_appdata,
        env.temp_roots[0],
        *env.crash_dump_roots,
        *env.pytest_temp_roots,
    ]
    for root in redirected:
        assert _within(root, base), f"{root} escaped the test sandbox"
    # SYSTEMROOT is not redirected (Windows needs it); its Temp dir is covered by the guard.
    windows_temp = env.temp_roots[1]
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(windows_temp)


# --- (b) an apply pointed at the REAL profile is refused before any mutation ------------------


def test_apply_against_the_real_profile_is_refused_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _Spy(monkeypatch)
    run_log: list[object] = []
    env = _fake_env(Path(_ghost_real_path()), run_log)
    with pytest.raises(RealProfileAccessError):
        regenerable.run_regenerable_clean(env, apply=True, audit_log_path=None)
    assert spy.calls == []
    assert run_log == []


@pytest.mark.parametrize("func_name", ["_delete_one_file", "_delete_tree_contents"])
def test_regenerable_delete_primitives_refuse_the_real_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, func_name: str
) -> None:
    spy = _Spy(monkeypatch)
    env = _fake_env(tmp_path / "local", [])
    result = regenerable.RegenerableItemResult("browser", "k", "l", "nothing_to_clean")
    ghost = _ghost_real_path("Cache", "f_000001")
    with pytest.raises(RealProfileAccessError):
        if func_name == "_delete_one_file":
            regenerable._delete_one_file(ghost, os.stat_result(range(10)), env=env, result=result)
        else:
            regenerable._delete_tree_contents(ghost, env=env, result=result, remove_top=True)
    assert spy.calls == []


def test_regenerable_reparse_removal_refuses_the_real_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = _Spy(monkeypatch)
    env = _fake_env(tmp_path / "local", [])
    result = regenerable.RegenerableItemResult("browser", "k", "l", "nothing_to_clean")
    with pytest.raises(RealProfileAccessError):
        regenerable._remove_reparse_entry(
            _ghost_real_path("link"), os.stat_result(range(10)), result, env=env
        )
    assert spy.calls == []


def test_executor_delete_primitives_refuse_the_real_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spy = _Spy(monkeypatch)
    ghost = _ghost_real_path("x")
    with pytest.raises(RealProfileAccessError):
        executor.unlink_clear_readonly(ghost)
    with pytest.raises(RealProfileAccessError):
        executor.rmtree_reparse_point_safe(ghost)
    with pytest.raises(RealProfileAccessError):
        executor._atomic_move(Path(ghost), tmp_path / "vault" / "x", is_dir=False)
    with pytest.raises(RealProfileAccessError):
        executor._atomic_move(tmp_path / "src", Path(ghost), is_dir=False)
    assert spy.calls == []


def test_key_store_delete_refuses_the_real_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from reclaim import anthropic_key_store

    ghost = Path(_ghost_real_path("anthropic_key.bin"))
    with pytest.raises(RealProfileAccessError):
        anthropic_key_store.delete_key(ghost)


def test_junction_into_a_real_root_is_resolved_and_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard resolves links: a path that LOOKS harmless but is really inside a real root.
    The "real" root here is a stand-in dir under tmp, so no actual profile is ever linked."""
    pretend_real = tmp_path / "pretend-real-profile"
    pretend_real.mkdir()
    monkeypatch.setattr(safety_env, "_real_roots", (os.path.normcase(str(pretend_real)),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    link = tmp_path / "elsewhere" / "sneaky"
    link.parent.mkdir()
    # A directory junction needs no privilege (unlike a symlink).
    made = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["cmd", "/c", "mklink", "/J", str(link), str(pretend_real)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("junction creation failed here")
    try:
        with pytest.raises(RealProfileAccessError):
            safety_env.assert_not_real_profile_under_pytest(link / "child")
        # ...but removing the link ENTRY itself never touches its target, so it is allowed.
        safety_env.assert_not_real_profile_under_pytest(link, link_itself=True)
    finally:
        os.rmdir(link)  # removes only the junction entry, never its target


# --- (c) the real runners refuse under pytest -------------------------------------------------


def test_real_schtasks_powershell_and_native_runner_refuse_under_pytest() -> None:
    with pytest.raises(RealProfileAccessError):
        autoclean_schedule.run_schtasks(["/?"])
    with pytest.raises(RealProfileAccessError):
        autoclean_schedule.run_powershell("exit 0", "reclaim-hermetic-probe")
    with pytest.raises(RealProfileAccessError):
        regenerable._run_command([sys.executable, "-c", "pass"], 5.0, {})


def test_genuine_toast_library_is_refused_but_an_injected_fake_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    genuine = types.ModuleType("windows_toasts")
    genuine.__file__ = "C:/somewhere/site-packages/windows_toasts/__init__.py"
    monkeypatch.setitem(sys.modules, "windows_toasts", genuine)
    with pytest.raises(RealProfileAccessError):
        notifications._refuse_real_toast_under_pytest()
    monkeypatch.setitem(sys.modules, "windows_toasts", types.ModuleType("windows_toasts"))
    notifications._refuse_real_toast_under_pytest()  # a fake (no __file__) is fine


# --- (d) the explicit opt-in bypasses ---------------------------------------------------------


def test_opt_in_env_var_bypasses_the_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_real = tmp_path / "pretend-real-profile"
    monkeypatch.setattr(safety_env, "_real_roots", (os.path.normcase(str(fake_real)),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    target = fake_real / "Cache" / "f"
    monkeypatch.delenv(safety_env.OPT_IN_ENV_VAR, raising=False)
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(target)
    with pytest.raises(RealProfileAccessError):
        safety_env.refuse_real_side_effect_under_pytest("spawn something")
    monkeypatch.setenv(safety_env.OPT_IN_ENV_VAR, "1")
    safety_env.assert_not_real_profile_under_pytest(target)
    safety_env.refuse_real_side_effect_under_pytest("spawn something")
    monkeypatch.setenv(safety_env.OPT_IN_ENV_VAR, "0")  # only the literal "1" opts in
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(target)


def test_registered_sandbox_root_is_allowed_inside_a_real_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_real = tmp_path / "pretend-real-profile"
    monkeypatch.setattr(safety_env, "_real_roots", (os.path.normcase(str(fake_real)),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    safety_env.register_sandbox_root(fake_real / "pytest-of-me" / "pytest-1")
    safety_env.assert_not_real_profile_under_pytest(fake_real / "pytest-of-me" / "pytest-1" / "t")
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(fake_real / "pytest-of-me" / "pytest-2")


# --- (e) the autouse redirect covers every variable -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "USERPROFILE", "HOME", "PROGRAMDATA"],
)
def test_autouse_fixture_redirects_each_env_var(
    name: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    value = os.environ[name]
    assert _within(value, tmp_path_factory.getbasetemp())
    assert os.path.normcase(os.path.realpath(value)) not in safety_env.real_profile_roots()


def test_autouse_fixture_redirects_home_and_temp_helpers(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp()
    home = os.environ["USERPROFILE"]
    assert _within(Path.home(), base)
    assert _within(os.path.expanduser("~"), base)
    assert _within(tempfile.gettempdir(), base)
    assert _within(os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"], base)
    assert os.path.normcase(os.path.realpath(Path.home())) == os.path.normcase(
        os.path.realpath(home)
    )
    assert os.path.normcase(os.path.realpath(tempfile.gettempdir())) == os.path.normcase(
        os.path.realpath(os.environ["TEMP"])
    )


def test_a_later_monkeypatch_still_wins_over_the_autouse_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "mine"))
    assert RegenerableEnv.from_os_environment().local_appdata == tmp_path / "mine"


# --- (f) outside pytest the guard is a no-op --------------------------------------------------


def test_guard_is_a_no_op_when_not_running_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ghost = _ghost_real_path("x")
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(ghost)
    monkeypatch.setattr(safety_env, "running_under_pytest", lambda: False)
    safety_env.assert_not_real_profile_under_pytest(ghost)
    safety_env.refuse_real_side_effect_under_pytest("spawn something")


def test_detector_fails_closed_on_either_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert safety_env.running_under_pytest() is True  # "pytest" is in sys.modules
    monkeypatch.delitem(sys.modules, "pytest")
    assert safety_env.running_under_pytest() is False
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert safety_env.running_under_pytest() is True


def test_a_child_process_inherits_the_real_roots_and_sandbox_not_the_redirected_env(
    tmp_path: Path,
) -> None:
    """A spawned child (the crash-recovery harness shape) sees only the REDIRECTED environment.
    It must still know what "real" is (published by the parent) and that the sandbox is allowed,
    or it would either treat the redirected dirs as real (refusing legitimate tmp work) or not
    know the real ones at all."""
    code = (
        "import sys; from reclaim import safety_env as s;"
        "s.assert_not_real_profile_under_pytest(sys.argv[1]);"
        "print(';'.join(s.real_profile_roots()))"
    )
    ok = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", code, str(tmp_path / "inside-sandbox")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip().split(";") == list(safety_env.real_profile_roots())
    refused = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter
        [sys.executable, "-c", code, _ghost_real_path("x")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "RealProfileAccessError" in refused.stderr


# --- (g) UNC / device / loopback spellings cannot bypass a real root; sandbox env hardening ----
# Every "real" root here is a stand-in dir under tmp_path registered through the module's own
# attribute; the real profile is never involved and nothing is deleted (the guard only raises).


def _stand_in_real(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    real = tmp_path / "pretend-real-profile"
    real.mkdir()
    monkeypatch.setattr(safety_env, "_real_roots", (safety_env._norm(real),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    monkeypatch.setattr(safety_env, "_sandbox_inherited", True)  # do not load the env var
    return real


def _spellings(path: Path) -> list[str]:
    """The same local path through every UNC/device/loopback form (path must be on a drive)."""
    drive, rest = os.path.splitdrive(str(path))
    letter = drive[0]
    host = socket.gethostname()
    return [
        f"\\\\localhost\\{letter}$" + rest,
        f"\\\\LocalHost\\{letter}$" + rest,
        f"\\\\?\\UNC\\localhost\\{letter}$" + rest,
        f"\\\\127.0.0.1\\{letter}$" + rest,
        f"\\\\[::1]\\{letter}$" + rest,
        f"\\\\0--1.ipv6-literal.net\\{letter}$" + rest,
        f"\\\\{host}\\{letter}$" + rest,
        f"\\\\.\\{letter}:" + rest,
        f"\\\\?\\{letter}:" + rest,
        f"//localhost/{letter}$" + rest.replace("\\", "/"),
    ]


def test_unc_device_and_loopback_spellings_of_a_real_root_are_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    for spelling in _spellings(real / "Cache" / "f_000001"):
        assert safety_env._norm(spelling).startswith(safety_env._norm(real)), (
            f"{spelling!r} did not normalise to the local drive path"
        )
        with pytest.raises(RealProfileAccessError):
            safety_env.assert_not_real_profile_under_pytest(spelling)


def test_unmappable_device_paths_naming_a_real_root_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    tail = os.path.splitdrive(str(real))[1] + "\\Cache"
    for spelling in (
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume3" + tail,
        "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}" + tail,
        "\\\\otherhost\\share" + tail,
    ):
        with pytest.raises(RealProfileAccessError):
            safety_env.assert_not_real_profile_under_pytest(spelling)
    # ...but an unmappable path that names no real root is not refused (and never touches the net).
    safety_env.assert_not_real_profile_under_pytest("\\\\otherhost\\share\\unrelated\\x")


def test_unc_spellings_are_a_no_op_outside_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    monkeypatch.setattr(safety_env, "running_under_pytest", lambda: False)
    for spelling in _spellings(real / "x"):
        safety_env.assert_not_real_profile_under_pytest(spelling)
    safety_env.assert_not_real_profile_under_pytest("\\\\?\\GLOBALROOT" + str(real)[2:])


def _load_env_sandboxes(monkeypatch: pytest.MonkeyPatch, raw: str) -> list[str]:
    monkeypatch.setattr(safety_env, "_sandbox_roots", [])
    monkeypatch.setattr(safety_env, "_sandbox_inherited", False)
    monkeypatch.setenv(safety_env._INHERIT_SANDBOX_ENV_VAR, raw)
    return list(safety_env._sandboxes())


def test_sandbox_env_entries_are_normalised_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    box = real / "pytest-of-me" / "pytest-1"
    raw = os.pathsep.join(["", str(box).swapcase(), "  "])
    sandboxes = _load_env_sandboxes(monkeypatch, raw)
    assert sandboxes == [safety_env._norm(box)]
    safety_env.assert_not_real_profile_under_pytest(box / "t")  # allowed inside a real root
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(real / "pytest-of-me" / "pytest-2")


def test_sandbox_env_entry_that_is_or_contains_a_real_root_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    drive = os.path.splitdrive(str(real))[0] + "\\"
    ancestors = [str(real), str(real.parent), str(real.parent.parent), drive]
    assert _load_env_sandboxes(monkeypatch, os.pathsep.join(ancestors)) == []
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(real / "Cache" / "f")
    # a UNC spelling of the real root as a "sandbox" is no way around it either
    assert _load_env_sandboxes(monkeypatch, _spellings(real)[0]) == []
    with pytest.raises(RealProfileAccessError):
        safety_env.assert_not_real_profile_under_pytest(real / "Cache" / "f")


def test_sandbox_env_is_not_consulted_outside_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _stand_in_real(monkeypatch, tmp_path)
    monkeypatch.setattr(safety_env, "running_under_pytest", lambda: False)
    monkeypatch.setattr(safety_env, "_sandbox_inherited", False)
    monkeypatch.setenv(safety_env._INHERIT_SANDBOX_ENV_VAR, str(real))
    safety_env.assert_not_real_profile_under_pytest(real / "x")
    assert safety_env._sandbox_inherited is False  # production never even parsed it
