from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from reclaim.app_paths import data_root

# data/ is this app's existing convention for all local state (scan index, quarantine vault,
# mode log, first-run marker -- see executor.DEFAULT_MANIFEST_PATH, mode.DEFAULT_MODE_LOG_PATH,
# first_run.DEFAULT_FIRST_RUN_STATE_PATH), and the packaged installer's Start Menu/desktop
# shortcuts set WorkingDir to the install folder specifically so these relative paths land
# inside it (see packaging/reclaim.iss) rather than wherever the shortcut happens to be
# launched from. The log file follows the same convention -- one answer to "where does Reclaim
# keep its stuff", not a second one under %LOCALAPPDATA% that only this file would use.
#
# P0 fix (2026-08-22, live-reproduced under a real frozen install): a bare CWD-relative Path
# breaks for any invocation path that has no way to set WorkingDir at all --
# packaging/reclaim.iss's [Registry] `reclaim-notify:` URI protocol handler (the toast's Snooze
# button) is the one CONFIRMED-live case: a `shell\open\command` registry value has no working-
# directory concept, so the process's CWD at launch is whatever the invoking shell context
# happened to be (observed: `C:\Windows\System32`) -- `resolved_path.parent.mkdir(...)` below
# crashed with a raw, unhandled `PermissionError` before the app ever reached the actual snooze
# logic, so clicking Snooze on a real toast silently did nothing. `data_root()` (see
# `reclaim.app_paths`) anchors to the real running executable's directory when compiled (matching
# the "next to the app" semantic this comment already documents) and falls back to today's
# CWD-relative behavior otherwise -- so dev/test runs (always launched from the repo root) are
# unaffected; only the frozen build's behavior changes, and only in the direction of "no longer
# crashes when CWD isn't the install dir." Originally landed here alone (PR #51); generalized to
# every other `data/`-relative default in the app (`mode.py`, `first_run.py`, `executor.py`,
# `api/app.py`, `mcp/server.py`) once AA1 established "not reachable today" is a property of
# today's call sites, not of the code.
DEFAULT_LOG_PATH = data_root() / "data" / "logs" / "reclaim.log"

# Size-based rotation, not time-based: Reclaim is invoked as a short-lived CLI command most of
# the time (scan/apply/purge/undo/mode all exit immediately) with the dashboard as the one
# longer-lived process -- a size cap gives a hard, predictable ceiling regardless of how long
# any single process runs or how many separate processes write to the file across a day, which
# a daily TimedRotatingFileHandler would not (a dashboard left open for a week, or a burst of
# CLI invocations in a debugging session, could each still blow past a time-based cutoff).
# 5 MB x 5 backups = 30 MB ceiling total: this is a low-frequency local tool, not a busy
# service, so a modest cap already holds weeks of history -- and a disk-cleanup tool letting
# its own log grow unbounded would be a bad joke.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

# BE2 (2026-08-26 audit): the main log's own rotation is exactly right for its purpose (routine
# debug volume, capped so a disk-cleanup tool doesn't grow its own footprint unbounded) but wrong
# for a handful of rare, security-relevant events that need to survive regardless of how much
# routine logging happens around them -- live-reproduced this session: a single heavy dedup
# computation (one candidates/warm call against a real ~1M-entry index) filled and rotated the
# ENTIRE 5MB active file within ~20 seconds, evicting every earlier event including the one
# api.scan_initiated line (AN4) that would have named an unexplained scan's origin and
# confirmation-token presence -- the exact evidence AN4 exists to preserve. A 5MB/5-backup budget
# under a SUSTAINED burst at that ~115KB/s observed rate empties in about 4-5 minutes; a single
# ~50s warm-up call is enough to evict everything on its own. That is not a retention window an
# audit trail can rely on. Routed instead to its own small, isolated, append-only-in-spirit log
# (get_audit_logger below) that never competes with debug volume for space: one JSON line per
# scan-initiation is on the order of 150-250 bytes, so even this modest cap holds on the order of
# tens of thousands of real audit events -- orders of magnitude beyond any realistic personal-tool
# usage pattern, and completely immune to being crowded out by an unrelated dedup/scan burst.
_AUDIT_MAX_BYTES = 1 * 1024 * 1024
_AUDIT_BACKUP_COUNT = 10

# The exact log path handlers were last attached for, or `None` before the first call. Compared
# by value (not a bare "already ran once" bool) so a second call with a *different* path (e.g.
# each pytest test building its own `create_app()` against its own `tmp_path`) reconfigures
# instead of silently keeping the first test's handlers pointed at a file that test has since
# torn down -- while a second call with the *same* path (the common real-world case: `main()`
# configures once, then `_run_serve` -> `create_app()` calls in again with the same default) is
# a cheap no-op rather than a duplicate pair of handlers double-emitting every line.
_configured_for_path: Path | None = None

