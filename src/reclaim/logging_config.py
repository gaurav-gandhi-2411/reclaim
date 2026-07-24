from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

# data/ is this app's existing convention for all local state (scan index, quarantine vault,
# mode log, first-run marker -- see executor.DEFAULT_MANIFEST_PATH, mode.DEFAULT_MODE_LOG_PATH,
# first_run.DEFAULT_FIRST_RUN_STATE_PATH), and the packaged installer's Start Menu/desktop
# shortcuts set WorkingDir to the install folder specifically so these relative paths land
# inside it (see packaging/reclaim.iss) rather than wherever the shortcut happens to be
# launched from. The log file follows the same convention -- one answer to "where does Reclaim
# keep its stuff", not a second one under %LOCALAPPDATA% that only this file would use.
DEFAULT_LOG_PATH = Path("data/logs/reclaim.log")

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

# The exact log path handlers were last attached for, or `None` before the first call. Compared
# by value (not a bare "already ran once" bool) so a second call with a *different* path (e.g.
# each pytest test building its own `create_app()` against its own `tmp_path`) reconfigures
# instead of silently keeping the first test's handlers pointed at a file that test has since
# torn down -- while a second call with the *same* path (the common real-world case: `main()`
# configures once, then `_run_serve` -> `create_app()` calls in again with the same default) is
# a cheap no-op rather than a duplicate pair of handlers double-emitting every line.
_configured_for_path: Path | None = None


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
