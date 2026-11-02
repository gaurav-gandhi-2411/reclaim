from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp_gates import BrokerGate
from test_mcp import _build_power_mode_state, _write
from test_mcp_planner_tools import _config, _snapshot

import reclaim.mcp.audit as audit_module
from reclaim import relocate
from reclaim.api import relocate_mcp, relocate_routes
from reclaim.api.approval_routes import ApprovalCreate
from reclaim.app_paths import data_root
from reclaim.approvals import (
    EXECUTING_LEASE_SECONDS,
    PENDING_TTL_SECONDS,
    RELOCATE_EXECUTING_LEASE_SECONDS,
    ApprovalBroker,
)
from reclaim.mcp.approval_gate import ApprovalUnavailableError
from reclaim.mcp.server import build_mcp_server

# Assistant-initiated relocations (docs/specs/assistant-mcp.md section 4a): plan, request, and a
# move that happens only after a click in Reclaim's window. Everything runs on folders under the
# test's own tmp dir: the "home" profile is a per-test dir (the repo-wide redirect uses the shared
# basetemp as home, which would let one test's hub folder leak into the next), and a tmp folder
# stands in for the second fixed drive (same trick as tests/test_relocate_api.py). The click is
# the test calling the broker's `approve`, the one thing no MCP tool can do.

pytestmark = pytest.mark.skipif(os.name != "nt", reason="relocate targets Windows/NTFS only")

_CLICK = {"sec_fetch_site": "test", "user_agent": "pytest-user"}


class _Audit:
    """Stands in for `reclaim.mcp.audit.logger` (structlog's capture cannot see cached loggers)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    def named(self, event: str, **match: Any) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if e["event"] == event and all(e.get(k) == v for k, v in match.items())
        ]


class _Gate(BrokerGate):
    """The HTTP gate answers an unknown id with `ApprovalUnavailableError` (a 404); so does this."""

    def get(self, approval_id: str) -> dict[str, Any]:
        approval = self.broker.get(approval_id)
        if approval is None:
            raise ApprovalUnavailableError("unknown approval id")
        return approval.public()


@dataclass
class Env:
    state: Any
    home: Path
    hub: Path
    local: Path
    drive: Path
    gate: _Gate
    audit: _Audit

    @property
    def journals(self) -> Path:
        return relocate_routes.journal_dir(self.state)

    @property
    def old_copy(self) -> Path:
        return Path(str(self.hub) + relocate.MOVED_SUFFIX)

    def server(self) -> Any:
        return build_mcp_server(self.state, approval_gate=self.gate)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    for name, value in (
        ("USERPROFILE", home),
        ("HOME", home),
        ("LOCALAPPDATA", local),
        ("APPDATA", home / "AppData" / "Roaming"),
    ):
        monkeypatch.setenv(name, str(value))
    hub = home / ".cache" / "huggingface" / "hub"
    _write(hub / "models--org--big" / "blobs" / "b1", b"w" * 4_096)
    _write(hub / "models--org--big" / "blobs" / "b2", b"v" * 2_048)
    drive = tmp_path / "D"
    drive.mkdir()
    drive_key = relocate._key(drive)
    # One test volume: the tmp "D" folder reads as a second fixed drive.
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(drive_key) else 1
    )
    monkeypatch.setattr(relocate_routes, "_target_drives", lambda _anchor: [drive])
    audit = _Audit()
    monkeypatch.setattr(audit_module, "logger", audit)
    # Reclaim's own data folder is a must-stay location: keep it apart from home and the drive.
    (tmp_path / "app").mkdir()
    state = _build_power_mode_state(tmp_path / "app", config=_config())
    return Env(state, home, hub, local, drive, _Gate(), audit)


# --- helpers -----------------------------------------------------------------------------------


async def _ids(session: Any, source: Path) -> tuple[str, str, dict[str, Any]]:
    listing = (await session.call_tool("list_relocation_options", {})).structuredContent
    option = next(o for o in listing["options"] if o["path"] == str(source))
    return option["option_id"], listing["drives"][0]["drive_id"], listing


async def _plan(session: Any, option_id: str, drive_id: str) -> dict[str, Any]:
    result = await session.call_tool(
        "plan_relocation", {"option_id": option_id, "drive_id": drive_id}
    )
    assert result.isError is False, result.content
    body: dict[str, Any] = result.structuredContent
    return body


async def _request(
    session: Any, option_id: str, drive_id: str, plan_hash: str, **extra: Any
) -> Any:
    return await session.call_tool(
        "request_relocation",
        {"option_id": option_id, "drive_id": drive_id, "plan_hash": plan_hash, **extra},
    )


async def _pending_request(session: Any, env: Env) -> tuple[str, str, str, str]:
    """Plans and requests with wait_seconds=0: the card is open, nothing has been answered."""
    option_id, drive_id, _ = await _ids(session, env.hub)
    plan = await _plan(session, option_id, drive_id)
    assert plan["ok"] is True, plan
    first = await _request(session, option_id, drive_id, plan["plan_hash"], wait_seconds=0)
    assert first.isError is False, first.content
    assert first.structuredContent["status"] == "awaiting_user"
    return first.structuredContent["approval_id"], option_id, drive_id, plan["plan_hash"]


async def _click_when_pending(env: Env, *, approve: bool, check: Any = None) -> None:
    """The user: wait for the card, optionally look at the disk (nothing may have moved), click."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pending = [a for a in env.gate.broker.listing() if a.status == "pending"]
        if pending:
            if check is not None:
                check()
            decide = env.gate.broker.approve if approve else env.gate.broker.decline
            decide(pending[0].id, channel=_CLICK)
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no approval request ever appeared")


