from __future__ import annotations

import ast
import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_gates import BrokerGate
from test_mcp import (
    _build_power_mode_state,
    _build_tree,
    _config,
)

from reclaim.api import service
from reclaim.api.app import create_app
from reclaim.api.security import CSRF_HEADER_NAME, MCP_CHANNEL_AUTH_HEADER
from reclaim.approvals import (
    APPROVED_CLAIM_TTL_SECONDS,
    EXECUTING_LEASE_SECONDS,
    MAX_PENDING,
    PENDING_TTL_SECONDS,
    ApprovalBroker,
    BrokerFullError,
    channel_file_path,
)
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, ExclusionsConfig
from reclaim.mcp.approval_gate import DashboardApprovalGate
from reclaim.mcp.selection import (
    ApprovalDeclinedError,
    ApprovalExpiredError,
    ApprovalUnavailableError,
    SelectionMismatchError,
    StaleScanError,
    compute_selection_hash,
)
from reclaim.mcp.server import build_mcp_server

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_RULE = "dev_artifact_node_modules"
_BROWSER = {"sec-fetch-site": "same-origin", "user-agent": "pytest-browser"}


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _Ctx:
    client_id = None
    request_id = "approval-test"


def _selection(state: Any, scan_id: str) -> tuple[str, int]:
    selected = service.select_candidates_for_selector(state, tier="A", rule_id_or_category=_RULE)
    digest = compute_selection_hash(
        scan_id=scan_id,
        tier="A",
        rule_id_or_category=_RULE,
        paths=[c.path.as_posix() for c in selected],
    )
    return digest, len(selected)


def _prepare(tmp_path: Path, *, config: Config | None = None) -> tuple[Any, dict[str, Path], str]:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    state = _build_power_mode_state(tmp_path, config=config or _config())
    service.run_scan(state, [root], time.time())
    return state, paths, service.scan_id_for_state(state)


def _delete_args(scan_id: str, digest: str, **extra: Any) -> dict[str, Any]:
    return {
        "scan_id": scan_id,
        "rule_id_or_category": _RULE,
        "tier": "A",
        "selection_hash": digest,
        **extra,
    }


