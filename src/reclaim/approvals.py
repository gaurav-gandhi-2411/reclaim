from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Approval broker for assistant-initiated (MCP) deletes -- docs/specs/assistant-mcp.md section 5.
#
# Lives in the DASHBOARD process's memory (one broker per `AppState`). An approval is created by
# the MCP server over the token-authenticated `/api/mcp-channel/` routes, shown in Reclaim's own
# window, and decided ONLY by a click that reaches `approve`/`decline` through the CSRF-protected
# browser routes. Nothing in the MCP package imports the deciding methods (a source-level test
# enforces it), and the MCP channel token cannot reach the deciding routes. State is deliberately
# in memory, not a file: a file the MCP process can read and an agent can edit would make the
# decision forgeable by a one-line write.
#
# State machine:  pending -> approved -> executing -> executed | failed | stale
#                 pending -> declined | expired     approved -> expired (never claimed)

ApprovalStatus = Literal[
    "pending", "approved", "declined", "expired", "executing", "executed", "failed", "stale"
]

# Generous enough to walk to the machine and read the dialog, short enough that an old request
# does not sit approvable for hours.
PENDING_TTL_SECONDS = 600.0
# An approved request must be claimed by the waiting MCP call promptly; otherwise the click is void.
APPROVED_CLAIM_TTL_SECONDS = 120.0
# A runaway agent must not be able to bury the user's window in requests.
MAX_PENDING = 5
_KEEP_FINISHED = 20  # decided/finished records kept for the window's "recent" list


CHANNEL_FILE_NAME = "dashboard_channel.json"


def channel_file_path(db_path: Path) -> Path:
    """Where a running dashboard publishes how the MCP server can reach it: next to the index, so
    the two processes agree whenever they point at the same data directory."""
    return db_path.parent / CHANNEL_FILE_NAME


