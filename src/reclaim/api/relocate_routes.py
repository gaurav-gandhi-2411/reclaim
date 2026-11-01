from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from reclaim import relocate
from reclaim.api.state import AppState
from reclaim.drives import NoFixedDrivesFoundError, list_fixed_drives
from reclaim.preflight import check_within_allowed_scope

# Relocate (docs/specs/relocate.md, ADR-0044) as a review-tier action: the user picks a folder,
# reads the dry run's figures, and confirms. The confirmation carries a token derived from those
# figures, so a move can only start for the plan the user saw: if the folder changed since, the
# token no longer matches and the user is shown the new figures instead. Only one relocate job
# (move, rollback, delete of the old copy) runs at a time. Every route is behind the app-wide
# CSRF and local-origin middleware.

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/relocate")

_PATH = 1024


@dataclass(slots=True)
class RelocateJob:
    status: Literal["idle", "running", "done", "failed"] = "idle"
    action: str = ""
    move_id: str | None = None
    message: str = ""
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    kept: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Runtime:
    lock: threading.Lock = field(default_factory=threading.Lock)
    job: RelocateJob = field(default_factory=RelocateJob)


def _runtime(request: Request) -> _Runtime:
    app_state = request.app.state
    runtime: _Runtime | None = getattr(app_state, "relocate", None)
    if runtime is None:
        runtime = _Runtime()
        app_state.relocate = runtime
    return runtime


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.reclaim
    return state


def journal_dir(state: AppState) -> Path:
    """Next to the vault (`data/quarantine` -> `data/relocate`), so a test's tmp vault keeps its
    journals in the same tmp dir."""
    return state.vault_dir.parent / "relocate"


def _policy(state: AppState) -> relocate.Policy:
    app_dirs = [state.vault_dir, state.db_path.parent, journal_dir(state)]
    return relocate.default_policy(state.config, environ=os.environ, app_dirs=app_dirs)


def _target_drives(source_anchor: str) -> list[Path]:
    try:
        drives = list_fixed_drives()
    except NoFixedDrivesFoundError:
        return []
    return [d for d in drives if str(d).casefold() != source_anchor.casefold()]


def _best_drive(source: Path) -> Path | None:
    drives = _target_drives(source.anchor)
    return max(drives, key=lambda d: relocate._free_bytes(str(d))) if drives else None


def _in_home(path: Path) -> bool:
    # Only folders inside the user's own profile are offered or accepted (the same default scope
    # the scan uses): the API never takes an arbitrary system path.
    return check_within_allowed_scope(path, allowed_roots=[Path.home()])


def _token(state: AppState, plan: relocate.Plan) -> str:
    figures = json.dumps([plan.source, plan.target, plan.files, plan.bytes], sort_keys=True)
    return hmac.new(state.csrf_token.encode(), figures.encode(), hashlib.sha256).hexdigest()


# --- schemas ----------------------------------------------------------------------------------


class CandidateOut(BaseModel):
    path: str
    verdict: str
    bytes: int
    files: int
    reasons: list[str]
    link_target: str | None
    suggested_target: str | None


class CandidatesOut(BaseModel):
    target_drive: str | None
    target_drive_free: int | None
    candidates: list[CandidateOut]


class PlanIn(BaseModel):
    source: str = Field(max_length=_PATH)
    target: str | None = Field(default=None, max_length=_PATH)


class PlanOut(BaseModel):
    source: str
    target: str
    moved: str
    ok: bool
    reasons: list[str]
    files: int
    bytes: int
    target_free: int
    confirm_token: str | None


class MoveIn(BaseModel):
    source: str = Field(max_length=_PATH)
    target: str = Field(max_length=_PATH)
    confirm_token: str = Field(max_length=128)


class DeleteOldIn(BaseModel):
    confirm: bool = False
    accept_missing: bool = False


class JobOut(BaseModel):
    status: str
    action: str
    move_id: str | None
    message: str
    error: str | None
    started_at: float | None
    finished_at: float | None
    kept: list[str]


class MoveOut(BaseModel):
    id: str
    source: str
    target: str
    moved: str
    stage: str
    files: int
    bytes: int
    swapped_at: float
    delete_old_allowed_at: float | None
    old_copy_kept: bool
    history: list[list[object]]


class MovesOut(BaseModel):
    moves: list[MoveOut]