def _is_link(path: Path) -> bool:
    return relocate._is_reparse(str(path))


def _assert_nothing_moved(env: Env) -> None:
    assert not _is_link(env.hub)
    assert (env.hub / "models--org--big" / "blobs" / "b1").exists()
    assert not env.old_copy.exists()
    assert not env.journals.exists()
    assert not (env.drive / "reclaim-relocated").exists()


# --- the listing and the plan ------------------------------------------------------------------


async def test_listing_offers_opaque_ids_and_no_tool_takes_a_path_a_drive_or_a_token(
    env: Env,
) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, listing = await _ids(session, env.hub)
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert re.fullmatch(r"reloc-[0-9a-f]{16}", option_id)
    assert re.fullmatch(r"drive-[0-9a-f]{12}", drive_id)
    assert listing["drives"][0]["drive"] == str(env.drive)
    for name in ("plan_relocation", "request_relocation", "relocation_status"):
        props = set(tools[name].inputSchema["properties"])
        assert not [
            p for p in props if re.search(r"path|drive$|letter|token|approve|decision", p)
        ], (
            name,
            props,
        )
    assert set(tools["plan_relocation"].inputSchema["properties"]) == {"option_id", "drive_id"}
    assert set(tools["request_relocation"].inputSchema["properties"]) == {
        "option_id",
        "drive_id",
        "plan_hash",
        "wait_seconds",
    }
    assert set(tools["relocation_status"].inputSchema["properties"]) == {
        "approval_id",
        "wait_seconds",
    }


async def test_plan_shows_the_dry_run_and_changes_nothing(env: Env) -> None:
    before = _snapshot([env.hub], env.journals)
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
    assert _snapshot([env.hub], env.journals) == before  # not even the in-use probe's rename
    _assert_nothing_moved(env)
    assert plan["ok"] is True and plan["reasons"] == [], plan["reasons"]
    assert plan["files"] == 2 and plan["bytes_total"] == 6_144
    assert plan["source"] == str(env.hub)
    assert Path(plan["target"]).is_relative_to(env.drive / "reclaim-relocated")
    assert plan["old_copy_kept_at"] == str(env.old_copy)
    assert plan["reversible"] is True and plan["plan_hash"]
    assert "reclaim relocate" in plan["rollback"] and "rollback" in plan["rollback"]
    assert env.gate.requests == []  # nobody was asked anything
    assert env.audit.named("mcp.relocation_planned", ok=True)


