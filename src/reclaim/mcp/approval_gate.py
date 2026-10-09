from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from reclaim.approvals import channel_file_path, read_channel_file
from reclaim.mcp.selection import ApprovalUnavailableError

# The MCP server's side of the user-confirmation flow (docs/specs/assistant-mcp.md section 5).
# It can ask for an approval, read its state, claim it for execution and report the outcome. It
# has NO way to decide one: this module deliberately never touches `ApprovalBroker.approve` /
# `.decline` or the `/api/mcp/approvals/*` decide routes, and the credential it holds (the
# per-process MCP channel token) is rejected by those routes. `evals/test_mcp_safety_gate.py`
# and `tests/test_mcp_approval.py` enforce both properties.

_HTTP_TIMEOUT_SECONDS = 5.0


class ApprovalGate(Protocol):
    """What `reclaim.mcp.server.delete` needs from the confirmation window. Production uses
    `DashboardApprovalGate`; tests pass an in-process fake (see tests/test_mcp.py)."""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a pending approval; returns its public record (`id`, `status`, ...)."""

    def get(self, approval_id: str) -> dict[str, Any]:
        """Current record. Raises `ApprovalUnavailableError` if the window cannot be reached."""

    def claim(self, approval_id: str) -> bool:
        """approved -> executing, exactly once. False if it is in any other state."""

    def finish(
        self,
        approval_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Record how the claimed execution ended (`executed`, `failed` or `stale`)."""


class DashboardApprovalGate:
    """Talks to a running Reclaim window over loopback HTTP. The window publishes its port and
    per-process channel token in `dashboard_channel.json` next to the index; if the file is
    missing or the window does not answer, every call raises `ApprovalUnavailableError` and
    nothing is deleted (fail closed)."""

    def __init__(
        self,
        *,
        db_path: Path,
        client_factory: Callable[[dict[str, Any]], httpx.Client] | None = None,
    ) -> None:
        self._channel_path = channel_file_path(db_path)
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(channel: dict[str, Any]) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://127.0.0.1:{channel['port']}", timeout=_HTTP_TIMEOUT_SECONDS
        )

    def _call(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        channel = read_channel_file(self._channel_path)
        if channel is None:
            raise ApprovalUnavailableError(
                "Reclaim's window is not open, so there is nobody to approve this delete. Ask the "
                "user to open Reclaim, then call delete again. Nothing was deleted."
            )
        try:
            with self._client_factory(channel) as client:
                response = client.request(
                    method,
                    path,
                    json=json,
                    headers={"x-reclaim-mcp-token": str(channel["token"])},
                )
        except httpx.HTTPError as exc:
            raise ApprovalUnavailableError(
                "Reclaim's window did not answer (it may have been closed). Ask the user to open "
                f"Reclaim, then call delete again. Nothing was deleted. ({type(exc).__name__})"
            ) from exc
        if response.status_code == 429:
            raise ApprovalUnavailableError(str(response.json().get("detail", "too many requests")))
        if response.status_code >= 400:
            raise ApprovalUnavailableError(
                f"Reclaim's window refused the request (HTTP {response.status_code}). "
                "Nothing was deleted."
            )
        return response

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = self._call(
            "POST", "/api/mcp-channel/approvals", json=payload
        ).json()
        return record

    def get(self, approval_id: str) -> dict[str, Any]:
        record: dict[str, Any] = self._call(
            "GET", f"/api/mcp-channel/approvals/{approval_id}"
        ).json()
        return record

    def claim(self, approval_id: str) -> bool:
        body = self._call("POST", f"/api/mcp-channel/approvals/{approval_id}/claim").json()
        return bool(body["claimed"])

    def finish(
        self,
        approval_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._call(
            "POST",
            f"/api/mcp-channel/approvals/{approval_id}/finish",
            json={"status": status, "result": result, "error": error},
        )
