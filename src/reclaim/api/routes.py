from __future__ import annotations

import secrets
import time
from pathlib import Path

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from reclaim.api import ai_orchestration, service
from reclaim.api.schemas import (
    AIAnalysisStatusOut,
    AISuggestionsResponse,
    AnthropicKeyStatusResponse,
    ApplyRequest,
    ApplyStatusOut,
    CandidatesResponse,
    CandidatesWarmStatusOut,
    CategoryExplanationResponse,
    DiagnosticsResponse,
    DuplicateClusterReviewResponse,
    FirstRunStatusResponse,
    FixedDrivesResponse,
    FullDriveScanConfirmIntentResponse,
    FullDriveScanRequest,
    ModeStatusResponse,
    OneClickCleanSummaryResponse,
    PowerModeRequest,
    QuarantineListResponse,
    RecoveryStatusResponse,
    RestoreStatusOut,
    ScanRequest,
    ScanStatusOut,
    SetAnthropicKeyRequest,
    SettingsResponse,
    SuggestedScanRootsResponse,
    SummaryResponse,
    TestAnthropicKeyRequest,
    TestAnthropicKeyResponse,
    TreemapResponse,
    UpdateCategorySettingRequest,
    UpdateCheckResponse,
)
from reclaim.api.state import (
    AIAnalysisStatus,
    ApplyStatus,
    AppState,
    CandidatesWarmStatus,
    RestoreStatus,
    ScanStatus,
)
from reclaim.drives import NoFixedDrivesFoundError
from reclaim.executor import (
    BatchNotFoundError,
    DirectDeleteRestoreImpossibleError,
    RecycleBinRestoreUnsupportedError,
    RestoreIntegrityError,
    SafeModeViolationError,
)
from reclaim.mode import ModeSwitchDeniedError
from reclaim.preflight import check_within_allowed_scope

router = APIRouter(prefix="/api")
logger = structlog.get_logger(__name__)


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

    # AO1 (2026-08-23 audit): this route accepted ANY caller-supplied path with zero
    # restriction -- not even the weak CSRF-only check /full-drive used to have -- found while
    # sweeping for siblings of that bug. A within-home path (the overwhelming common case: the
    # manual-scan form's quick-root shortcuts, and any typed path a real user's own profile
    # contains) needs no token, same as always. A path outside home needs the same single-use,
    # session-minted confirmation /full-drive now requires -- see
    # AppState.scan_outside_home_confirmation_tokens's docstring.
    within_home = check_within_allowed_scope(root, allowed_roots=[Path.home()])
    token_present = False
    if not within_home:
        token_valid = _consume_scan_confirmation_token(state, payload.token)
        if not token_valid:
            logger.info(
                "api.scan_denied", reason="missing_or_invalid_confirmation_token", root=str(root)
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{root} is outside your home directory -- call "
                    "POST /api/scan/full-drive/confirm-intent first and pass its token here"
                ),
            )
        token_present = True

    started_at = time.time()
    with state.lock:
        if state.scan_status.status == "running":
            raise HTTPException(
                status_code=409,
                detail=f"a scan is already running for {state.scan_status.root}",
            )
        # Scan cancellation: cleared HERE (not inside `run_scan` itself) -- see
        # `AppState.cancel_scan_event`'s docstring for why this avoids a real race against an
        # immediate `POST /api/scan/cancel`.
        state.cancel_scan_event.clear()
        state.scan_status = ScanStatus(
            status="running",
            root=root,
            started_at=started_at,
            phase="estimating",
            current_drive=root.as_posix(),
            drives_total=1,
            drives_done=0,
        )
        status_snapshot = state.scan_status

    # AN4 (2026-08-23 audit): no scan-start route logged anything before this -- found only
    # after a full-drive scan ran against a real account mid-session with no way to reconstruct
    # afterward which endpoint/params actually initiated it. Every scan-start route now logs
    # root + origin + confirmation-token presence unconditionally, regardless of outcome.
    logger.info(
        "api.scan_initiated", root=str(root), origin="POST /api/scan", token_present=token_present
    )
    background_tasks.add_task(service.run_scan, state, [root], started_at)
    return service.to_scan_status_out(status_snapshot)


