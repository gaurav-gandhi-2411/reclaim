from __future__ import annotations

# Source-text gate for the uninstaller's leftover handling (Inno Setup [Code] cannot be run in CI).
# Found on a real uninstall (2026-10-10): an empty {app} folder stayed behind after the user chose
# to delete the data folder (Inno's own empty-dir removal runs before the usPostUninstall prompt,
# while data\ still exists), and config.toml -- which holds the user's safety exclusions -- was
# removed by Inno's file pass even when the user kept their data.
import re
from pathlib import Path

_ISS = (Path(__file__).parent.parent / "packaging" / "reclaim.iss").read_text(encoding="utf-8")


def test_config_toml_survives_the_file_removal_pass() -> None:
    line = next(ln for ln in _ISS.splitlines() if 'DestName: "config.toml"' in ln)
    assert "onlyifdoesntexist" in line
    assert "uninsneveruninstall" in line


def test_leftover_check_does_not_flag_the_preserved_config() -> None:
    helper = _ISS[_ISS.index("FindFirst(AppDir") :]
    helper = helper[: helper.index("FindClose")]
    assert "'config.toml'" in helper and "'data'" in helper


def test_deleting_data_also_removes_config_and_the_empty_app_dir_in_that_order() -> None:
    branch = _ISS[_ISS.index("if Response = IDYES then") :]
    branch = branch[: branch.index("end;\n    end;")]
    order = [
        branch.index("DelTree(DataDir"),
        branch.index("DeleteFile(AppDir + '\\config.toml')"),
        branch.index("RemoveDir(AppDir)"),
    ]
    assert order == sorted(order)


def test_app_dir_is_only_removed_by_the_non_recursive_call() -> None:
    # RemoveDir fails on a non-empty directory; a recursive delete of {app} would not.
    assert not re.search(r"DelTree\(\s*(AppDir|ExpandConstant\('\{app\}'\))", _ISS)
