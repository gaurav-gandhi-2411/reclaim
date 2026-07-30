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


def test_build_script_version_matches_pyproject() -> None:
    # The Nuitka --product-version flag moved out of README.md and into build_installer.ps1
    # (packaging/build_installer.ps1) when the manual build command became a script -- this is
    # now the single place that flag is declared, so it's the one this gate has to watch.
    script_text = (_REPO_ROOT / "packaging" / "build_installer.ps1").read_text(encoding="utf-8")
    versions = re.findall(r"--product-version=([\w.\-]+)", script_text)
    assert versions, "packaging/build_installer.ps1 no longer documents a --product-version flag"
    for found in versions:
        assert found == _pyproject_version(), (
            f"build_installer.ps1 declares --product-version={found} but pyproject.toml says "
            f"{_pyproject_version()!r} — running the build script ships a mislabeled exe."
        )