@router.get("/scan/status", response_model=ScanStatusOut)
def scan_status(request: Request) -> ScanStatusOut:
    state = get_state(request)
    with state.lock:
        return service.to_scan_status_out(state.scan_status)


@router.post("/scan/cancel", response_model=ScanStatusOut)
def cancel_scan(request: Request) -> ScanStatusOut:
    """Requests a cooperative stop of whatever scan is currently running (single-path or
    full-drive) -- `service.run_scan` observes `state.cancel_scan_event` and stops at the next
    safe point (a batch boundary; see `scanner.scan_tree`'s own `cancel_event` docstring),
    finishing with `scan_status.status="cancelled"` and whatever partial results were already
    durably written, never `"failed"` (a user-requested stop is not an error).

    A no-op, not an error, when nothing is running -- mirrors this API's other idempotent
    "nothing to do" actions rather than inventing a new 409 case for a call that's inherently
    safe to make speculatively (e.g. a UI racing its own poll loop)."""
    state = get_state(request)
    with state.lock:
        if state.scan_status.status == "running":
            state.cancel_scan_event.set()
        return service.to_scan_status_out(state.scan_status)


@router.get("/scan/suggested-roots", response_model=SuggestedScanRootsResponse)
def scan_suggested_roots() -> SuggestedScanRootsResponse:
    return service.suggested_scan_roots()


@router.get("/scan/fixed-drives", response_model=FixedDrivesResponse)
def scan_fixed_drives() -> FixedDrivesResponse:
    """SIMPLE mode's explicit "scan the whole drive" opt-in (P0 fix, 2026-08-22 -- see
    `service.user_scan_roots`'s docstring: no longer the default) shows what's about to be
    scanned before the user commits (full-drive-scan-eta) -- lets the frontend render the drive
    list without its own drive-enumeration logic."""
    try:
        return service.fixed_drives()
    except NoFixedDrivesFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/scan/my-files", response_model=ScanStatusOut, status_code=202)
def start_my_files_scan(background_tasks: BackgroundTasks, request: Request) -> ScanStatusOut:
    """P0 fix (2026-08-22 real-disk finding, see `service.user_scan_roots`'s docstring for the
    full incident): SIMPLE mode's DEFAULT "Clean My Computer" action -- scans only the invoking
    user's own profile (`Path.home()`), never a whole fixed-drive volume. Same background-task +
    single-flight + polling shape as `POST /api/scan`/`POST /api/scan/full-drive` (indeed the
    same background task, `service.run_scan`, just with `roots=service.user_scan_roots()`),
    reusing `GET /api/scan/status` for progress/ETA polling rather than a second status
    endpoint."""
    state = get_state(request)
    roots = service.user_scan_roots()
    root = roots[0]
    if not root.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"user profile directory not found or inaccessible: {root}",
        )

    started_at = time.time()
    with state.lock:
        if state.scan_status.status == "running":
            raise HTTPException(
                status_code=409,
                detail=f"a scan is already running for {state.scan_status.root}",
            )
        # Scan cancellation: see the matching comment in `start_scan` above -- same race
        # avoided the same way.
        state.cancel_scan_event.clear()
        state.scan_status = ScanStatus(
            status="running",
            root=root,
            started_at=started_at,
            phase="estimating",
            current_drive=root.as_posix(),
            drives_total=len(roots),
            drives_done=0,
        )
        status_snapshot = state.scan_status

    logger.info(
        "api.scan_initiated", root=str(root), origin="POST /api/scan/my-files", token_present=False
    )
    background_tasks.add_task(service.run_scan, state, roots, started_at)
    return service.to_scan_status_out(status_snapshot)


# Item-7 fix (2026-08-23 audit): a real click-to-scan round trip (dialog confirm -> this token
# used) is milliseconds; 60s is generous headroom for that real interaction while closing the
# actual gap -- a token minted but never immediately consumed used to stay valid indefinitely,
# so a suspended/delayed fetch (or a token leaked/logged somewhere) could fire a scan outside
# home arbitrarily later, in a process that could stay alive for hours or days.
_SCAN_CONFIRMATION_TOKEN_TTL_SECONDS = 60.0


