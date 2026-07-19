from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from reclaim.api import service
from reclaim.api.schemas import (
    ApplyRequest,
    ApplyResponse,
    CandidatesResponse,
    DuplicateClusterReviewResponse,
    FirstRunStatusResponse,
    ModeStatusResponse,
    PowerModeRequest,
    QuarantineListResponse,
    RestoreResponse,
    ScanRequest,
    ScanStatusOut,
    SummaryResponse,
    TreemapResponse,
)
from reclaim.api.state import AppState, ScanStatus
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
    SafetyInvariantError,
    restore_batch,
)
from reclaim.mode import ModeSwitchDeniedError

router = APIRouter(prefix="/api")


def get_state(request: Request) -> AppState:
    """Fetches this process's single `AppState` off `app.state.reclaim` — never a module-level
    global, so each `create_app()` instance (one per test, one per `reclaim serve` process)
    stays isolated."""
    state: AppState = request.app.state.reclaim
    return state


@router.post("/scan", response_model=ScanStatusOut, status_code=202)
def start_scan(
    payload: ScanRequest, background_tasks: BackgroundTasks, request: Request
) -> ScanStatusOut:
    state = get_state(request)
    root = Path(payload.path)
    if not root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"scan path does not exist or is not a directory: {root}",
        )

    started_at = time.time()
    with state.lock:
        if state.scan_status.status == "running":
            raise HTTPException(
                status_code=409,
                detail=f"a scan is already running for {state.scan_status.root}",
            )
        state.scan_status = ScanStatus(status="running", root=root, started_at=started_at)
        status_snapshot = state.scan_status

    background_tasks.add_task(service.run_scan, state, root, started_at)
    return service.to_scan_status_out(status_snapshot)


@router.get("/scan/status", response_model=ScanStatusOut)
def scan_status(request: Request) -> ScanStatusOut:
    state = get_state(request)
    with state.lock:
        return service.to_scan_status_out(state.scan_status)


@router.get("/summary", response_model=SummaryResponse)
def summary(request: Request) -> SummaryResponse:
    return service.build_summary(get_state(request))


@router.get("/treemap", response_model=TreemapResponse)
def treemap(request: Request) -> TreemapResponse:
    return service.build_treemap(get_state(request))


@router.get("/candidates", response_model=CandidatesResponse)
def candidates(
    request: Request, tier: str = "both", category: str | None = None
) -> CandidatesResponse:
    if tier not in ("A", "B", "both"):
        raise HTTPException(
            status_code=400, detail=f"tier must be one of A, B, both (got {tier!r})"
        )
    return service.list_candidates(get_state(request), tier=tier, category_group=category)


@router.get("/duplicate-clusters/review", response_model=DuplicateClusterReviewResponse)
def duplicate_cluster_review(request: Request, limit: int = 15) -> DuplicateClusterReviewResponse:
    if limit < 1:
        raise HTTPException(status_code=400, detail=f"limit must be >= 1 (got {limit!r})")
    return service.list_duplicate_cluster_review(get_state(request), limit=limit)


@router.post("/apply", response_model=ApplyResponse)
def apply(payload: ApplyRequest, request: Request) -> ApplyResponse:
    if payload.tier not in ("A", "B", "both"):
        raise HTTPException(
            status_code=400, detail=f"tier must be one of A, B, both (got {payload.tier!r})"
        )
    try:
        return service.apply_selection(get_state(request), payload)
    except SafetyInvariantError as exc:
        # Defense-in-depth surfaced honestly: this should never trigger (every candidate has
        # already passed SafetyValidator upstream), so a 500 is correct here — it means an
        # invariant broke, not that the caller supplied bad input.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except SafeModeViolationError as exc:
        # Unlike SafetyInvariantError above, this is a routine, expected outcome of the caller
        # (the dashboard frontend, or a future different client) not respecting the safe-mode
        # contract — e.g. requesting a blanket tier-apply with no explicit paths, or a non-
        # recycle_bin method — a real 400, not a sign anything is broken.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/quarantine", response_model=QuarantineListResponse)
def quarantine(request: Request) -> QuarantineListResponse:
    return service.list_quarantine_batches(get_state(request))


@router.post("/restore/{batch_id}", response_model=RestoreResponse)
def restore(batch_id: str, request: Request) -> RestoreResponse:
    state = get_state(request)
    try:
        report = restore_batch(
            batch_id,
            manifest_path=state.manifest_path,
            vault_dir=state.vault_dir,
            safety=state.safety,
        )
    except BatchNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RecycleBinRestoreUnsupportedError, DirectDeleteRestoreImpossibleError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RestoreIntegrityError as exc:
        # Same "should never trigger" honesty as apply's SafetyInvariantError -> 500 below:
        # every vault entry restore_batch reads back should already be well-formed, so hitting
        # this means an invariant broke (a corrupted/tampered manifest), not that the caller
        # supplied bad input.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return service.restore_response(report)


# --- Stage 2: mode + first-run -----------------------------------------------------------------


@router.get("/mode", response_model=ModeStatusResponse)
def mode_status(request: Request) -> ModeStatusResponse:
    return service.mode_status(get_state(request))


@router.post("/mode/power", response_model=ModeStatusResponse)
def mode_power(payload: PowerModeRequest, request: Request) -> ModeStatusResponse:
    try:
        return service.switch_mode_to_power(get_state(request), payload)
    except ModeSwitchDeniedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mode/safe", response_model=ModeStatusResponse)
def mode_safe(request: Request) -> ModeStatusResponse:
    return service.switch_mode_to_safe(get_state(request))


@router.get("/first-run", response_model=FirstRunStatusResponse)
def first_run_status(request: Request) -> FirstRunStatusResponse:
    return service.first_run_status(get_state(request))


@router.post("/first-run/acknowledge", response_model=FirstRunStatusResponse)
def first_run_acknowledge(request: Request) -> FirstRunStatusResponse:
    return service.acknowledge_first_run_screen(get_state(request))