def _job_out(job: RelocateJob) -> JobOut:
    return JobOut(
        status=job.status,
        action=job.action,
        move_id=job.move_id,
        message=job.message,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
        kept=list(job.kept),
    )


# --- routes -----------------------------------------------------------------------------------


@router.get("/candidates", response_model=CandidatesOut)
def candidates(request: Request) -> CandidatesOut:
    """Read-only: classifies and sizes the usual folders. No probe (it renames), no write."""
    return relocation_candidates(_state(request))


def relocation_candidates(state: AppState) -> CandidatesOut:
    """The read-only listing behind the page and the MCP tool `list_relocation_options`: one
    function, so the assistant sees exactly what the page shows, and the MCP package never
    imports the relocate engine (which can move and delete) to get it."""
    home = Path.home()
    drive = _best_drive(home)
    found = relocate.propose_candidates(relocate.known_locations(os.environ), _policy(state), drive)
    return CandidatesOut(
        target_drive=str(drive) if drive else None,
        target_drive_free=relocate._free_bytes(str(drive)) if drive else None,
        candidates=[
            CandidateOut(
                path=c.path,
                verdict=c.classification.verdict.value,
                bytes=c.classification.bytes,
                files=c.classification.files,
                reasons=list(c.classification.reasons),
                link_target=c.classification.link_target,
                suggested_target=c.suggested_target,
            )
            for c in found
        ],
    )


def _plan(state: AppState, source: Path, target: Path | None) -> relocate.Plan:
    if not _in_home(source):
        raise HTTPException(status_code=400, detail="only folders inside your user profile")
    if target is None:
        drive = _best_drive(source)
        if drive is None:
            raise HTTPException(status_code=409, detail="there is no other fixed drive")
        target = Path(relocate.suggested_target(source, drive))
    elif not any(
        relocate._key(target).startswith(relocate._key(d)) for d in _target_drives(source.anchor)
    ):
        raise HTTPException(status_code=400, detail="the target must be on another fixed drive")
    return relocate.plan_move(source, target, _policy(state), journal_dir=journal_dir(state))


def _plan_out(state: AppState, plan: relocate.Plan) -> PlanOut:
    return PlanOut(
        source=plan.source,
        target=plan.target,
        moved=plan.moved,
        ok=plan.ok,
        reasons=list(plan.reasons),
        files=plan.files,
        bytes=plan.bytes,
        target_free=plan.target_free,
        confirm_token=_token(state, plan) if plan.ok else None,
    )


@router.post("/plan", response_model=PlanOut)
def plan(payload: PlanIn, request: Request) -> PlanOut:
    """The dry run. Changes nothing (its in-use probe renames the folder and straight back)."""
    state = _state(request)
    target = Path(payload.target) if payload.target else None
    return _plan_out(state, _plan(state, Path(payload.source), target))


def _start(
    request: Request,
    background: BackgroundTasks,
    action: str,
    move_id: str | None,
    work: Callable[[], relocate.Outcome],
) -> JobOut:
    runtime = _runtime(request)
    with runtime.lock:
        if runtime.job.status == "running":
            raise HTTPException(status_code=409, detail="a relocate job is already running")
        runtime.job = RelocateJob(
            status="running", action=action, move_id=move_id, started_at=time.time()
        )
        snapshot = _job_out(runtime.job)
    background.add_task(_run, runtime, work)
    return snapshot


def _run(runtime: _Runtime, work: Callable[[], relocate.Outcome]) -> None:
    try:
        outcome: relocate.Outcome = work()
    except relocate.RelocateError as exc:
        with runtime.lock:
            runtime.job.status = "failed"
            runtime.job.error = str(exc)
            runtime.job.finished_at = time.time()
        return
    except Exception as exc:  # a bug: still reported, never swallowed silently
        logger.exception("relocate_job_crashed")
        with runtime.lock:
            runtime.job.status = "failed"
            runtime.job.error = f"unexpected error: {exc}"
            runtime.job.finished_at = time.time()
        return
    with runtime.lock:
        runtime.job.status = "done"
        runtime.job.move_id = outcome.journal.id
        runtime.job.message = outcome.message
        runtime.job.kept = list(outcome.kept)
        runtime.job.finished_at = time.time()