async def test_plan_reports_refusals_and_hands_out_no_hash_for_them(env: Env) -> None:
    # A hardlink-dependent cache (uv) and a folder that holds an excluded project.
    _write(env.local / "uv" / "cache" / "x.whl", b"u" * 100)
    _write(env.hub / "zzexclproj" / "f", b"x")
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        uv_id, drive_id, _ = await _ids(session, env.local / "uv")
        hub_id, _, _ = await _ids(session, env.hub)
        uv_plan = await _plan(session, uv_id, drive_id)
        hub_plan = await _plan(session, hub_id, drive_id)
    assert uv_plan["ok"] is False and uv_plan["plan_hash"] is None
    assert any("hardlink" in r for r in uv_plan["reasons"]), uv_plan["reasons"]
    assert hub_plan["ok"] is False and hub_plan["plan_hash"] is None
    assert any("excluded project" in r for r in hub_plan["reasons"]), hub_plan["reasons"]


# --- approve: the move happens, reversibly, audited --------------------------------------------


async def test_approve_moves_keeps_the_old_copy_and_records_the_rollback_command(env: Env) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        clicker = asyncio.create_task(
            _click_when_pending(env, approve=True, check=lambda: _assert_nothing_moved(env))
        )
        result = await _request(session, option_id, drive_id, plan["plan_hash"])
        await clicker
        again = await session.call_tool(
            "relocation_status", {"approval_id": result.structuredContent["approval_id"]}
        )

    assert result.isError is False, result.content
    body = result.structuredContent
    assert body["status"] == "executed" and body["bytes_moved"] == 6_144 and body["files"] == 2
    # The folder now lives on the other drive behind a link; the old copy is kept.
    assert _is_link(env.hub)
    assert (env.hub / "models--org--big" / "blobs" / "b1").read_bytes() == b"w" * 4_096
    # The target mirrors the whole source path under `reclaim-relocated\C\`, so under a deep
    # pytest basetemp it passes MAX_PATH (262 chars on the dev machine, LongPathsEnabled=0) and a
    # plain `exists()` says False although the move succeeded. Check through the extended-length
    # form, as the engine itself does; a genuinely missing file still fails.
    moved = Path(body["target"]) / "models--org--big" / "blobs" / "b1"
    assert Path("\\\\?\\" + str(moved.resolve())).exists()
    assert env.old_copy.is_dir() and body["old_copy_kept_at"] == str(env.old_copy)
    # The exact rollback command names this move and the journal folder it lives in.
    (journal_file,) = env.journals.glob("relocate-*.json")
    journal = relocate.Journal.load(journal_file)
    assert body["move_id"] == journal.id and journal.stage == relocate.Stage.DONE.value
    assert body["rollback_command"].startswith("reclaim relocate")
    assert str(env.journals) in body["rollback_command"]
    assert body["rollback_command"].endswith(f"rollback {journal.id}")
    assert "delete-old" in body["message"]  # told to the user, never run by a tool
    # What the card showed the user.
    (request,) = env.gate.requests
    assert request["kind"] == "relocate" and request["bytes_total"] == 6_144
    assert request["relocation"]["source"] == str(env.hub)
    assert request["relocation"]["target"] == body["target"]
    assert "rollback" in request["relocation"]["rollback_hint"]
    # Audited from request to result.
    assert env.audit.named("mcp.relocation_awaiting_approval")
    assert env.audit.named("mcp.relocation_executing")
    (executed,) = env.audit.named("mcp.relocation_executed")
    assert executed["rollback_command"] == body["rollback_command"]
    # Asking again returns the recorded result; it does not move a second time.
    assert again.structuredContent["move_id"] == body["move_id"]
    assert len(list(env.journals.glob("relocate-*.json"))) == 1
    # And the command's engine call really undoes it.
    relocate.rollback(journal_file)
    assert not _is_link(env.hub)
    assert (env.hub / "models--org--big" / "blobs" / "b2").read_bytes() == b"v" * 2_048


# --- reject, timeout ---------------------------------------------------------------------------


async def test_reject_moves_nothing_and_is_audited(env: Env) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        clicker = asyncio.create_task(_click_when_pending(env, approve=False))
        result = await _request(session, option_id, drive_id, plan["plan_hash"])
        await clicker
    assert result.isError is True and "declined" in str(result.content).lower()
    _assert_nothing_moved(env)
    assert env.audit.named("mcp.relocation_refused", reason="declined_by_user")