def _prune_expired_scan_confirmation_tokens(state: AppState, *, now: float) -> None:
    """Called with `state.lock` already held. Bounds the token dict's size over a long-lived
    process -- without this, a minted-but-never-consumed token would sit past its own TTL
    forever (rejected correctly by `_consume_scan_confirmation_token` either way, but never
    actually removed)."""
    expired = [
        token
        for token, minted_at in state.scan_outside_home_confirmation_tokens.items()
        if (now - minted_at) > _SCAN_CONFIRMATION_TOKEN_TTL_SECONDS
    ]
    for token in expired:
        del state.scan_outside_home_confirmation_tokens[token]


def _consume_scan_confirmation_token(state: AppState, token: str | None) -> bool:
    """True iff `token` was minted and is still within its TTL -- single-use either way: an
    expired token is removed here too, not left for the next prune pass to find."""
    if token is None:
        return False
    now = time.time()
    with state.lock:
        minted_at = state.scan_outside_home_confirmation_tokens.pop(token, None)
    if minted_at is None:
        return False
    return (now - minted_at) <= _SCAN_CONFIRMATION_TOKEN_TTL_SECONDS


@router.post("/scan/full-drive/confirm-intent", response_model=FullDriveScanConfirmIntentResponse)
def confirm_full_drive_scan_intent(request: Request) -> FullDriveScanConfirmIntentResponse:
    """AN1 (2026-08-23 audit): mints a fresh, single-use token proving the caller reached this
    endpoint in this same server process -- called by app.js's full-drive confirm dialog's
    "Yes, scan everything" button, and (AO1, same day) also by the manual-scan confirm dialog
    right before a `POST /api/scan` call whose path might resolve outside home (harmless to mint
    even when it turns out not to be needed -- the token is simply never consumed). Both
    `POST /api/scan/full-drive` and `POST /api/scan` (for an outside-home root) now require and
    consume one of these; the general CSRF token alone (valid for the whole process lifetime,
    reusable) is not sufficient on its own to start a scan outside the user's home. Closes the gap
    found when a full-drive scan ran against a real account with no code path identified that
    should have been able to trigger it -- both confirmation dialogs were, until this fix,
    frontend-only affordances with no server-side enforcement behind either of them at all.

    Item-7 fix (2026-08-23, same audit): also records the mint time now, so
    `_consume_scan_confirmation_token` can reject a token that's still unused past
    `_SCAN_CONFIRMATION_TOKEN_TTL_SECONDS` -- see that function's and the `AppState` field's
    docstrings for why an unbounded-lifetime token was a real, disclosed gap."""
    state = get_state(request)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with state.lock:
        _prune_expired_scan_confirmation_tokens(state, now=now)
        state.scan_outside_home_confirmation_tokens[token] = now
    return FullDriveScanConfirmIntentResponse(token=token)


