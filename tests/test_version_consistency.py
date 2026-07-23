from __future__ import annotations

# Version-consistency gate.
#
# The v1.0.0 release shipped an installer whose metadata said 0.1.0 — pyproject.toml, the Inno
# Setup script, and the README's documented Nuitka command had all drifted from the release tag.
# This test makes that class of mismatch a CI failure instead of a launch-audit finding: every
# place a version string is declared must agree with pyproject.toml's, which is the single source
# of truth. (The git tag itself can't be checked here — it doesn't exist until release time; the
# release checklist in packaging/reclaim.iss's header comment covers that step.)
import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def _pyproject_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_installer_script_version_matches_pyproject() -> None:
    iss_text = (_REPO_ROOT / "packaging" / "reclaim.iss").read_text(encoding="utf-8")
    match = re.search(r'#define MyAppVersion "([^"]+)"', iss_text)
    assert match is not None, "packaging/reclaim.iss no longer defines MyAppVersion"
    assert match.group(1) == _pyproject_version(), (
        f"packaging/reclaim.iss says {match.group(1)!r} but pyproject.toml says "
        f"{_pyproject_version()!r} — the installer would ship mislabeled. Update the .iss."
    )


def test_readme_documented_build_command_version_matches_pyproject() -> None:
    readme_text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    versions = re.findall(r"--product-version=([\w.\-]+)", readme_text)
    assert versions, "README no longer documents a --product-version flag"
    for found in versions:
        assert found == _pyproject_version(), (
            f"README documents --product-version={found} but pyproject.toml says "
            f"{_pyproject_version()!r} — a reader following the docs builds a mislabeled exe."
        )
