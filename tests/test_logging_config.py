from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import structlog

from reclaim.logging_config import configure_logging

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
