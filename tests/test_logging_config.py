from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import structlog

from reclaim.logging_config import DEFAULT_LOG_PATH, configure_logging, get_audit_logger

_EVENT_NAME = "test_logging_config.sample_event"
_AUDIT_EVENT_NAME = "test_logging_config.sample_audit_event"


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


def test_default_log_path_is_built_from_data_root() -> None:
    """Regression proof for the actual bug, at the correct level: `DEFAULT_LOG_PATH` must be
    `data_root() / "data" / "logs" / "reclaim.log"`, not a bare `Path("data/logs/reclaim.log")`
    literal independently constructed -- the fix is `data_root()` anchoring to the real exe's
    directory when compiled, not "always absolute" (that would be a DIFFERENT, wrong regression
    proof: in this always-uncompiled test environment, `data_root()` is `Path(".")`, so the
    correct value here is the plain relative `data/logs/reclaim.log`, byte-identical to the
    pre-fix literal -- see `reclaim.app_paths.data_root`'s docstring for why an eager
    `Path.cwd()` capture instead of `Path(".")` would silently break `monkeypatch.chdir(
    tmp_path)`-based test isolation elsewhere in this codebase, which is exactly what happened
    and was caught the hard way when this fix was first generalized beyond this one module)."""
    from reclaim.app_paths import data_root

    assert data_root() / "data" / "logs" / "reclaim.log" == DEFAULT_LOG_PATH
    assert not DEFAULT_LOG_PATH.is_absolute()


# `data_root()`/`compiled_exe_dir()` themselves now live in reclaim.app_paths (generalized to
# every `data/`-relative default in the app, not just this module's) -- see
# tests/test_app_paths.py for their own dedicated coverage.


# --- BE2 (2026-08-26 audit): a small, isolated, long-retention sink for rare security-relevant
# events (currently api.scan_initiated, AN4) that must survive regardless of routine debug
# volume -- live-reproduced this session: a single heavy dedup computation filled and rotated
# the ENTIRE 5MB main log within ~20 seconds, evicting the one event that would have named an
# unexplained scan's origin. ------------------------------------------------------------------


def test_get_audit_logger_writes_to_a_separate_file_from_the_main_log(tmp_path: Path) -> None:
    """The audit logger must not just be a differently-named structlog logger that still funnels
    into the same rotating file handler -- it needs its own handler pointed at its own file, or
    it inherits the exact eviction risk this feature exists to avoid."""
    log_path = tmp_path / "reclaim.log"
    configure_logging(log_path)

    audit_logger = get_audit_logger()
    audit_logger.info(_AUDIT_EVENT_NAME, root="C:/example")

    audit_path = tmp_path / "reclaim_audit.log"
    assert audit_path.exists()
    assert audit_path != log_path
    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(json.loads(line).get("event") == _AUDIT_EVENT_NAME for line in lines)


def test_audit_events_also_reach_the_main_log(tmp_path: Path) -> None:
    """The audit file is an ADDITION, not a redirection -- an audit event must still show up in
    the main log stream too (propagation), so a human reading the regular log in order still
    sees it in context."""
    log_path = tmp_path / "reclaim.log"
    configure_logging(log_path)

    get_audit_logger().info(_AUDIT_EVENT_NAME, root="C:/example")

    main_lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(json.loads(line).get("event") == _AUDIT_EVENT_NAME for line in main_lines)


def test_audit_log_survives_main_log_rotation_from_unrelated_volume(tmp_path: Path) -> None:
    """The actual regression this feature fixes: flood the main log past its own rotation
    threshold with unrelated noise (simulating the real dedup-computation burst that evicted
    AN4's evidence this session), then confirm an audit event logged BEFORE the flood is still
    readable afterward -- proving the audit file's own, isolated rotation budget was never
    touched by the main log's rotation at all."""
    log_path = tmp_path / "reclaim.log"
    configure_logging(log_path)

    get_audit_logger().info(_AUDIT_EVENT_NAME, root="C:/example", marker="before_flood")

    noise_logger = structlog.get_logger("test_logging_config.noise")
    # Comfortably past the main log's 5MB cap -- forces at least one real rotation of reclaim.log.
    for _ in range(20_000):
        noise_logger.info("test_logging_config.noise_event", padding="x" * 300)

    audit_path = tmp_path / "reclaim_audit.log"
    audit_lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    assert any(json.loads(line).get("marker") == "before_flood" for line in audit_lines), (
        "the pre-flood audit event must still be present -- the whole point of a separate handler"
    )
    # And confirm the main log genuinely did rotate, so this test would have caught the bug it's
    # written against (a flood that never actually rotated anything proves nothing).
    assert (tmp_path / "reclaim.log.1").exists()


def test_configure_logging_reconfigures_audit_handler_for_a_different_path(tmp_path: Path) -> None:
    """Same reasoning as the main log's own reconfigure-for-a-different-path test: a second
    `configure_logging` call with a different path (each test building its own tmp_path) must
    redirect the audit sink too, not leave it writing to a torn-down prior test's file."""
    first_path = tmp_path / "first" / "reclaim.log"
    second_path = tmp_path / "second" / "reclaim.log"

    configure_logging(first_path)
    get_audit_logger().info(_AUDIT_EVENT_NAME, marker="first")

    configure_logging(second_path)
    get_audit_logger().info(_AUDIT_EVENT_NAME, marker="second")

    second_audit_path = tmp_path / "second" / "reclaim_audit.log"
    assert second_audit_path.exists()
    second_lines = [
        line for line in second_audit_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert any(json.loads(line).get("marker") == "second" for line in second_lines)
    assert not any(json.loads(line).get("marker") == "first" for line in second_lines)
