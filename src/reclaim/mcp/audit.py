from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# reclaim.mcp's audit trail: one structured log line per agent-initiated action, through the
# SAME structlog sink every other part of this codebase writes to (reclaim.logging_config) --
# not a second, separate log file. The one addition specific to this surface is
# `mcp_client_id`/`mcp_request_id`: which MCP session/request performed the action, a field no
# other caller of this codebase's structlog wrapper needs (every other log line in Reclaim is
# produced by the one human operating this process's own machine -- an MCP client is the first
# caller in this codebase that ISN'T that).


def log_mcp_action(
    event: str, *, client_id: str | None, request_id: str | int | None, **fields: object
) -> None:
    """Structured log line for one MCP-tool invocation or refusal. `event` follows this
    codebase's existing `<module>.<what_happened>` naming convention (see `api.service`'s
    `api.scan_failed`/`api.apply_failed` etc.) under the `mcp.` prefix -- e.g.
    `mcp.scan_started`, `mcp.delete_refused`, `mcp.delete_executed`. Always `logger.info`: a
    refusal (a stale scan_id, a mismatched selection_hash) is an expected, correctly-working
    outcome of this control surface, not a warning-level condition -- the interesting signal is
    that it happened and why (`fields["reason"]`), not that something went wrong."""
    logger.info(event, mcp_client_id=client_id, mcp_request_id=request_id, **fields)