async def test_an_unanswered_request_moves_nothing_and_expiry_is_audited(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Clock:
        now = 1_000_000.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    env.gate = _Gate(ApprovalBroker(clock=clock))
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        approval_id, *_ = await _pending_request(session, env)
        _assert_nothing_moved(env)  # waiting out the call is not consent
        assert env.audit.named("mcp.relocation_awaiting_user")
        clock.now += PENDING_TTL_SECONDS + 1
        expired = await session.call_tool(
            "relocation_status", {"approval_id": approval_id, "wait_seconds": 0}
        )
        # a click after expiry is refused too
        assert env.gate.broker.approve(approval_id, channel=_CLICK) is None
    assert expired.isError is True and "expired" in str(expired.content).lower()
    _assert_nothing_moved(env)
    assert env.audit.named("mcp.relocation_refused", reason="expired")


# --- stale selection: refused at execution, after the click ------------------------------------


def _grow(env: Env) -> None:
    _write(env.hub / "models--org--big" / "blobs" / "b3", b"n" * 10)


def _shrink(env: Env) -> None:
    (env.hub / "models--org--big" / "blobs" / "b2").unlink()


def _replace(env: Env) -> None:
    """Same names and sizes, but a different folder (a new NTFS file id)."""
    env.hub.rename(str(env.hub) + "-old")
    _write(env.hub / "models--org--big" / "blobs" / "b1", b"w" * 4_096)
    _write(env.hub / "models--org--big" / "blobs" / "b2", b"v" * 2_048)


def _hardlink(env: Env) -> None:
    os.link(env.hub / "models--org--big" / "blobs" / "b1", env.hub / "models--org--big" / "alias")


def _exclude(env: Env) -> None:
    _write(env.hub / "zzexclproj" / "notes.txt", b"x")


def _vanish(env: Env) -> None:
    shutil.rmtree(env.hub)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (_grow, None),
        (_shrink, None),
        (_replace, None),
        (_hardlink, "hardlinked"),
        (_exclude, "excluded project"),
        (_vanish, None),
    ],
    ids=["grows", "shrinks", "replaced", "gains-hardlink", "becomes-excluded", "disappears"],
)
async def test_a_selection_that_changed_after_the_request_is_refused_at_execution(
    env: Env, change: Any, why: str | None
) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        approval_id, *_ = await _pending_request(session, env)
        change(env)
        assert env.gate.broker.approve(approval_id, channel=_CLICK) is not None
        done = await session.call_tool(
            "relocation_status", {"approval_id": approval_id, "wait_seconds": 5}
        )
    assert done.isError is True and "AFTER the user approved" in str(done.content)
    record = env.gate.broker.get(approval_id)
    assert record is not None and record.status == "stale"  # not "failed": caught by the re-check
    assert not _is_link(env.hub)
    assert not env.old_copy.exists() and not env.journals.exists()
    assert not (env.drive / "reclaim-relocated").exists()
    (refused,) = env.audit.named("mcp.relocation_refused", reason="changed_after_approval")
    if why is not None:
        assert why in refused["detail"], refused["detail"]
    assert not env.audit.named("mcp.relocation_executing")


async def test_a_program_holding_a_file_open_at_approval_time_refuses_the_move(env: Env) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        approval_id, *_ = await _pending_request(session, env)
        assert env.gate.broker.approve(approval_id, channel=_CLICK) is not None
        with (env.hub / "models--org--big" / "blobs" / "b1").open("rb"):  # a live handle
            done = await session.call_tool(
                "relocation_status", {"approval_id": approval_id, "wait_seconds": 5}
            )
    assert done.isError is True
    record = env.gate.broker.get(approval_id)
    assert record is not None and record.status == "stale"
    (refused,) = env.audit.named("mcp.relocation_refused", reason="changed_after_approval")
    assert "in use" in refused["detail"], refused["detail"]
    _assert_nothing_moved(env)