# BE2: the audit logger's own last-configured path, tracked separately from _configured_for_path
# so reconfiguring (a different log_path, e.g. each pytest test's own tmp_path) correctly tears
# down and reattaches the audit handler too, the same reasoning _configured_for_path's own
# docstring gives for the main handler.
_audit_configured_for_path: Path | None = None

AUDIT_LOGGER_NAME = "reclaim.audit"


def get_audit_logger() -> structlog.stdlib.BoundLogger:
    """The logger for rare, security-relevant events that must survive regardless of how much
    routine debug volume happens around them (BE2) -- currently just api.scan_initiated (AN4).
    Events logged here still propagate to the main log/console (this is a stdlib child logger
    under the root logger, propagation is on by default) -- this is purely an ADDITIONAL,
    isolated, long-retention sink, not a replacement for the main log stream."""
    return structlog.get_logger(AUDIT_LOGGER_NAME)  # type: ignore[no-any-return]


def configure_logging(log_path: Path | None = None, *, level: int = logging.INFO) -> None:
    """Wires structlog to render through stdlib `logging` to two sinks: a human-readable stream
    to stderr (what a user sees in an open console window) and a machine-parseable rotating
    JSON file at `log_path` (default `DEFAULT_LOG_PATH`) that survives after that console
    window closes -- see G25 in the production-readiness audit: before this, every
    `structlog.get_logger(__name__)` call in the codebase rendered to structlog's
    console-only default, so a user running the packaged `.exe` with no visible console (a
    Start Menu shortcut, or `reclaim dashboard` running in the background behind the browser
    tab) had nothing to attach to a bug report.

    Called once per real process from `cli.main()` (every subcommand) and again from
    `api.app.create_app()` (belt-and-suspenders for anything that constructs the FastAPI app
    without going through `cli.main()` first, e.g. a test) -- safe to call from both places
    and any number of times: only actually (re)attaches handlers when `log_path` differs from
    whatever it was last configured with.

    PRIVACY: this function only ever formats whatever fields callers pass to `logger.info(...)`
    /`logger.warning(...)` etc. -- it has no way to filter file content after the fact. The
    actual privacy guarantee is upstream, at every call site (paths/counts/error strings only,
    never file content or OCR'd text -- see PRIVACY.md and `reclaim.ai.screenshot_ocr`'s
    module-level comment) and is verified by `tests/test_ai_screenshot_ocr.py`'s canary-string
    test, which asserts a real OCR'd secret never reaches any `caplog`-captured log record at
    any level. That test asserts at the stdlib `logging` layer (independent of whichever
    handlers, if any, are attached), not by inspecting this function's rotating file directly --
    if the canary never reaches a log record at all, it cannot reach this (or any) handler's
    output either.
    """
    global _configured_for_path
    resolved_path = log_path if log_path is not None else DEFAULT_LOG_PATH
    if _configured_for_path == resolved_path:
        return
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            foreign_pre_chain=shared_processors,
        )
    )

    file_handler = logging.handlers.RotatingFileHandler(
        resolved_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    root_logger = logging.getLogger()
    # Reconfiguring (a different log_path than last time) must not leave the previous run's
    # handlers attached -- otherwise every subsequent line would double-emit (once to the old
    # sink, once to the new one), and the old file handle would stay open indefinitely.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(level)

    _configured_for_path = resolved_path

    # BE2: a sibling of the main log, not a hardcoded separate default -- following whatever
    # log_path override the caller passed (e.g. run_frozen_smoke_suite.ps1's isolated --log-path,
    # AZ4) means this respects the exact same test-isolation posture the main log already does,
    # with no new CLI flag needed.
    global _audit_configured_for_path
    audit_path = resolved_path.parent / "reclaim_audit.log"
    if _audit_configured_for_path != audit_path:
        audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
        for handler in list(audit_logger.handlers):
            audit_logger.removeHandler(handler)
            handler.close()
        audit_file_handler = logging.handlers.RotatingFileHandler(
            audit_path, maxBytes=_AUDIT_MAX_BYTES, backupCount=_AUDIT_BACKUP_COUNT, encoding="utf-8"
        )
        audit_file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        audit_logger.addHandler(audit_file_handler)
        # propagate stays True (the default) -- audit events still flow to the main log/console
        # too; this handler is a pure ADDITION for isolated, long-retention durability, not a
        # redirection away from the normal stream.
        audit_logger.setLevel(level)
        _audit_configured_for_path = audit_path
