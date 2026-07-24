from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from reclaim.api import ai_orchestration, service
from reclaim.api.schemas import (
    AIAnalysisStatusOut,
    AISuggestionsResponse,
    ApplyRequest,
    ApplyStatusOut,
    CandidatesResponse,
    DuplicateClusterReviewResponse,
    FirstRunStatusResponse,
    ModeStatusResponse,
    OneClickCleanSummaryResponse,
    PowerModeRequest,
    QuarantineListResponse,
    RecoveryStatusResponse,
    RestoreStatusOut,
    ScanRequest,
    ScanStatusOut,
    SuggestedScanRootsResponse,
    SummaryResponse,
    TreemapResponse,
)
from reclaim.api.state import AIAnalysisStatus, ApplyStatus, AppState, RestoreStatus, ScanStatus
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
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


@router.get("/scan/suggested-roots", response_model=SuggestedScanRootsResponse)
def scan_suggested_roots() -> SuggestedScanRootsResponse:
    return service.suggested_scan_roots()


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


@router.get("/clean/one-click-summary", response_model=OneClickCleanSummaryResponse)
def clean_one_click_summary(request: Request) -> OneClickCleanSummaryResponse:
    return service.build_one_click_summary(get_state(request))


@router.get("/duplicate-clusters/review", response_model=DuplicateClusterReviewResponse)
def duplicate_cluster_review(request: Request, limit: int = 15) -> DuplicateClusterReviewResponse:
    if limit < 1:
        raise HTTPException(status_code=400, detail=f"limit must be >= 1 (got {limit!r})")
    return service.list_duplicate_cluster_review(get_state(request), limit=limit)


@router.post("/apply", response_model=ApplyStatusOut, status_code=202)
def apply(
    payload: ApplyRequest, background_tasks: BackgroundTasks, request: Request
) -> ApplyStatusOut:
    """fix/apply-progress-feedback: mirrors `POST /api/scan`'s background-task + polling shape —
    a large apply's ADR-0026 fsync cost previously blocked this HTTP request for the whole
    multi-minute duration with zero progress and real risk of a client/proxy timeout. Request-
    shape validation (bad tier, safe mode's blanket-selection gate) stays synchronous — see
    `service.resolve_apply_selection` — only the real, potentially slow `apply_batch` filesystem
    work moved to the background (`service.run_apply`)."""
    if payload.tier not in ("A", "B", "both"):
        raise HTTPException(
            status_code=400, detail=f"tier must be one of A, B, both (got {payload.tier!r})"
        )
    state = get_state(request)
    try:
        selected, method, apply_flag = service.resolve_apply_selection(state, payload)
    except SafeModeViolationError as exc:
        # A routine, expected outcome of the caller (the dashboard frontend, or a future
        # different client) not respecting the safe-mode contract — e.g. requesting a blanket
        # tier-apply with no explicit paths — a real 400, not a sign anything is broken.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    started_at = time.time()
    with state.lock:
        if state.apply_status.status == "running":
            raise HTTPException(status_code=409, detail="an apply is already running")
        state.apply_status = ApplyStatus(
            status="running",
            items_processed=0,
            items_total=len(selected),
            started_at=started_at,
        )
        status_snapshot = state.apply_status

    background_tasks.add_task(service.run_apply, state, selected, method, apply_flag, started_at)
    return service.to_apply_status_out(status_snapshot)


@router.get("/apply/status", response_model=ApplyStatusOut)
def apply_status(request: Request) -> ApplyStatusOut:
    state = get_state(request)
    with state.lock:
        return service.to_apply_status_out(state.apply_status)


# --- AI suggestions (recommend-only; ADR-0025) -------------------------------------------------


@router.post("/ai/analyze", response_model=AIAnalysisStatusOut)
def start_ai_analysis(
    background_tasks: BackgroundTasks, request: Request, response: Response
) -> AIAnalysisStatusOut:
    """Mirrors `POST /api/scan`'s exact shape: starts a background analysis and returns
    immediately. Degraded mode (no `ai` extra installed) returns a typed "unavailable" body with
    the default `200` — nothing was accepted for background work, so `202` would be misleading."""
    state = get_state(request)
    if not ai_orchestration.ai_extra_available():
        return service.ai_status_out(state)

    if not service.has_scan_data(state):
        raise HTTPException(status_code=400, detail="run a scan before analyzing with AI")

    started_at = time.time()
    with state.lock:
        if state.ai_status.status == "running":
            raise HTTPException(status_code=409, detail="an AI analysis is already running")
        scan_generation = state.scan_generation
        state.ai_status = AIAnalysisStatus(
            status="running", scan_generation=scan_generation, started_at=started_at
        )
        status_snapshot = state.ai_status

    background_tasks.add_task(service.run_ai_analysis, state, scan_generation, started_at)
    response.status_code = 202
    return service.to_ai_status_out(status_snapshot, current_scan_generation=scan_generation)


@router.get("/ai/status", response_model=AIAnalysisStatusOut)
def ai_status(request: Request) -> AIAnalysisStatusOut:
    return service.ai_status_out(get_state(request))


@router.get("/ai/suggestions", response_model=AISuggestionsResponse)
def ai_suggestions(request: Request) -> AISuggestionsResponse:
    return service.build_ai_suggestions(get_state(request))


@router.get("/quarantine", response_model=QuarantineListResponse)
def quarantine(request: Request) -> QuarantineListResponse:
    return service.list_quarantine_batches(get_state(request))


@router.get("/recovery/status", response_model=RecoveryStatusResponse)
def recovery_status(request: Request) -> RecoveryStatusResponse:
    """Read-only preview of ADR-0026 crash recovery — see `service.recovery_status`. Never
    writes anything; a real fix still requires `reclaim recover --apply` from the CLI."""
    return service.recovery_status(get_state(request))


@router.post("/restore/{batch_id}", response_model=RestoreStatusOut, status_code=202)
def restore(batch_id: str, background_tasks: BackgroundTasks, request: Request) -> RestoreStatusOut:
    """fix/apply-progress-feedback: same background-task + polling conversion as `POST
    /api/apply` (see that route's docstring) — restoring a batch runs through the identical
    ADR-0026 fsync-bearing loop. `service.validate_restorable_batch` runs the exact same
    up-front validation `restore_batch` itself performs (cheap: a manifest read, no filesystem
    mutation) synchronously here, so a bad batch id/unsupported method/corrupted manifest still
    gets an immediate 404/409/500 exactly as before this conversion."""
    state = get_state(request)
    try:
        vault_entry_count = service.validate_restorable_batch(state, batch_id)
    except BatchNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RecycleBinRestoreUnsupportedError, DirectDeleteRestoreImpossibleError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RestoreIntegrityError as exc:
        # Same "should never trigger" honesty as apply's SafetyInvariantError -> 500: every
        # vault entry restore_batch reads back should already be well-formed, so hitting this
        # means an invariant broke (a corrupted/tampered manifest), not that the caller supplied
        # bad input.
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    started_at = time.time()
    with state.lock:
        if state.restore_status.status == "running":
            raise HTTPException(status_code=409, detail="a restore is already running")
        state.restore_status = RestoreStatus(
            status="running",
            items_processed=0,
            items_total=vault_entry_count,
            started_at=started_at,
        )
        status_snapshot = state.restore_status

    background_tasks.add_task(service.run_restore, state, batch_id, started_at)
    return service.to_restore_status_out(status_snapshot)


@router.get("/restore/status", response_model=RestoreStatusOut)
def restore_status(request: Request) -> RestoreStatusOut:
    state = get_state(request)
    with state.lock:
        return service.to_restore_status_out(state.restore_status)


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