async def _click_when_pending(gate: BrokerGate, *, approve: bool, check: Any = None) -> None:
    """The user: wait until a request shows up in the window, optionally look at the disk (nothing
    may be deleted yet), then click."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = [a for a in gate.broker.listing() if a.status == "pending"]
        if pending:
            if check is not None:
                check()
            channel = {"sec_fetch_site": "test", "user_agent": "pytest-user"}
            if approve:
                gate.broker.approve(pending[0].id, channel=channel)
            else:
                gate.broker.decline(pending[0].id, channel=channel)
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no approval request ever appeared")


# --- broker unit tests -------------------------------------------------------------------------


def _create(broker: ApprovalBroker, **over: Any) -> Any:
    fields: dict[str, Any] = {
        "client_id": "c",
        "scan_id": "scan-1",
        "tier": "A",
        "rule_id_or_category": _RULE,
        "selection_hash": "h",
        "item_count": 1,
        "bytes_total": 10,
        "sample_paths": ["C:/x"],
        "protected_names": [],
        "reversible_until_days": 30,
    }
    fields.update(over)
    return broker.create(**fields)


def test_broker_approve_is_single_use_and_claim_is_exactly_once() -> None:
    broker = ApprovalBroker()
    a = _create(broker)
    assert broker.claim(a.id) is False  # not approved yet
    assert broker.approve(a.id, channel={}) is not None
    assert broker.approve(a.id, channel={}) is None  # a click is never replayed
    assert broker.decline(a.id, channel={}) is None  # and cannot be flipped afterwards
    assert broker.claim(a.id) is True
    assert broker.claim(a.id) is False
    assert broker.finish(a.id, status="executed", result={"x": 1}) is True
    assert broker.finish(a.id, status="failed") is False  # already finished


def test_broker_pending_expires_and_an_unclaimed_approval_expires() -> None:
    clock = _FakeClock()
    broker = ApprovalBroker(clock=clock)
    pending = _create(broker)
    clock.now += PENDING_TTL_SECONDS + 1
    assert broker.get(pending.id).status == "expired"  # type: ignore[union-attr]
    assert broker.approve(pending.id, channel={}) is None  # too late to click

    approved = _create(broker)
    broker.approve(approved.id, channel={})
    clock.now += APPROVED_CLAIM_TTL_SECONDS + 1
    assert broker.get(approved.id).status == "expired"  # type: ignore[union-attr]
    assert broker.claim(approved.id) is False


def test_broker_caps_pending_requests() -> None:
    broker = ApprovalBroker()
    for n in range(MAX_PENDING):
        _create(broker, selection_hash=f"h{n}")
    with pytest.raises(BrokerFullError):
        _create(broker)


# --- the tool flow, in process -----------------------------------------------------------------


async def test_nothing_is_deleted_until_the_user_approves_then_it_is_executed(
    tmp_path: Path,
) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, count = _selection(state, scan_id)
    assert count == 1
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    def still_there() -> None:
        assert paths["node_modules_dir"].exists(), "deleted before the user approved"

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        clicker = asyncio.create_task(_click_when_pending(gate, approve=True, check=still_there))
        result = await session.call_tool("delete", _delete_args(scan_id, digest))
        await clicker

    assert result.isError is False, result.content
    body = result.structuredContent
    assert body["status"] == "executed" and body["files_succeeded"] == 1
    assert not paths["node_modules_dir"].exists()
    assert any((tmp_path / "vault").rglob("index.js")), "approved deletes are reversible"
    record = gate.broker.get(body["approval_id"])
    assert record is not None and record.status == "executed"
    # what the window showed the user:
    payload = gate.requests[0]
    assert payload["item_count"] == 1 and payload["bytes_total"] > 0
    assert payload["method"] == "vault" and payload["reversible_until_days"] >= 1
    assert payload["sample_paths"] == [paths["node_modules_dir"].as_posix()]


async def test_declined_request_raises_and_deletes_nothing(tmp_path: Path) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        clicker = asyncio.create_task(_click_when_pending(gate, approve=False))
        result = await session.call_tool("delete", _delete_args(scan_id, digest))
        await clicker

    assert result.isError is True
    assert "declined" in str(result.content).lower()
    assert paths["node_modules_dir"].exists()
    assert not list((tmp_path / "vault").rglob("index.js"))


async def test_unanswered_request_returns_awaiting_user_then_delete_status_completes(
    tmp_path: Path,
) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        assert first.isError is False, first.content
        assert first.structuredContent["status"] == "awaiting_user"
        approval_id = first.structuredContent["approval_id"]
        assert paths["node_modules_dir"].exists()  # timeout is not consent

        still = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )
        assert still.structuredContent["status"] == "awaiting_user"

        gate.broker.approve(approval_id, channel={"sec_fetch_site": "test"})
        done = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 5}
        )
        again = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )

    assert done.structuredContent["status"] == "executed"
    assert not paths["node_modules_dir"].exists()
    # asking again returns the recorded result; it does not delete or fail a second time
    assert again.structuredContent["status"] == "executed"
    assert again.structuredContent["batch_id"] == done.structuredContent["batch_id"]


async def test_an_expired_request_is_refused_and_deletes_nothing(tmp_path: Path) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    clock = _FakeClock()
    gate = BrokerGate(ApprovalBroker(clock=clock))
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        approval_id = first.structuredContent["approval_id"]
        clock.now += PENDING_TTL_SECONDS + 1
        late = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )

    assert late.isError is True and "expired" in str(late.content).lower()
    assert paths["node_modules_dir"].exists()


async def test_a_selection_that_changed_after_approval_is_refused(tmp_path: Path) -> None:
    """The user approved selection X; before it ran, a new scan completed. Executing would act on
    something the user never saw, so it is refused and recorded as stale."""
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        approval_id = first.structuredContent["approval_id"]
        gate.broker.approve(approval_id, channel={"sec_fetch_site": "test"})
        service.run_scan(state, [tmp_path / "tree"], time.time())  # a newer scan completes
        late = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 5}
        )

    assert late.isError is True
    assert paths["node_modules_dir"].exists()
    assert not list((tmp_path / "vault").rglob("index.js"))
    assert gate.broker.get(approval_id).status == "stale"  # type: ignore[union-attr]


async def test_a_candidate_set_drift_after_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        approval_id = first.structuredContent["approval_id"]
        gate.broker.approve(approval_id, channel={"sec_fetch_site": "test"})
        monkeypatch.setattr(service, "select_candidates_for_selector", lambda *a, **k: [])
        late = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 5}
        )

    assert late.isError is True and "after the user approved" in str(late.content).lower()
    assert paths["node_modules_dir"].exists()
    assert gate.broker.get(approval_id).status == "stale"  # type: ignore[union-attr]


async def test_excluded_projects_are_never_offered_and_never_deleted_even_if_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30)
        ),
        exclusions=ExclusionsConfig(project_names=["Project"]),
    )
    state, paths, scan_id = _prepare(tmp_path, config=config)
    digest, count = _selection(state, scan_id)
    assert count == 0, "an excluded project must not be a candidate at all"

    gate = BrokerGate(auto="approve")
    server = build_mcp_server(state, approval_gate=gate)
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("delete", _delete_args(scan_id, digest))
    assert result.isError is False and result.structuredContent["files_succeeded"] == 0
    assert gate.requests[0]["item_count"] == 0
    assert gate.requests[0]["protected_names"] == ["Project"]  # what the window states
    assert paths["node_modules_dir"].exists()

    # Defence in depth: even if the candidate list were wrong and the user approved, the executor
    # refuses an excluded path (ADR-0039) and the approval ends "failed", not "executed".
    permissive = _build_power_mode_state(tmp_path / "second", config=_config())
    service.run_scan(permissive, [tmp_path / "tree"], time.time())
    leaked = service.select_candidates_for_selector(permissive, tier="A", rule_id_or_category=_RULE)
    assert len(leaked) == 1
    monkeypatch.setattr(service, "select_candidates_for_selector", lambda *a, **k: leaked)
    gate2 = BrokerGate(auto="approve")
    server2 = build_mcp_server(state, approval_gate=gate2)
    leaked_digest = compute_selection_hash(
        scan_id=scan_id,
        tier="A",
        rule_id_or_category=_RULE,
        paths=[c.path.as_posix() for c in leaked],
    )
    async with create_connected_server_and_client_session(server2._mcp_server) as session:
        refused = await session.call_tool("delete", _delete_args(scan_id, leaked_digest))
    assert refused.isError is True
    assert paths["node_modules_dir"].exists()
    assert not list((tmp_path / "vault").rglob("index.js"))


async def test_no_open_window_means_no_delete_and_a_clear_error(tmp_path: Path) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    assert not channel_file_path(state.db_path).exists()
    server = build_mcp_server(state)  # the production default gate

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("delete", _delete_args(scan_id, digest))

    assert result.isError is True and "not open" in str(result.content)
    assert paths["node_modules_dir"].exists()


async def test_a_bad_hash_is_refused_before_the_user_is_ever_asked(tmp_path: Path) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("delete", _delete_args(scan_id, "0" * 64))

    assert result.isError is True
    assert gate.requests == []  # nobody was bothered
    assert paths["node_modules_dir"].exists()


# --- the MCP surface cannot approve ------------------------------------------------------------


async def test_the_mcp_tool_list_has_no_way_to_approve_or_decline(tmp_path: Path) -> None:
    state, _, _ = _prepare(tmp_path)
    server = build_mcp_server(state, approval_gate=BrokerGate())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
    assert "delete" in names and "delete_status" in names
    assert not [n for n in names if re.search(r"approv|declin|confirm|consent", n)], names
    delete_tool = next(t for t in tools if t.name == "delete")
    assert "path" not in delete_tool.inputSchema["properties"]


def test_the_mcp_package_never_references_the_deciding_api() -> None:
    """Source-level (AST, so comments and docstrings do not count): nothing under reclaim.mcp can
    call a deciding method or name a decide route / the browser CSRF header. `delete_status` and
    the gate only read, claim and finish."""
    root = Path(__file__).parent.parent / "src" / "reclaim" / "mcp"
    forbidden_attrs = {"approve", "decline", "_decide"}
    forbidden_strings = ("/api/mcp/approvals", "x-reclaim-csrf", "/approve", "/decline")
    offenders: list[str] = []
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                offenders.append(f"{source.name}:{node.lineno} .{node.attr}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and any(f in node.value.lower() for f in forbidden_strings)
            ):
                offenders.append(f"{source.name}:{node.lineno} {node.value[:40]!r}")
            if isinstance(node, ast.ImportFrom) and node.module == "reclaim.approvals":
                names = {a.name for a in node.names}
                if names - {"channel_file_path", "read_channel_file"}:
                    offenders.append(f"{source.name}:{node.lineno} imports {sorted(names)}")
    assert not offenders, offenders


# --- the dashboard routes + a real HTTP round trip ---------------------------------------------


class _NoClose:
    """Lets the gate's `with client:` reuse the one TestClient without closing the app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def __enter__(self) -> _NoClose:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def request(self, *a: Any, **k: Any) -> httpx.Response:
        return self._client.request(*a, **k)


