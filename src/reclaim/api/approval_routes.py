from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from reclaim.api.state import AppState
from reclaim.approvals import Approval, BrokerFullError

# Two route groups, two credentials (docs/specs/assistant-mcp.md section 5):
#
# * `/api/mcp-channel/approvals...`  -- the MCP server's own client. Authenticated by the
#   per-process MCP token (`reclaim.api.security`), NOT by the browser CSRF token. It can create,
#   read, claim and finish a request. It cannot decide one.
# * `/api/mcp/approvals...`          -- Reclaim's own window. Protected by the CSRF token the page
#   was served with (middleware) plus a `Sec-Fetch-Site: same-origin` requirement here. This is
#   the ONLY place `approve`/`decline` are reachable from.
#
# The MCP package never imports `ApprovalBroker.approve`/`decline` (a source-level test enforces
# it), and the MCP token is not accepted by the second group, so an MCP tool call cannot approve.

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api")


# Everything the window shows comes from the creator, so every field is length-capped (the window
# renders them as inert text; the caps stop a client burying the real figures in noise).
_SHORT = 200
_PATH = 1024


class ApprovalCreate(BaseModel):
    client_id: str | None = Field(default=None, max_length=_SHORT)
    scan_id: str = Field(max_length=_SHORT)
    tier: str = Field(max_length=_SHORT)
    rule_id_or_category: str = Field(max_length=_SHORT)
    selection_hash: str = Field(max_length=_SHORT)
    item_count: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    sample_paths: list[Annotated[str, Field(max_length=_PATH)]] = Field(
        default_factory=list, max_length=20
    )
    protected_names: list[Annotated[str, Field(max_length=_SHORT)]] = Field(
        default_factory=list, max_length=50
    )
    reversible_until_days: int | None = None
    method: Literal["vault", "recycle_bin"] = "vault"


_MAX_RESULT_BYTES = 20_000


class ApprovalFinish(BaseModel):
    status: Literal["executed", "failed", "stale"]
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)

    @field_validator("result")
    @classmethod
    def _bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > _MAX_RESULT_BYTES:
            raise ValueError("result too large")
        return value


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.reclaim
    return state


def _get_or_404(state: AppState, approval_id: str) -> Approval:
    approval = state.approval_broker.get(approval_id)
    if approval is None:
        raise HTTPException(
            status_code=404,
            detail="unknown approval id (Reclaim may have been restarted since it was created)",
        )
    return approval


# --- MCP channel (token-authenticated) ---------------------------------------------------------


@router.post("/mcp-channel/approvals", status_code=201)
def channel_create(body: ApprovalCreate, request: Request) -> dict[str, Any]:
    state = _state(request)
    try:
        approval = state.approval_broker.create(**body.model_dump())
    except BrokerFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    logger.info(
        "mcp.approval_requested",
        approval_id=approval.id,
        client_id=approval.client_id,
        item_count=approval.item_count,
        bytes_total=approval.bytes_total,
        rule_id_or_category=approval.rule_id_or_category,
    )
    return approval.public()


@router.get("/mcp-channel/approvals")
def channel_list(request: Request) -> dict[str, Any]:
    """So a model whose `delete` call was cut off (client timeout) can find its request again."""
    return {"approvals": [a.public() for a in _state(request).approval_broker.listing()]}


@router.get("/mcp-channel/approvals/{approval_id}")
def channel_get(approval_id: str, request: Request) -> dict[str, Any]:
    return _get_or_404(_state(request), approval_id).public()


@router.post("/mcp-channel/approvals/{approval_id}/claim")
def channel_claim(approval_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    claimed = state.approval_broker.claim(approval_id)
    return {"claimed": claimed, "approval": _get_or_404(state, approval_id).public()}


@router.post("/mcp-channel/approvals/{approval_id}/finish")
def channel_finish(approval_id: str, body: ApprovalFinish, request: Request) -> dict[str, Any]:
    state = _state(request)
    if not state.approval_broker.finish(
        approval_id, status=body.status, result=body.result, error=body.error
    ):
        raise HTTPException(status_code=409, detail="approval is not in the executing state")
    logger.info("mcp.approval_finished", approval_id=approval_id, status=body.status)
    return _get_or_404(state, approval_id).public()


# --- Reclaim's own window (CSRF-protected) -----------------------------------------------------


def _require_browser_navigation_context(request: Request) -> dict[str, str]:
    """A real browser always sends `Sec-Fetch-Site` on a fetch() and the page here is same-origin,
    so a missing or non-`same-origin` value means the call did not come from this page. It is
    recorded with the decision either way. Honest limit: a local program can forge the header, so
    this raises the bar for a scripted self-approval; it is not a boundary against code running as
    the same user (see the spec's residual-risk section)."""
    site = request.headers.get("sec-fetch-site", "")
    channel = {
        "sec_fetch_site": site or "absent",
        "user_agent": request.headers.get("user-agent", ""),
    }
    if site != "same-origin":
        logger.warning("mcp.approval_decision_refused_non_browser", **channel)
        raise HTTPException(
            status_code=403,
            detail="Approvals can only be decided from Reclaim's own window.",
        )
    return channel


@router.get("/mcp/approvals")
def window_list(request: Request) -> dict[str, Any]:
    items = [a.public() for a in _state(request).approval_broker.listing()]
    return {"approvals": items, "pending": sum(1 for a in items if a["status"] == "pending")}


def _decide(approval_id: str, request: Request, *, approve: bool) -> dict[str, Any]:
    state = _state(request)
    channel = _require_browser_navigation_context(request)
    broker = state.approval_broker
    decided = (
        broker.approve(approval_id, channel=channel)
        if approve
        else broker.decline(approval_id, channel=channel)
    )
    if decided is None:
        raise HTTPException(
            status_code=409, detail="This request was already answered or has expired."
        )
    logger.info(
        "mcp.approval_approved" if approve else "mcp.approval_declined",
        approval_id=approval_id,
        **channel,
    )
    return decided.public()


@router.post("/mcp/approvals/{approval_id}/approve")
def window_approve(approval_id: str, request: Request) -> dict[str, Any]:
    return _decide(approval_id, request, approve=True)


@router.post("/mcp/approvals/{approval_id}/decline")
def window_decline(approval_id: str, request: Request) -> dict[str, Any]:
    return _decide(approval_id, request, approve=False)