async def test_a_wrong_plan_hash_or_an_unmovable_plan_is_refused_before_the_user_is_asked(
    env: Env,
) -> None:
    _write(env.local / "uv" / "cache" / "x.whl", b"u" * 100)
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        bad_hash = await _request(session, option_id, drive_id, "0" * 64)
        uv_id, _, _ = await _ids(session, env.local / "uv")
        unmovable = await _request(session, uv_id, drive_id, "0" * 64)
    assert bad_hash.isError is True and "plan_hash" in str(bad_hash.content)
    assert unmovable.isError is True and "hardlink" in str(unmovable.content)
    assert env.gate.requests == []
    assert env.audit.named("mcp.relocation_refused", reason="plan_hash_mismatch")
    assert env.audit.named("mcp.relocation_refused", reason="not_movable")
    _assert_nothing_moved(env)


# --- the MCP surface cannot approve ------------------------------------------------------------


async def test_an_attempt_to_approve_through_mcp_is_refused_and_audited(env: Env) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert not [n for n in names if re.search(r"approv|declin|confirm|consent|decide", n)]
        approval_id, option_id, drive_id, plan_hash = await _pending_request(session, env)

        attempts: list[tuple[str, dict[str, Any]]] = [
            ("approve_relocation", {"approval_id": approval_id}),
            ("approve", {"approval_id": approval_id}),
            ("confirm_relocation", {"approval_id": approval_id, "token": "x"}),
            ("relocation_status", {"approval_id": approval_id, "approve": True}),
            ("relocation_status", {"approval_id": approval_id, "decision": "approved"}),
            ("delete_status", {"approval_id": approval_id, "approved": True}),
            (
                "request_relocation",
                {
                    "option_id": option_id,
                    "drive_id": drive_id,
                    "plan_hash": plan_hash,
                    "approval_token": "forged",
                    "wait_seconds": 0,
                },
            ),
            ("plan_relocation", {"option_id": option_id, "drive_id": drive_id, "confirm": True}),
        ]
        for name, args in attempts:
            result = await session.call_tool(name, args)
            assert result.isError is True, (name, args, result.content)

        # forged or path-shaped request ids reach nothing
        for forged in ("forged-id", "../../mcp/approvals/" + approval_id + "/approve?"):
            result = await session.call_tool("relocation_status", {"approval_id": forged})
            assert result.isError is True, forged

    record = env.gate.broker.get(approval_id)
    assert record is not None and record.status == "pending", "nothing approved it"
    _assert_nothing_moved(env)
    refused = env.audit.named("mcp.call_refused")
    assert len(refused) == len(attempts)  # every attempt, each of the two shapes
    assert {e["reason"] for e in refused} == {"unknown_tool", "undeclared_argument"}
    assert all(
        e["approval_attempt"] for e in refused if e["tool"].startswith(("approve", "confirm"))
    )
    assert any(e["arguments"] == ["approve"] for e in refused)
    assert len(env.audit.named("mcp.relocation_refused", reason="approval_unavailable")) == 2


async def test_an_approval_for_one_kind_of_request_cannot_run_the_other(env: Env) -> None:
    clean = env.gate.broker.create(
        client_id=None,
        scan_id="scan-1",
        tier="A",
        rule_id_or_category="dev_artifact_node_modules",
        selection_hash="h",
        item_count=1,
        bytes_total=1,
        sample_paths=[],
        protected_names=[],
        reversible_until_days=30,
    )
    env.gate.broker.approve(clean.id, channel=_CLICK)  # a real click, on a delete card
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        wrong = await session.call_tool("relocation_status", {"approval_id": clean.id})
        approval_id, *_ = await _pending_request(session, env)
        wrong_way = await session.call_tool("delete_status", {"approval_id": approval_id})
        listed = (await session.call_tool("delete_requests", {})).structuredContent
    assert wrong.isError is True and wrong_way.isError is True
    still = env.gate.broker.get(clean.id)
    assert still is not None and still.status == "approved", "the delete's click was not used"
    _assert_nothing_moved(env)
    assert len(env.audit.named("mcp.relocation_refused", reason="wrong_request_kind")) == 1
    assert len(env.audit.named("mcp.delete_refused", reason="wrong_request_kind")) == 1
    assert {r["kind"] for r in listed["requests"]} == {"clean", "relocate"}