@pytest.fixture
def dashboard(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(),
        config_path=tmp_path / "config.toml",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host="127.0.0.1",
        port=8420,
    )
    with TestClient(app, base_url="http://127.0.0.1:8420") as client:
        yield app.state.reclaim, client


def _browser(app_state: Any) -> dict[str, str]:
    return {CSRF_HEADER_NAME: app_state.csrf_token, **_BROWSER}


def _mcp(app_state: Any) -> dict[str, str]:
    return {MCP_CHANNEL_AUTH_HEADER: app_state.mcp_channel_token}


_PAYLOAD = {
    "client_id": "c",
    "scan_id": "scan-1",
    "tier": "A",
    "rule_id_or_category": _RULE,
    "selection_hash": "h",
    "item_count": 2,
    "bytes_total": 2048,
    "sample_paths": ["C:/a", "C:/b"],
    "protected_names": ["fr-en-transformer"],
    "reversible_until_days": 30,
    "method": "vault",
}


def test_dashboard_publishes_and_removes_its_channel_file(tmp_path: Path, dashboard) -> None:  # type: ignore[no-untyped-def]
    state, _client = dashboard
    path = channel_file_path(state.db_path)
    assert path.exists() and state.mcp_channel_token in path.read_text(encoding="utf-8")


def test_the_two_credentials_are_not_interchangeable(dashboard) -> None:  # type: ignore[no-untyped-def]
    state, client = dashboard
    created = client.post("/api/mcp-channel/approvals", json=_PAYLOAD, headers=_mcp(state))
    assert created.status_code == 201, created.text
    approval_id = created.json()["id"]

    # The MCP token cannot decide (the decide routes need the browser CSRF token) ...
    for action in ("approve", "decline"):
        denied = client.post(
            f"/api/mcp/approvals/{approval_id}/{action}", headers={**_mcp(state), **_BROWSER}
        )
        assert denied.status_code == 403, denied.text
    # ... the browser CSRF token cannot drive the MCP channel ...
    assert (
        client.get(f"/api/mcp-channel/approvals/{approval_id}", headers=_browser(state)).status_code
        == 403
    )
    # ... and with no credential at all neither works.
    assert client.post("/api/mcp-channel/approvals", json=_PAYLOAD).status_code == 403
    assert state.approval_broker.get(approval_id).status == "pending"  # type: ignore[union-attr]


