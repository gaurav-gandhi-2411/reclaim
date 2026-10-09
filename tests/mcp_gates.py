from __future__ import annotations

from typing import Any, Literal

from reclaim.approvals import ApprovalBroker

# Test doubles for `reclaim.mcp.approval_gate.ApprovalGate`. `BrokerGate` drives the REAL
# `ApprovalBroker` state machine in-process (no HTTP), so a test that uses it exercises the same
# transitions the dashboard does. `auto` simulates the user's click at request time -- test-only:
# production code has no auto-approve path at all (`build_mcp_server` defaults to the dashboard).


class BrokerGate:
    def __init__(
        self,
        broker: ApprovalBroker | None = None,
        *,
        auto: Literal["approve", "decline"] | None = None,
    ) -> None:
        self.broker = broker if broker is not None else ApprovalBroker()
        self.auto = auto
        self.requests: list[dict[str, Any]] = []

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(payload)
        approval = self.broker.create(**payload)
        channel = {"sec_fetch_site": "test", "user_agent": "BrokerGate"}
        if self.auto == "approve":
            self.broker.approve(approval.id, channel=channel)
        elif self.auto == "decline":
            self.broker.decline(approval.id, channel=channel)
        return approval.public()

    def get(self, approval_id: str) -> dict[str, Any]:
        approval = self.broker.get(approval_id)
        assert approval is not None, approval_id
        return approval.public()

    def claim(self, approval_id: str) -> bool:
        return self.broker.claim(approval_id)

    def finish(
        self,
        approval_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.broker.finish(approval_id, status=status, result=result, error=error)  # type: ignore[arg-type]


def auto_approve() -> BrokerGate:
    return BrokerGate(auto="approve")