@router.post("/scan/full-drive", response_model=ScanStatusOut, status_code=202)
def start_full_drive_scan(
    payload: FullDriveScanRequest, background_tasks: BackgroundTasks, request: Request
) -> ScanStatusOut:
    """Whole-drive scan (full-drive-scan-eta) -- every locally-attached fixed drive, from its
    volume root. P0 fix (2026-08-22, see `service.user_scan_roots`'s docstring): this is NO
    LONGER SIMPLE mode's default action -- `POST /api/scan/my-files` (the invoking user's own
    profile only) is. This endpoint remains available as a deliberate, separately-surfaced
    opt-in for a user who genuinely wants a whole-drive scan (the SIMPLE-mode UI gates it behind
    its own explicit confirmation dialog warning that a volume-root scan can reach other users'
    files if the filesystem's ACLs happen to allow it -- see `app.js`'s
    `openFullDriveConfirmDialog`). Same background-task + single-flight + polling shape as
    `POST /api/scan` (indeed the same background task, `service.run_scan`, just with
    `roots=service.fixed_drive_roots()` instead of a single user-supplied path), reusing
    `GET /api/scan/status` for progress/ETA polling rather than a second status endpoint.

    AN1 (2026-08-23 audit): requires a single-use `token` minted by
    `POST /api/scan/full-drive/confirm-intent` -- the general CSRF token alone (valid for this
    entire process's lifetime, reusable across any number of requests) used to be the ONLY
    server-side check here, meaning the confirmation dialog's "click to proceed" was purely a
    frontend affordance with nothing enforcing it actually happened. Found after an unexplained
    full-drive scan ran against a real account with no code path identified that should have been
    able to reach this endpoint."""
    state = get_state(request)
    token_valid = _consume_scan_confirmation_token(state, payload.token)
    if not token_valid:
        logger.info("api.full_drive_scan_denied", reason="missing_or_invalid_confirmation_token")
        raise HTTPException(
            status_code=403,
            detail=(
                "missing or invalid full-drive-scan confirmation token -- call "
                "POST /api/scan/full-drive/confirm-intent first and pass its token here"
            ),
        )

    try:
        roots = service.fixed_drive_roots()
    except NoFixedDrivesFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    started_at = time.time()
    with state.lock:
        if state.scan_status.status == "running":
            raise HTTPException(
                status_code=409,
                detail=f"a scan is already running for {state.scan_status.root}",
            )
        # Scan cancellation: see the matching comment in `start_scan` above -- same race
        # avoided the same way.
        state.cancel_scan_event.clear()
        state.scan_status = ScanStatus(
            status="running",
            root=roots[0],
            started_at=started_at,
            phase="estimating",
            current_drive=roots[0].as_posix(),
            drives_total=len(roots),
            drives_done=0,
        )
        status_snapshot = state.scan_status

    logger.info(
        "api.scan_initiated",
        root=str(roots[0]),
        origin="POST /api/scan/full-drive",
        drives_total=len(roots),
        token_present=True,
    )
    background_tasks.add_task(service.run_scan, state, roots, started_at)
    return service.to_scan_status_out(status_snapshot)


@router.get("/candidates/warm-status", response_model=CandidatesWarmStatusOut)
def candidates_warm_status(request: Request) -> CandidatesWarmStatusOut:
    """AE3: poll target for `POST /api/candidates/warm` — see `CandidatesWarmStatus`'s own
    docstring for why this exists (a real, multi-minute cold-compute cost on a large index, with
    no feedback, driving a "Not Responding" server state before this fix)."""
    state = get_state(request)
    with state.lock:
        return service.to_candidates_warm_status_out(state.candidates_warm_status)


@router.post("/candidates/warm", response_model=CandidatesWarmStatusOut, status_code=202)
def start_candidates_warm(
    background_tasks: BackgroundTasks, request: Request
) -> CandidatesWarmStatusOut:
    """AE3: mirrors `POST /api/apply`'s background-task + polling shape exactly. A caller (the
    dashboard frontend) should check `GET /api/candidates/warm-status` first — if already
    `"ready"` for the current scan generation, calling `/api/summary`/`/api/treemap`/etc.
    directly is already fast and this endpoint is unnecessary; this exists for the COLD-cache
    case, so that cost is visible and non-blocking instead of hanging the request thread."""
    state = get_state(request)
    with state.lock:
        if state.candidates_warm_status.status == "computing":
            raise HTTPException(status_code=409, detail="a candidates warm-up is already running")
        state.candidates_warm_status = CandidatesWarmStatus(
            status="computing", scan_generation=state.scan_generation, started_at=time.time()
        )
        status_snapshot = state.candidates_warm_status

    background_tasks.add_task(service.run_candidates_warm, state)
    return service.to_candidates_warm_status_out(status_snapshot)


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
) -> ApplyStatusOut | JSONResponse:
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
    except service.CandidatesNotWarmError as exc:
        # Same single-flight courtesy POST /api/candidates/warm's own route already gives the
        # frontend: kick off a warm-up here too (if one isn't already running) so a caller that
        # didn't know to call that endpoint first still converges on a retry, instead of needing
        # its own separate warm-up trigger.
        #
        # Returns a JSONResponse directly (background attached to it) rather than `raise
        # HTTPException` -- BackgroundTasks registered via `background_tasks.add_task` are only
        # executed if attached to the Response object FastAPI actually returns; on an exception
        # path, FastAPI's own exception handler builds an independent Response for the
        # HTTPException, which never sees this function's `background_tasks` instance, so the
        # warm-up would silently never run. Confirmed live: with `raise HTTPException(...)`, the
        # courtesy warm-up never started (`warm-status` stayed "computing" indefinitely, not
        # merely slow) -- caught by this fix's own regression test before it shipped.
        response_background = None
        with state.lock:
            if state.candidates_warm_status.status != "computing":
                state.candidates_warm_status = CandidatesWarmStatus(
                    status="computing",
                    scan_generation=state.scan_generation,
                    started_at=time.time(),
                )
                background_tasks.add_task(service.run_candidates_warm, state)
                response_background = background_tasks
        return JSONResponse(
            status_code=409, content={"detail": str(exc)}, background=response_background
        )

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


