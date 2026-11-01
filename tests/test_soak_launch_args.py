"""Launch-argv tests for packaging/smoke/soak_serve.py: the soak's server must never read the
real installed config.toml, and every per-user path flag must point under the soak's out-dir.
Pure functions only; no server or reclaim.exe is ever started."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "smoke" / "soak_serve.py"
_PATH_FLAGS = (
    "--config",
    "--db",
    "--vault-dir",
    "--manifest",
    "--mode-log",
    "--first-run-state",
    "--log-path",
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("soak_serve", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soak_serve"] = mod
    spec.loader.exec_module(mod)
    return mod


soak = _load()


def _flag_value(cmd: list[str], flag: str) -> str:
    assert cmd.count(flag) == 1, f"{flag} must appear exactly once in {cmd}"
    return cmd[cmd.index(flag) + 1]


def test_default_config_is_an_empty_file_in_out_dir(tmp_path: Path) -> None:
    cfg = soak.resolve_soak_config(tmp_path, None)
    assert cfg == tmp_path / "config.toml"
    assert cfg.is_file()
    assert cfg.read_bytes() == b""


def test_operator_config_is_passed_through_and_nothing_is_written(tmp_path: Path) -> None:
    operator = tmp_path / "elsewhere" / "mine.toml"
    operator.parent.mkdir()
    operator.write_text("# mine", encoding="utf-8")
    cfg = soak.resolve_soak_config(tmp_path, str(operator))
    assert cfg == operator
    assert not (tmp_path / "config.toml").exists()
    assert operator.read_text(encoding="utf-8") == "# mine"  # untouched


def test_command_pins_config_and_every_path_flag_under_out_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    cfg = soak.resolve_soak_config(tmp_path, None)
    cmd = soak.build_serve_command("reclaim.exe", "", 8421, data, cfg)
    assert cmd[:4] == ["reclaim.exe", "serve", "--port", "8421"]
    for flag in _PATH_FLAGS:
        value = Path(_flag_value(cmd, flag))
        assert value.is_relative_to(tmp_path), f"{flag} escapes out-dir: {value}"
    assert Path(_flag_value(cmd, "--config")) == tmp_path / "config.toml"


def test_command_passes_operator_config_and_exe_args(tmp_path: Path) -> None:
    operator = tmp_path / "ops.toml"
    cmd = soak.build_serve_command("python.exe", "-m reclaim", 9000, tmp_path / "data", operator)
    assert cmd[:6] == ["python.exe", "-m", "reclaim", "serve", "--port", "9000"]
    assert _flag_value(cmd, "--config") == str(operator)


def test_missing_operator_config_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "typo.toml"
    with pytest.raises(FileNotFoundError, match="--config is not a file"):
        soak.resolve_soak_config(tmp_path, str(missing))
    with pytest.raises(FileNotFoundError):  # a directory is not a config file either
        soak.resolve_soak_config(tmp_path, str(tmp_path))
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="soak_serve.main is Windows-only")
def test_main_exits_3_on_missing_config_before_launching_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code = soak.main(
        ["--exe", "reclaim-never-launched.exe", "--config", str(tmp_path / "nope.toml"),
         "--out-dir", str(out)]
    )  # fmt: skip
    assert code == 3
    assert "--config is not a file" in capsys.readouterr().err
    assert not (out / "fixture").exists()  # refused before the fixture build
    assert not (out / "server_stdout.log").exists()  # and before any server launch