@router.post("/move", response_model=JobOut, status_code=202)
def move(payload: MoveIn, background: BackgroundTasks, request: Request) -> JobOut:
    """Starts the move the user confirmed. The plan is re-run first; if its figures differ from
    the ones the token was issued for, nothing starts (409) and the user reviews again."""
    state = _state(request)
    fresh = _plan(state, Path(payload.source), Path(payload.target))
    if not fresh.ok:
        raise HTTPException(status_code=409, detail="; ".join(fresh.reasons))
    if not hmac.compare_digest(_token(state, fresh), payload.confirm_token):
        raise HTTPException(
            status_code=409, detail="the folder changed since you reviewed it; check it again"
        )
    policy, journals = _policy(state), journal_dir(state)

    def work() -> relocate.Outcome:
        return relocate.execute_move(fresh.source, fresh.target, policy, journals)

    return _start(request, background, "move", None, work)


def _journal_path(state: AppState, move_id: str) -> Path:
    if not move_id.isalnum() or len(move_id) > 32:
        raise HTTPException(status_code=400, detail="not a move id")
    path = journal_dir(state) / f"relocate-{move_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no move with id {move_id}")
    return path


@router.post("/{move_id}/rollback", response_model=JobOut, status_code=202)
def rollback(
    move_id: str, background: BackgroundTasks, request: Request, use_old_copy: bool = False
) -> JobOut:
    """Puts the folder back as the programs left it (default), or with `use_old_copy` the copy
    from before the move (ADR-0044 addendum 5)."""
    path = _journal_path(_state(request), move_id)
    return _start(
        request,
        background,
        "rollback",
        move_id,
        lambda: relocate.rollback(path, use_old_copy=use_old_copy),
    )


@router.post("/{move_id}/delete-old", response_model=JobOut, status_code=202)
def delete_old(
    move_id: str, payload: DeleteOldIn, background: BackgroundTasks, request: Request
) -> JobOut:
    state = _state(request)
    path = _journal_path(state, move_id)
    # B7 M3: a delete never goes ahead for an excluded project, even a kept old copy.
    policy = _policy(state)

    def work() -> relocate.Outcome:
        return relocate.delete_old_copy(
            path, policy=policy, confirmed=payload.confirm, accept_missing=payload.accept_missing
        )

    return _start(request, background, "delete-old", move_id, work)


@router.get("/status", response_model=JobOut)
def status(request: Request) -> JobOut:
    runtime = _runtime(request)
    with runtime.lock:
        return _job_out(runtime.job)


def _old_copy_kept(journal: relocate.Journal, rows: list[relocate.Journal]) -> bool:
    """Whether `<name>.reclaim-moved` is THIS move's kept old copy. Identity alone is not
    enough: a later move of the same folder freezes a folder to the same path that can carry
    this journal's folder ID again (B8). So an undone move whose old copy was deleted keeps
    nothing, and neither does one a later, live move of the same folder has superseded. A
    ROLLED_BACK move keeps it (the page offers delete-old for it, as the engine allows)."""
    if journal.stage == relocate.Stage.DELETED_OLD and journal.rolled_back:
        return False
    superseded = any(
        other.id != journal.id
        and relocate._key(other.source) == relocate._key(journal.source)
        and not relocate._is_inert(other)
        and other.created_at >= journal.created_at
        for other in rows
    )
    return not superseded and relocate._is_original(journal, journal.moved)


@router.get("/moves", response_model=MovesOut)
def moves(request: Request) -> MovesOut:
    try:
        rows = relocate.journals(journal_dir(_state(request)))
    except relocate.RelocateError as exc:  # an unreadable journal: say which, never hide it
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    keep = relocate.KEEP_OLD_COPY_DAYS * 86400
    return MovesOut(
        moves=[
            MoveOut(
                id=j.id,
                source=j.source,
                target=j.target,
                moved=j.moved,
                stage=j.stage,
                files=j.files,
                bytes=j.bytes,
                swapped_at=j.swapped_at,
                # The engine's own clock (`_kept_since`): a swap a kill interrupted has no
                # `swapped_at`, and the 7 days count from its SWAPPING entry (B8).
                delete_old_allowed_at=(
                    relocate._kept_since(j) + keep if j.swapped_at or j.junction_attempted else None
                ),
                old_copy_kept=_old_copy_kept(j, rows),
                history=j.history,
            )
            for j in reversed(rows)
        ]
    )
