from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog

from reclaim.logging_config import DEFAULT_LOG_PATH, _data_root, configure_logging

_EVENT_NAME = "test_logging_config.sample_event"


def _root_handlers() -> list[logging.Handler]:
    return list(logging.getLogger().handlers)


def test_configure_logging_creates_rotating_file_with_structured_json_lines(
    tmp_path: Path,
) -> None:
    """The persistent log file (G25) must exist after configuration and contain one JSON object
    per emitted record, with the structured fields a caller passed intact — this is what makes
    a file useful for a bug report instead of an opaque blob."""
    log_path = tmp_path / "reclaim.log"
    configure_logging(log_path)

    logger = structlog.get_logger("test_logging_config")
    logger.info(_EVENT_NAME, path="C:/example/file.txt", count=3)

    assert log_path.exists()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "expected at least one log line to have been written"
    record = json.loads(lines[-1])
    assert record["event"] == _EVENT_NAME
    assert record["path"] == "C:/example/file.txt"
    assert record["count"] == 3
    assert record["level"] == "info"
    assert "timestamp" in record


def test_configure_logging_attaches_both_console_and_file_handlers(tmp_path: Path) -> None:
    """Console output must keep working alongside the new file sink (the task's explicit
    requirement: "alongside (not instead of) console output") -- both handler types must be
    present on the root logger after configuration."""
    configure_logging(tmp_path / "reclaim.log")

    handlers = _root_handlers()
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
    assert any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in handlers
    )


def test_configure_logging_caps_file_size_and_backup_count(tmp_path: Path) -> None:
    """A reasonable max size + backup count so the log can never grow unbounded (task
    requirement) -- asserted against the actual attached handler's configuration, not just the
    module's private constants, so a future refactor that silently drops the cap would fail
    this test."""
    configure_logging(tmp_path / "reclaim.log")

    file_handlers = [
        h for h in _root_handlers() if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert 0 < handler.maxBytes <= 10 * 1024 * 1024  # generous upper bound, still finite
    assert 0 < handler.backupCount <= 10


def test_configure_logging_is_a_no_op_for_the_same_path(tmp_path: Path) -> None:
    """Calling `configure_logging` twice with the same path (e.g. once from `cli.main()`, again
    from `api.app.create_app()` in the same process) must not double-attach handlers -- that
    would double-emit every log line."""
    log_path = tmp_path / "reclaim.log"
    configure_logging(log_path)
    handlers_after_first_call = _root_handlers()

    configure_logging(log_path)
    handlers_after_second_call = _root_handlers()

    assert handlers_after_first_call == handlers_after_second_call


def test_configure_logging_reconfigures_for_a_different_path(tmp_path: Path) -> None:
    """A second call with a *different* path (the common test scenario: each `create_app()`
    call in a test suite builds against its own `tmp_path`) must redirect subsequent log output
    to the new file rather than continuing to write to a file a prior test has already torn
    down."""
    first_path = tmp_path / "first" / "reclaim.log"
    second_path = tmp_path / "second" / "reclaim.log"

    configure_logging(first_path)
    logger = structlog.get_logger("test_logging_config")
    logger.info(_EVENT_NAME, marker="first")

    configure_logging(second_path)
    logger.info(_EVENT_NAME, marker="second")

    assert second_path.exists()
    second_lines = [line for line in second_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(json.loads(line).get("marker") == "second" for line in second_lines)
    assert not any(json.loads(line).get("marker") == "first" for line in second_lines)

    # Only one rotating file handler ever attached at a time -- the stale one pointed at
    # `first_path` must have been detached, not left running alongside the new one.
    file_handlers = [
        h for h in _root_handlers() if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == second_path.resolve()


# --- P0 fix (2026-08-22, live-reproduced under a real frozen install): DEFAULT_LOG_PATH must
# not depend on CWD at the time it happens to be *used* --------------------------------------
#
# Bug: packaging/reclaim.iss registers a `reclaim-notify:` URI protocol handler for the disk-
# space toast's Snooze button. A `shell\open\command` registry value has no working-directory
# concept, so the process's CWD at launch is whatever the invoking shell context happened to be
# (observed live: `C:\Windows\System32`) -- a bare `Path("data/logs/reclaim.log")` crashed with
# an unhandled `PermissionError` from `resolved_path.parent.mkdir(...)` before the app ever
# reached the actual snooze logic, so clicking Snooze on a real toast silently did nothing.


def test_default_log_path_is_absolute_not_cwd_relative_at_use_time() -> None:
    """Regression proof for the actual bug: the old `Path("data/logs/reclaim.log")` stayed
    relative until something resolved it against whatever CWD was active *at that moment* --
    exactly the property that broke under the protocol handler's arbitrary launch CWD. An
    absolute `DEFAULT_LOG_PATH` cannot be re-broken by a later CWD change the way a relative one
    could."""
    assert DEFAULT_LOG_PATH.is_absolute()


def test_data_root_falls_back_to_cwd_when_not_compiled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real, always-true-in-a-test-run case: `_compiled_exe_dir()` returns `None` outside a
    Nuitka-compiled program (confirmed empirically this session -- `__compiled__` is a Nuitka
    compile-time construct, not something a test can fake at the name-lookup level), so
    `_data_root()` must fall back to `Path.cwd()` exactly as it did before this fix."""
    monkeypatch.setattr("reclaim.logging_config._compiled_exe_dir", lambda: None)
    assert _data_root() == Path.cwd()


def test_data_root_uses_the_compiled_exe_directory_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix itself, exercised via the testable seam: when `_compiled_exe_dir()` reports a
    directory (the real compiled-build case, monkeypatched here since a live Nuitka compile
    can't run inside this test suite), `_data_root()` must anchor there instead of `Path.cwd()`
    -- this is what makes the frozen build's `data/logs/` land next to the real exe regardless
    of the launching process's working directory."""
    fake_exe_dir = tmp_path / "Reclaim"
    monkeypatch.setattr("reclaim.logging_config._compiled_exe_dir", lambda: fake_exe_dir)
    assert _data_root() == fake_exe_dir
