from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from reclaim import cli, logging_config, safety_env
from reclaim.api import app as api_app
from reclaim.api import state as api_state

# Repo-root conftest on purpose: it applies to `tests/` AND `evals/` (scripts/verify.py runs
# both; a conftest under tests/ alone would leave every eval able to reach the real profile).

# Captured at IMPORT, before any fixture can redirect an environment variable: these are the
# machine's REAL roots, which `reclaim.safety_env` refuses to let any destructive code touch
# under pytest. Tests reach them via `safety_env.real_profile_roots()`.
REAL_PROFILE_ROOTS = safety_env.capture_real_profile_roots()


@pytest.fixture(autouse=True)
def _hermetic_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """CLAUDE.md rule 7: no test may reach the real profile. A verifier probe once ran a
    `regenerable` apply with `local_appdata`/`home` still real (only TEMP redirected) and deleted
    the owner's real browser caches. Every root variable now points inside pytest's basetemp, and
    `Path.home()`, `os.path.expanduser("~")` and `tempfile.gettempdir()` agree with it.

    Home is the basetemp itself (not a per-test dir) so every test's `tmp_path` is "inside home":
    the API's scan-scope check (`Path.home()` is the default allowed root) otherwise answers 403
    for every tree a test builds. LOCALAPPDATA/APPDATA/TEMP/PROGRAMDATA are per-test dirs.
    `SYSTEMROOT` is deliberately NOT redirected (Windows itself and child interpreters need it);
    the Windows Temp dir under it is covered by the product-side guard instead. Tests that need
    another value monkeypatch after this fixture and win."""
    home = tmp_path_factory.getbasetemp()
    safety_env.register_sandbox_root(home)
    root = tmp_path_factory.mktemp("hermetic")
    local = root / "AppData" / "Local"
    roaming = root / "AppData" / "Roaming"
    temp = local / "Temp"
    program_data = root / "ProgramData"
    for directory in (roaming, temp, program_data):
        directory.mkdir(parents=True)
    values = {
        "LOCALAPPDATA": local,
        "APPDATA": roaming,
        "TEMP": temp,
        "TMP": temp,
        "USERPROFILE": home,
        "HOME": home,
        "PROGRAMDATA": program_data,
    }
    for name, path in values.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("HOMEDRIVE", home.drive)
    monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive) :])
    # `tempfile` caches its choice on first use; pin it to the redirected dir (restored on
    # teardown by monkeypatch) so `gettempdir()` agrees with `TEMP`.
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    # `data_root()` is cwd-relative, so an unpatched default log path lands in the real checkout's
    # `data/logs/` (inside the real profile: the guard in `configure_logging` now refuses it).
    # `cli`/`api.app` bind the constant by name, so each binding is patched, not just the source.
    log_path = root / "logs" / "reclaim.log"
    for module in (logging_config, cli, api_app, api_state):
        monkeypatch.setattr(module, "DEFAULT_LOG_PATH", log_path)
    return root