def write_channel_file(path: Path, *, port: int, token: str) -> None:
    """Atomic. Holds the per-process MCP channel token, which can create/read approval requests
    but not decide them (see `reclaim.api.security`)."""
    payload = {"port": port, "pid": os.getpid(), "token": token, "started_at": time.time()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def read_channel_file(path: Path) -> dict[str, Any] | None:
    """None for a missing, unreadable or malformed file (never raises)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("port"), int):
        return None
    if not isinstance(raw.get("token"), str) or not raw["token"]:
        return None
    return raw


def remove_channel_file(path: Path, *, token: str) -> None:
    """Removes the file only if it is still OURS (a newer dashboard may have replaced it)."""
    current = read_channel_file(path)
    if current is not None and current.get("token") == token:
        with contextlib.suppress(OSError):
            path.unlink()


class BrokerFullError(RuntimeError):
    """Too many pending approvals; the caller should wait for the user to decide the others."""


@dataclass(slots=True)
class Approval:
    id: str
    created_at: float
    expires_at: float
    status: ApprovalStatus
    client_id: str | None
    scan_id: str
    tier: str
    rule_id_or_category: str
    selection_hash: str
    item_count: int
    bytes_total: int
    sample_paths: list[str]
    # What the window states next to the buttons. Computed by the MCP side from the live config.
    protected_names: list[str] = field(default_factory=list)
    # "vault" (restorable for at least `reversible_until_days` days) or "recycle_bin" (restore
    # from Windows' Recycle Bin). Never a permanent delete (invariant I7).
    method: str = "vault"
    reversible_until_days: int | None = None
    decided_at: float | None = None
    decision_channel: dict[str, str] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    claim_deadline: float | None = None

    def public(self) -> dict[str, Any]:
        """Everything the window or the waiting MCP call may see (no secrets exist here)."""
        return asdict(self)


class ApprovalBroker:
    def __init__(self, *, clock: Any = time.time) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Approval] = {}
        self._clock = clock

    # --- requester side (MCP channel) ---------------------------------------------------------

    def create(
        self,
        *,
        client_id: str | None,
        scan_id: str,
        tier: str,
        rule_id_or_category: str,
        selection_hash: str,
        item_count: int,
        bytes_total: int,
        sample_paths: list[str],
        protected_names: list[str],
        reversible_until_days: int | None,
        method: str = "vault",
    ) -> Approval:
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            pending = [a for a in self._items.values() if a.status == "pending"]
            if len(pending) >= MAX_PENDING:
                raise BrokerFullError(
                    f"{len(pending)} approvals are already waiting for a decision in Reclaim's "
                    "window; wait for the user to answer them."
                )
            approval = Approval(
                id=secrets.token_urlsafe(16),
                created_at=now,
                expires_at=now + PENDING_TTL_SECONDS,
                status="pending",
                client_id=client_id,
                scan_id=scan_id,
                tier=tier,
                rule_id_or_category=rule_id_or_category,
                selection_hash=selection_hash,
                item_count=item_count,
                bytes_total=bytes_total,
                sample_paths=list(sample_paths),
                protected_names=list(protected_names),
                reversible_until_days=reversible_until_days,
                method=method,
            )
            self._items[approval.id] = approval
            return approval

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            self._expire_locked(self._clock())
            return self._items.get(approval_id)

    def claim(self, approval_id: str) -> bool:
        """approved -> executing, exactly once. False for any other state (never raises)."""
        with self._lock:
            self._expire_locked(self._clock())
            approval = self._items.get(approval_id)
            if approval is None or approval.status != "approved":
                return False
            approval.status = "executing"
            return True

    def finish(
        self,
        approval_id: str,
        *,
        status: Literal["executed", "failed", "stale"],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        with self._lock:
            approval = self._items.get(approval_id)
            if approval is None or approval.status != "executing":
                return False
            approval.status = status
            approval.result = result
            approval.error = error
            return True

    # --- decider side (browser routes only) ---------------------------------------------------

    def approve(self, approval_id: str, *, channel: dict[str, str]) -> Approval | None:
        return self._decide(approval_id, "approved", channel)

    def decline(self, approval_id: str, *, channel: dict[str, str]) -> Approval | None:
        return self._decide(approval_id, "declined", channel)

    def _decide(
        self, approval_id: str, status: Literal["approved", "declined"], channel: dict[str, str]
    ) -> Approval | None:
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            approval = self._items.get(approval_id)
            if approval is None or approval.status != "pending":
                return None  # unknown, already decided, or expired: a click is never replayed
            approval.status = status
            approval.decided_at = now
            approval.decision_channel = dict(channel)
            if status == "approved":
                approval.claim_deadline = now + APPROVED_CLAIM_TTL_SECONDS
            return approval

    # --- window listing -----------------------------------------------------------------------

    def listing(self) -> list[Approval]:
        """Pending first (oldest first), then the most recent finished ones."""
        with self._lock:
            self._expire_locked(self._clock())
            pending = sorted(
                (a for a in self._items.values() if a.status == "pending"),
                key=lambda a: a.created_at,
            )
            finished = sorted(
                (a for a in self._items.values() if a.status != "pending"),
                key=lambda a: a.decided_at or a.created_at,
                reverse=True,
            )[:_KEEP_FINISHED]
            return [*pending, *finished]

    def _expire_locked(self, now: float) -> None:
        for approval in self._items.values():
            if approval.status == "pending" and now >= approval.expires_at:
                approval.status = "expired"
                approval.decided_at = now
            elif (
                approval.status == "approved"
                and approval.claim_deadline is not None
                and now >= approval.claim_deadline
            ):
                approval.status = "expired"
        if len(self._items) > 4 * _KEEP_FINISHED:
            finished = sorted(
                (a for a in self._items.values() if a.status not in ("pending", "executing")),
                key=lambda a: a.decided_at or a.created_at,
            )
            for stale in finished[: len(self._items) - 4 * _KEEP_FINISHED]:
                del self._items[stale.id]