async def test_ids_that_carry_a_path_or_drive_letter_or_name_another_folder_are_rejected(
    env: Env,
) -> None:
    other = env.home / "Documents"
    _write(other / "keep.txt", b"mine")
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        well_formed_but_not_listed = relocate_mcp.option_id_for(str(other))  # exists, not offered
        cases = [
            ("plan_relocation", {"option_id": str(env.hub), "drive_id": drive_id}),
            ("plan_relocation", {"option_id": option_id, "drive_id": "D:"}),
            ("plan_relocation", {"option_id": option_id, "drive_id": str(env.drive)}),
            ("plan_relocation", {"option_id": well_formed_but_not_listed, "drive_id": drive_id}),
            ("plan_relocation", {"option_id": option_id, "drive_id": "drive-" + "0" * 12}),
            ("plan_relocation", {"option_id": option_id, "drive_id": drive_id, "path": str(other)}),
            (
                "request_relocation",
                {"option_id": str(env.hub), "drive_id": drive_id, "plan_hash": plan["plan_hash"]},
            ),
            (
                "request_relocation",
                {"option_id": option_id, "drive_id": "D:\\", "plan_hash": plan["plan_hash"]},
            ),
        ]
        for name, args in cases:
            result = await session.call_tool(name, args)
            assert result.isError is True, (name, args)
    assert env.gate.requests == []
    _assert_nothing_moved(env)
    assert (other / "keep.txt").read_bytes() == b"mine"


async def test_delete_old_and_rollback_are_not_reachable_from_mcp(env: Env) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert not [n for n in names if re.search(r"delete.?old|rollback|recover|undo", n)], names
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        clicker = asyncio.create_task(_click_when_pending(env, approve=True))
        await _request(session, option_id, drive_id, plan["plan_hash"])
        await clicker
        for name in ("delete_old", "delete-old", "rollback", "rollback_relocation", "recover"):
            result = await session.call_tool(name, {"approval_id": "x", "move_id": "x"})
            assert result.isError is True, name
    assert _is_link(env.hub) and env.old_copy.is_dir(), "the old copy is still there"


