from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="matches project's Windows-only scope"
)


def _load_git_guard() -> ModuleType:
    """Loads scripts/git_guard.py by file path — it's a standalone operational script (see the
    scripts/ convention, rule 16), not a package, so this avoids any sys.path/import-mode
    ambiguity rather than relying on `scripts` being importable as `scripts.git_guard`."""
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "git_guard.py"
    spec = importlib.util.spec_from_file_location("git_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


git_guard = _load_git_guard()


# --- requested_scope -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["config", "--global", "user.email", "x"], "global"),
        (["config", "--system", "user.email", "x"], "system"),
        (["config", "--global", "--unset", "user.email"], "global"),
        (["config", "--local", "user.email", "x"], None),
        (["config", "user.email", "x"], None),  # implicit local (no scope flag) — not blocked
        (["-c", "user.email=x", "commit", "-m", "hi"], None),  # not a config subcommand
        (["status", "--porcelain"], None),
        (["init", "--quiet"], None),
        ([], None),
    ],
)
def test_requested_scope(argv: list[str], expected: str | None) -> None:
    assert git_guard.requested_scope(argv) == expected


# --- sandbox_violation -----------------------------------------------------------------------


def test_global_unsandboxed_is_a_violation() -> None:
    violation = git_guard.sandbox_violation("global", {})
    assert violation is not None
    assert "--global" in violation
    assert "GIT_CONFIG_GLOBAL" in violation


def test_system_unsandboxed_is_a_violation() -> None:
    violation = git_guard.sandbox_violation("system", {})
    assert violation is not None
    assert "GIT_CONFIG_SYSTEM" in violation


def test_global_sandboxed_via_git_config_global_env_var_passes(tmp_path: Path) -> None:
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "sandbox-gitconfig")}
    assert git_guard.sandbox_violation("global", env) is None


def test_system_sandboxed_via_git_config_system_env_var_passes(tmp_path: Path) -> None:
    env = {"GIT_CONFIG_SYSTEM": str(tmp_path / "sandbox-systemconfig")}
    assert git_guard.sandbox_violation("system", env) is None


def test_global_sandboxed_via_explicit_sandbox_flag_plus_home_passes(tmp_path: Path) -> None:
    env = {"RECLAIM_GIT_SANDBOX_HOME": "1", "HOME": str(tmp_path)}
    assert git_guard.sandbox_violation("global", env) is None


def test_sandbox_flag_alone_without_home_is_still_a_violation() -> None:
    """The flag alone isn't enough — HOME/USERPROFILE must also actually be set, or the flag is
    a no-op claim with nothing behind it."""
    env = {"RECLAIM_GIT_SANDBOX_HOME": "1"}
    assert git_guard.sandbox_violation("global", env) is not None


def test_falsy_sandbox_flag_is_a_violation() -> None:
    env = {"RECLAIM_GIT_SANDBOX_HOME": "0", "HOME": "C:/somewhere"}
    assert git_guard.sandbox_violation("global", env) is not None


def test_home_redirect_alone_without_the_flag_is_still_a_violation() -> None:
    """A merely-present HOME/USERPROFILE is not itself proof of intentional sandboxing (it's
    load-bearing for lots of things) — the explicit flag is what makes it unambiguous."""
    env = {"HOME": "C:/some/redirected/path"}
    assert git_guard.sandbox_violation("global", env) is not None


# --- main(): blocked path never touches git at all --------------------------------------------


def test_main_blocks_unsandboxed_global_write_and_never_calls_git(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("git_guard must never invoke git for a blocked command")

    monkeypatch.setattr(git_guard.subprocess, "run", _boom)

    exit_code = git_guard.main(
        ["config", "--global", "user.email", "should-never-be-set@example.com"], env={}
    )
    assert exit_code == 1
    assert "refusing" in capsys.readouterr().err


def test_main_blocks_unsandboxed_system_write(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = git_guard.main(["config", "--system", "user.name", "x"], env={})
    assert exit_code == 1
    assert "--system" in capsys.readouterr().err


# --- main(): passthrough / sandboxed paths actually invoke git, and prove the real config -----
# --- is never touched -------------------------------------------------------------------------


def _real_global_user_email() -> str | None:
    """Reads the REAL global user.email (no GIT_CONFIG_GLOBAL override) — used as a before/after
    baseline to prove a sandboxed guarded call never touches it."""
    git_exe = shutil.which("git")
    assert git_exe is not None
    result = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        [git_exe, "config", "--global", "--get", "user.email"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def test_main_passthrough_for_non_config_command(tmp_path: Path) -> None:
    exit_code = git_guard.main(["init", "--quiet", str(tmp_path / "repo")], env=dict(os.environ))
    assert exit_code == 0
    assert (tmp_path / "repo" / ".git").is_dir()


def test_main_sandboxed_global_write_lands_only_in_the_redirected_file_never_the_real_config(
    tmp_path: Path,
) -> None:
    baseline = _real_global_user_email()

    sandbox_config = tmp_path / "sandbox-gitconfig"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(sandbox_config)}
    exit_code = git_guard.main(
        ["config", "--global", "user.email", "sandboxed@example.com"], env=env
    )

    assert exit_code == 0
    assert sandbox_config.exists()
    assert "sandboxed@example.com" in sandbox_config.read_text(encoding="utf-8")
    # The real global config's own user.email is byte-for-byte unaffected by the call above.
    assert _real_global_user_email() == baseline


def test_main_rejects_and_does_not_write_when_git_config_global_points_at_a_bad_path() -> None:
    """Even a "sandboxed" call still goes through the real git binary and can fail normally
    (e.g. an unwritable directory) — the guard doesn't swallow or mask a genuine git error."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "Z:/definitely/does/not/exist/gitconfig"}
    exit_code = git_guard.main(["config", "--global", "user.email", "x"], env=env)
    assert exit_code != 0