def test_deciding_needs_a_same_origin_browser_request_and_cannot_be_replayed(dashboard) -> None:  # type: ignore[no-untyped-def]
    state, client = dashboard
    approval_id = client.post(
        "/api/mcp-channel/approvals", json=_PAYLOAD, headers=_mcp(state)
    ).json()["id"]

    headless = {CSRF_HEADER_NAME: state.csrf_token}  # right token, but not from the page
    assert (
        client.post(f"/api/mcp/approvals/{approval_id}/approve", headers=headless).status_code
        == 403
    )
    assert state.approval_broker.get(approval_id).status == "pending"  # type: ignore[union-attr]

    listing = client.get("/api/mcp/approvals", headers=_browser(state)).json()
    card = listing["approvals"][0]
    assert listing["pending"] == 1
    assert (card["item_count"], card["bytes_total"], card["method"]) == (2, 2048, "vault")
    assert card["protected_names"] == ["fr-en-transformer"]

    ok = client.post(f"/api/mcp/approvals/{approval_id}/approve", headers=_browser(state))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert ok.json()["decision_channel"]["sec_fetch_site"] == "same-origin"
    again = client.post(f"/api/mcp/approvals/{approval_id}/approve", headers=_browser(state))
    flip = client.post(f"/api/mcp/approvals/{approval_id}/decline", headers=_browser(state))
    assert again.status_code == 409 and flip.status_code == 409