def test_the_mcp_package_cannot_name_the_engine_calls_that_undo_or_delete() -> None:
    """Source level (AST): the MCP package never imports the relocate engine, and the one module
    it reaches relocation through never calls rollback, recover or delete_old_copy."""
    src = Path(__file__).parent.parent / "src" / "reclaim"
    offenders: list[str] = []
    for source in (src / "mcp").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "reclaim.relocate"
                or (node.module == "reclaim" and any(a.name == "relocate" for a in node.names))
            ):
                offenders.append(f"{source.name}:{node.lineno} imports the relocate engine")
            if isinstance(node, ast.Import) and any(
                a.name == "reclaim.relocate" for a in node.names
            ):
                offenders.append(f"{source.name}:{node.lineno} imports the relocate engine")
    tree = ast.parse((src / "api" / "relocate_mcp.py").read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    offenders.extend(sorted(used & {"rollback", "recover", "delete_old_copy"}))
    assert not offenders, offenders


# --- supporting pieces -------------------------------------------------------------------------


def test_a_relocate_request_must_carry_its_detail_and_a_clean_one_must_not() -> None:
    base: dict[str, Any] = {
        "scan_id": "(relocation)",
        "tier": "-",
        "rule_id_or_category": "reloc-" + "0" * 16,
        "selection_hash": "h",
        "item_count": 1,
        "bytes_total": 1,
    }
    detail = {
        "option_id": "reloc-" + "0" * 16,
        "drive_id": "drive-" + "0" * 12,
        "source": "C:/a",
        "target": "D:/a",
        "moved": "C:/a.reclaim-moved",
        "rollback_hint": "x",
        "target_free_bytes": 1,
    }
    assert ApprovalCreate(**base, kind="relocate", relocation=detail).relocation is not None
    with pytest.raises(ValueError, match="needs `relocation`"):
        ApprovalCreate(**base, kind="relocate")
    with pytest.raises(ValueError, match="only it may carry one"):
        ApprovalCreate(**base, kind="clean", relocation=detail)
    with pytest.raises(ValueError):
        ApprovalCreate(**base, kind="relocate", relocation={**detail, "source": "C" * 2000})


def test_a_relocation_has_a_lease_long_enough_for_a_big_copy() -> None:
    clock = SimpleNamespace(now=1_000.0)
    broker = ApprovalBroker(clock=lambda: clock.now)
    approval = broker.create(
        client_id=None,
        scan_id="(relocation)",
        tier="-",
        rule_id_or_category="r",
        selection_hash="h",
        item_count=1,
        bytes_total=1,
        sample_paths=[],
        protected_names=[],
        reversible_until_days=None,
        kind="relocate",
        relocation={"source": "C:/a"},
    )
    broker.approve(approval.id, channel={})
    assert broker.claim(approval.id) is True
    clock.now += EXECUTING_LEASE_SECONDS + 1  # a delete would have been written off by now
    got = broker.get(approval.id)
    assert got is not None and got.status == "executing"
    clock.now += RELOCATE_EXECUTING_LEASE_SECONDS
    got = broker.get(approval.id)
    assert got is not None and got.status == "failed"


def test_the_rollback_command_names_the_journal_folder_only_when_it_is_not_the_default() -> None:
    default = SimpleNamespace(vault_dir=data_root() / "data" / "quarantine")
    other = SimpleNamespace(vault_dir=Path("X:/elsewhere/quarantine"))
    assert relocate_mcp.rollback_command(default, "abc") == "reclaim relocate rollback abc"  # type: ignore[arg-type]
    assert relocate_mcp.rollback_command(other, "abc") == (  # type: ignore[arg-type]
        'reclaim relocate --journal-dir "X:\\elsewhere\\relocate" rollback abc'
    )
    # The status/recover hints name the same folder, or they would look in the wrong place.
    assert relocate_mcp.rollback_hint(other).startswith(  # type: ignore[arg-type]
        'reclaim relocate --journal-dir "X:\\elsewhere\\relocate" status'
    )
    assert 'relocate --journal-dir "X:\\elsewhere\\relocate" recover <id>' in (
        relocate_mcp.recover_hint(other)  # type: ignore[arg-type]
    )


# --- a move that fails part-way says how to recover, and is audited however it failed ---------


def _text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def test_a_move_that_fails_after_the_click_names_the_recover_command(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_state: Any, _live: Any) -> Any:
        raise OSError("the target drive went away mid-copy")

    monkeypatch.setattr(relocate_mcp, "execute_relocation", broken)
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        clicker = asyncio.create_task(_click_when_pending(env, approve=True))
        result = await _request(session, option_id, drive_id, plan["plan_hash"])
        await clicker
    assert result.isError is True
    text = _text(result)
    assert "the target drive went away mid-copy" in text
    assert "reclaim relocate" in text and "recover <id>" in text and "status" in text
    (record,) = env.gate.broker.listing()
    assert record.status == "failed" and "recover <id>" in (record.error or "")
    (failed,) = env.audit.named("mcp.relocation_failed")
    assert "went away" in failed["error"]


async def test_a_lease_that_ran_out_is_reported_with_the_recover_command_and_audited(
    env: Env,
) -> None:
    async with create_connected_server_and_client_session(env.server()._mcp_server) as session:
        approval_id, *_ = await _pending_request(session, env)
        broker = env.gate.broker
        broker.approve(approval_id, channel=_CLICK)
        assert broker.claim(approval_id) is True  # an executing call that then died
        broker._items[approval_id].claim_deadline = 0.0  # its lease has run out
        result = await session.call_tool(
            "relocation_status", {"approval_id": approval_id, "wait_seconds": 0}
        )
    assert result.isError is True
    text = _text(result)
    assert "(failed)" in text and "never reported back" in text and "recover <id>" in text
    (failed,) = env.audit.named("mcp.relocation_failed", approval_id=approval_id)
    assert failed["seen_by"] == "status"


async def test_request_relocation_audits_an_unreachable_approval_window(env: Env) -> None:
    class _Closed(_Gate):
        def request(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise ApprovalUnavailableError("Reclaim's window is not running")

    server = build_mcp_server(env.state, approval_gate=_Closed())
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        option_id, drive_id, _ = await _ids(session, env.hub)
        plan = await _plan(session, option_id, drive_id)
        result = await _request(session, option_id, drive_id, plan["plan_hash"])
    assert result.isError is True
    assert env.audit.named("mcp.relocation_refused", reason="approval_unavailable")
    _assert_nothing_moved(env)