# --- P0-2 fix (2026-08 audit): in-app category settings -----------------------------------------


@router.get("/settings/categories", response_model=SettingsResponse)
def settings_categories(request: Request) -> SettingsResponse:
    return service.settings_categories(get_state(request))


@router.post("/settings/categories/{category}", response_model=SettingsResponse)
def update_category_setting(
    category: str, payload: UpdateCategorySettingRequest, request: Request
) -> SettingsResponse:
    try:
        return service.update_category_setting(
            get_state(request), category, enabled=payload.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- Update check (opt-in; see PRIVACY.md's "Updates" section) ---------------------------------


@router.get("/update-check", response_model=UpdateCheckResponse)
def update_check_status(request: Request) -> UpdateCheckResponse:
    """Read-only, best-effort — see `service.check_for_update_status` and
    `reclaim.update_check.check_for_update` for why this can never raise, block, or make a
    network call unless the user has opted in via `config.toml`'s `[update_check] enabled`."""
    return service.check_for_update_status(get_state(request))


# --- G25: bug-report diagnostics ----------------------------------------------------------------


@router.get("/diagnostics", response_model=DiagnosticsResponse)
def diagnostics(request: Request) -> DiagnosticsResponse:
    """Backs the dashboard's "Copy diagnostics" button — paths, counts, and version/mode
    metadata only, never file content (see `DiagnosticsResponse`'s docstring and PRIVACY.md)."""
    return service.build_diagnostics(get_state(request))


# --- R2: Anthropic API key settings + per-category LLM explanations ----------------------------


@router.get("/settings/anthropic-key", response_model=AnthropicKeyStatusResponse)
def anthropic_key_status(request: Request) -> AnthropicKeyStatusResponse:
    """Whether a key is configured — never the key itself (see `AnthropicKeyStatusResponse`'s
    docstring)."""
    return service.anthropic_key_status(get_state(request))


@router.post("/settings/anthropic-key", response_model=AnthropicKeyStatusResponse)
def set_anthropic_key(
    payload: SetAnthropicKeyRequest, request: Request
) -> AnthropicKeyStatusResponse:
    return service.set_anthropic_key(get_state(request), payload)


@router.delete("/settings/anthropic-key", response_model=AnthropicKeyStatusResponse)
def delete_anthropic_key(request: Request) -> AnthropicKeyStatusResponse:
    """A no-op (never an error) when no key is configured — same idempotent-delete posture as
    `cancel_scan` above."""
    return service.delete_anthropic_key(get_state(request))


@router.post("/settings/anthropic-key/test", response_model=TestAnthropicKeyResponse)
def test_anthropic_key(
    payload: TestAnthropicKeyRequest, request: Request
) -> TestAnthropicKeyResponse:
    """Backs the Settings tab's "Test key" button — one cheap models-list call, never a
    completion, so testing a key before saving it costs nothing. Never raises: a network
    failure or a rejected key both come back as a typed `valid=False` response, never a 500."""
    return service.check_anthropic_key(get_state(request), payload)


@router.get("/ai/category-explanation/{category_group}", response_model=CategoryExplanationResponse)
def category_explanation(category_group: str, request: Request) -> CategoryExplanationResponse:
    """Per-category prose explanation (R2) — recommend-only, same as everything else under
    `reclaim.ai`: this can never influence a delete decision (see
    `reclaim.ai.category_explainer`'s module docstring). Degrades gracefully in every failure
    mode (no scan, no matching category, no key configured, an Anthropic API failure) — never a
    500, see `service.build_category_explanation`'s docstring."""
    return service.build_category_explanation(get_state(request), category_group)