async def test_full_round_trip_over_http_with_a_click_in_the_window(  # type: ignore[no-untyped-def]
    tmp_path: Path, dashboard
) -> None:
    dash_state, client = dashboard
    state, paths, scan_id = _prepare(tmp_path / "mcp_side")
    # Both processes share the data directory: the MCP side finds the window via the channel file.
    channel_src = channel_file_path(dash_state.db_path)
    channel_dst = channel_file_path(state.db_path)
    channel_dst.write_text(channel_src.read_text(encoding="utf-8"), encoding="utf-8")
    digest, _ = _selection(state, scan_id)
    gate = DashboardApprovalGate(
        db_path=state.db_path,
        client_factory=lambda _channel: _NoClose(client),  # type: ignore[arg-type,return-value]
    )
    server = build_mcp_server(state, approval_gate=gate)

    async def user_clicks() -> None:
        for _ in range(200):
            pending = (
                await asyncio.to_thread(
                    client.get, "/api/mcp/approvals", headers=_browser(dash_state)
                )
            ).json()
            if pending["pending"]:
                assert paths["node_modules_dir"].exists()
                await asyncio.to_thread(
                    client.post,
                    f"/api/mcp/approvals/{pending['approvals'][0]['id']}/approve",
                    headers=_browser(dash_state),
                )
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the request never reached the window")

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        clicker = asyncio.create_task(user_clicks())
        result = await session.call_tool("delete", _delete_args(scan_id, digest))
        await clicker

    assert result.isError is False, result.content
    assert result.structuredContent["status"] == "executed"
    assert not paths["node_modules_dir"].exists()
    assert any((tmp_path / "mcp_side" / "vault").rglob("index.js"))
    final = client.get(
        f"/api/mcp-channel/approvals/{result.structuredContent['approval_id']}",
        headers=_mcp(dash_state),
    ).json()
    assert final["status"] == "executed"


# --- verifier findings (PR #164) -----------------------------------------------------------------


def test_an_identical_open_request_is_returned_not_duplicated() -> None:
    broker = ApprovalBroker()
    first = _create(broker)
    again = _create(broker)
    assert again.id == first.id and len(broker.listing()) == 1
    broker.approve(first.id, channel={})
    assert _create(broker).id == first.id  # a retry picks up the user's click
    other = _create(broker, selection_hash="different")
    assert other.id != first.id


def test_an_executing_approval_that_nobody_finishes_fails_after_its_lease() -> None:
    clock = _FakeClock()
    broker = ApprovalBroker(clock=clock)
    a = _create(broker)
    broker.approve(a.id, channel={})
    assert broker.claim(a.id) is True
    clock.now += EXECUTING_LEASE_SECONDS - 1
    assert broker.get(a.id).status == "executing"  # type: ignore[union-attr]
    clock.now += 2
    stuck = broker.get(a.id)
    assert (
        stuck is not None and stuck.status == "failed" and "never reported back" in str(stuck.error)
    )
    assert broker.claim(a.id) is False


async def test_a_stuck_executing_request_is_reported_not_polled_forever(tmp_path: Path) -> None:
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    clock = _FakeClock()
    gate = BrokerGate(ApprovalBroker(clock=clock))
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        approval_id = first.structuredContent["approval_id"]
        gate.broker.approve(approval_id, channel={})
        assert gate.broker.claim(approval_id)  # a call that claimed then died
        waiting = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )
        assert waiting.structuredContent["status"] == "awaiting_user"
        assert "is running" in waiting.structuredContent["message"]
        clock.now += EXECUTING_LEASE_SECONDS + 1
        dead = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )

    assert dead.isError is True and "did not complete" in str(dead.content)
    assert paths["node_modules_dir"].exists()


async def test_retrying_delete_for_the_same_selection_reuses_the_card_and_delete_requests_finds_it(
    tmp_path: Path,
) -> None:
    state, _paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        one = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        two = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        listed = await session.call_tool("delete_requests", {})

    assert one.structuredContent["approval_id"] == two.structuredContent["approval_id"]
    rows = listed.structuredContent["requests"]
    assert [r["approval_id"] for r in rows] == [one.structuredContent["approval_id"]]
    assert rows[0]["status"] == "pending" and rows[0]["item_count"] == 1
    assert len([a for a in gate.broker.listing() if a.status == "pending"]) == 1


