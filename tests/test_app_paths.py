from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.app_paths import compiled_exe_dir, data_root

# P0 fix (2026-08-22, live-reproduced under a real frozen install, PR #51 + AA1 generalization):
# every `data/`-relative default path in this app (logging_config.DEFAULT_LOG_PATH,
# mode.DEFAULT_MODE_LOG_PATH, first_run.DEFAULT_FIRST_RUN_STATE_PATH, executor.DEFAULT_VAULT_DIR,
# api.app._DEFAULT_VAULT_DIR, mcp.server._DEFAULT_VAULT_DIR) now anchors through this module's
# `data_root()` instead of a bare `Path("data/...")` -- see `data_root`'s own docstring for why
# a CWD-relative default breaks under an invocation with no working-directory concept (the
# `reclaim-notify:` protocol handler is the one confirmed-live case).


def test_compiled_exe_dir_returns_none_outside_a_compiled_program() -> None:
    """The real, always-true-in-a-test-run case: `__compiled__` is a Nuitka compile-time
    construct with no runtime equivalent to fake (confirmed empirically this session -- it is
    not even a real `builtins` module attribute), so a normal pytest run must see `None`."""
    assert compiled_exe_dir() is None


def test_data_root_falls_back_to_cwd_when_not_compiled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`data_root()` must fall back to `Path.cwd()` exactly as every `data/`-relative default
    did before this fix, for the source/dev/test case."""
    monkeypatch.setattr("reclaim.app_paths.compiled_exe_dir", lambda: None)
    assert data_root() == Path.cwd()


def test_data_root_uses_the_compiled_exe_directory_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix itself, exercised via the testable seam: when `compiled_exe_dir()` reports a
    directory (the real compiled-build case, monkeypatched here since a live Nuitka compile
    can't run inside this test suite), `data_root()` must anchor there instead of `Path.cwd()`
    -- this is what makes the frozen build's `data/` land next to the real exe regardless of the
    launching process's working directory."""
    fake_exe_dir = tmp_path / "Reclaim"
    monkeypatch.setattr("reclaim.app_paths.compiled_exe_dir", lambda: fake_exe_dir)
    assert data_root() == fake_exe_dir