async def test_figures_shown_to_the_user_must_match_the_selection_at_execution(
    tmp_path: Path,
) -> None:
    """A creator talking to the channel directly could show '1 item, 4 KB' for a bigger selection
    whose (valid) hash it supplies. The numbers are re-checked when it runs."""
    state, paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    approval = gate.request(
        {
            "client_id": "Claude",
            "scan_id": scan_id,
            "tier": "A",
            "rule_id_or_category": _RULE,
            "selection_hash": digest,
            "item_count": 1,
            "bytes_total": 4096,  # the lie: the real selection is larger
            "sample_paths": ["C:/harmless"],
            "protected_names": [],
            "reversible_until_days": 30,
            "method": "vault",
        }
    )
    gate.broker.approve(approval["id"], channel={"sec_fetch_site": "test"})
    server = build_mcp_server(state, approval_gate=gate)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool(
            "delete_status", {"approval_id": approval["id"], "wait_seconds": 5}
        )

    assert result.isError is True and "shown" in str(result.content)
    assert paths["node_modules_dir"].exists()
    assert gate.broker.get(approval["id"]).status == "stale"  # type: ignore[union-attr]


async def test_an_unreadable_executed_result_is_a_clear_error(tmp_path: Path) -> None:
    state, _paths, scan_id = _prepare(tmp_path)
    digest, _ = _selection(state, scan_id)
    gate = BrokerGate()
    server = build_mcp_server(state, approval_gate=gate)
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        first = await session.call_tool("delete", _delete_args(scan_id, digest, wait_seconds=0))
        approval_id = first.structuredContent["approval_id"]
        gate.broker.approve(approval_id, channel={})
        gate.broker.claim(approval_id)
        gate.broker.finish(approval_id, status="executed", result={"bogus": 1})
        bad = await session.call_tool(
            "delete_status", {"approval_id": approval_id, "wait_seconds": 0}
        )
    assert bad.isError is True and "unreadable" in str(bad.content)


def test_a_stale_channel_file_is_ignored(tmp_path: Path) -> None:
    from reclaim.approvals import read_channel_file, write_channel_file

    path = tmp_path / "dashboard_channel.json"
    write_channel_file(path, port=1234, token="not-a-secret")  # noqa: S106 -- test value
    assert read_channel_file(path) is not None  # our own pid is alive
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f'"pid": {os.getpid()}', '"pid": 2147483000'), encoding="utf-8")
    assert read_channel_file(path) is None  # a crash left it behind: do not trust its port


def test_channel_inputs_are_length_capped_and_the_list_route_needs_the_token(dashboard) -> None:  # type: ignore[no-untyped-def]
    state, client = dashboard
    too_long = {**_PAYLOAD, "sample_paths": ["C:/" + "x" * 5000]}
    assert (
        client.post("/api/mcp-channel/approvals", json=too_long, headers=_mcp(state)).status_code
        == 422
    )
    huge_client = {**_PAYLOAD, "client_id": "c" * 5000}
    assert (
        client.post("/api/mcp-channel/approvals", json=huge_client, headers=_mcp(state)).status_code
        == 422
    )

    created = client.post("/api/mcp-channel/approvals", json=_PAYLOAD, headers=_mcp(state)).json()
    assert client.get("/api/mcp-channel/approvals").status_code == 403
    assert client.get("/api/mcp-channel/approvals", headers=_browser(state)).status_code == 403
    listed = client.get("/api/mcp-channel/approvals", headers=_mcp(state)).json()
    assert [a["id"] for a in listed["approvals"]] == [created["id"]]

    state.approval_broker.approve(created["id"], channel={})
    state.approval_broker.claim(created["id"])
    big = {"status": "executed", "result": {"x": "y" * 30_000}}
    posted = client.post(
        f"/api/mcp-channel/approvals/{created['id']}/finish", json=big, headers=_mcp(state)
    )
    assert posted.status_code == 422
    missing = client.get("/api/mcp-channel/approvals/nope", headers=_mcp(state))
    assert missing.status_code == 404 and "restarted" in missing.json()["detail"]


# keep the typed errors imported for readers: these are what the tool surfaces as isError text
_ = (ApprovalDeclinedError, ApprovalExpiredError, ApprovalUnavailableError)
_ = (SelectionMismatchError, StaleScanError)
