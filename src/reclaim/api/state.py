from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from reclaim.ai.category_explainer import DEFAULT_CACHE_DIR as DEFAULT_AI_EXPLANATION_CACHE_DIR
from reclaim.ai.models import AICluster
from reclaim.anthropic_key_store import DEFAULT_KEY_PATH as DEFAULT_ANTHROPIC_KEY_PATH
from reclaim.config import Config, apply_safe_mode_category_overrides
from reclaim.executor import BatchApplyReport, RestoreReport
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.logging_config import DEFAULT_LOG_PATH
from reclaim.mode import DEFAULT_MODE_LOG_PATH, current_mode
from reclaim.models import Candidate, Mode
from reclaim.safety import SafetyValidator

ScanStatusLiteral = Literal["idle", "running", "completed", "failed", "cancelled"]
AIAnalysisStatusLiteral = Literal["idle", "running", "completed", "failed"]
ApplyStatusLiteral = Literal["idle", "running", "completed", "failed"]
RestoreStatusLiteral = Literal["idle", "running", "completed", "failed"]
CandidatesWarmStatusLiteral = Literal["idle", "computing", "ready", "failed"]
# full-drive-scan-eta: which of `api.service.run_scan`'s two phases (per root) is currently
# active -- "estimating" while `scanner.count_entries_fast` is deriving `entries_estimated_total`,
# "scanning" while the real `scanner.scan_tree` walk is running, "done" once every root has
# completed. `None` before a scan has ever started this process session, same convention as every
# other optional `ScanStatus` field.
ScanPhaseLiteral = Literal["estimating", "scanning", "done"]


@dataclass(slots=True)
class ScanStatus:
    """Snapshot of the most recent (or in-progress) scan for this process."""

    status: ScanStatusLiteral = "idle"
    root: Path | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    dirs_visited: int | None = None
    entries_total: int | None = None
    files_written: int | None = None
    files_unchanged: int | None = None
    files_pruned: int | None = None
    elapsed_seconds: float | None = None
    # D12: real accounting of entries the scan could not stat/list (permission error, genuine I/O
    # fault) -- see `reclaim.scanner.SkippedPath`. `None` until a scan has actually completed,
    # same convention as every other field above.
    skipped_unreadable_count: int | None = None
    skipped_unreadable_paths: tuple[str, ...] | None = None
    # full-drive-scan-eta: live progress/ETA for the CURRENT root being scanned, plus how far
    # through the (possibly multi-drive) `roots` list the scan is overall. Populated for both the
    # existing single-path scan (`drives_total=1`) and the new full-drive scan
    # (`drives_total=len(list_fixed_drives())`) -- see `api.service.run_scan`, the one
    # orchestration path underneath both.
    phase: ScanPhaseLiteral | None = None
    entries_processed: int | None = None
    entries_estimated_total: int | None = None
    eta_seconds: float | None = None
    current_drive: str | None = None
    drives_total: int | None = None
    drives_done: int | None = None


@dataclass(slots=True)
class AIAnalysisStatus:
    """Snapshot of the most recent (or in-progress) AI analysis pass for this process --
    mirrors `ScanStatus`'s exact shape/locking pattern (ADR-0025). `scan_generation` records
    which `AppState.scan_generation` this analysis covered, so a caller can tell a completed
    analysis is stale (a newer scan has since completed) without forcing a recompute on every
    page load -- see `AppState.scan_generation`'s docstring."""

    status: AIAnalysisStatusLiteral = "idle"
    scan_generation: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    tracks_run: list[str] = field(default_factory=list)
    tracks_skipped: list[tuple[str, str]] = field(default_factory=list)  # (track, reason) pairs
    files_considered: dict[str, int] = field(default_factory=dict)
    files_capped: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ApplyStatus:
    """Snapshot of the most recent (or in-progress) `POST /api/apply` background task for this
    process (fix/apply-progress-feedback) — mirrors `ScanStatus`'s exact shape/locking
    convention. `items_processed`/`items_total`/`current_category` are updated at the same
    interval-gated cadence `executor.apply_batch`'s own progress heartbeat uses (see
    `executor.ProgressCallback`), so `GET /api/apply/status` polling sees real incremental
    progress during a long-running vault apply (ADR-0026's measured per-item fsync cost), not
    just idle/running/completed. `result` holds the real `BatchApplyReport` once `status`
    reaches `"completed"` — converted to the API's `ApplyResponse` shape only at the read
    boundary (`api.service.to_apply_status_out`), same convention every other `Out` schema in
    this module already follows."""

    status: ApplyStatusLiteral = "idle"
    items_processed: int | None = None
    items_total: int | None = None
    current_category: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: BatchApplyReport | None = None


@dataclass(slots=True)
class RestoreStatus:
    """Snapshot of the most recent (or in-progress) `POST /api/restore/{batch_id}` background
    task for this process (fix/apply-progress-feedback) — mirrors `ApplyStatus` exactly, one
    level down: restoring only ever moves a batch's `vault`-method entries (see `executor.
    resolve_restorable_entries`), so `items_total` here is that count, not the whole batch's
    item count."""

    status: RestoreStatusLiteral = "idle"
    items_processed: int | None = None
    items_total: int | None = None
    current_category: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: RestoreReport | None = None


@dataclass(slots=True)
class CandidatesWarmStatus:
    """AE1's `_all_candidates` cold-compute status (P0 finding, this session): the shared
    candidate-generation pass every one of `build_summary`/`build_treemap`/`list_candidates`/
    `build_one_click_summary`/apply-selection's blanket path draws from (`api.service.
    _cached_all_candidates`) is real, unbounded work on a large persisted index -- live-observed
    on a real profile with an 82.1 GB / 13,998-candidate index: a cold-cache `/api/summary` call
    ran for MINUTES with zero feedback, driving the server process to a sustained "Not
    Responding" state. This status exists so a caller can check readiness and trigger a
    background warm-up BEFORE calling `/api/summary` directly, instead of blocking the request
    thread (and the caller's UI) for the full duration with nothing to show. Mirrors
    `AIAnalysisStatus`'s exact shape/staleness-key convention: `scan_generation` records which
    `AppState.scan_generation` this warm-up covered, so a caller can tell a `"ready"` status is
    stale (a newer scan has since completed) without forcing a recompute on every check.

    Deliberately does NOT expose fine-grained item-level progress (a percentage, an item
    counter) -- the underlying `generate_candidates`/`generate_duplicate_candidates`/hardlink-
    dedup-clustering pass has no cheap, already-instrumented way to report that within this
    fix's scope (AE3 was explicit: "do not fix the algorithm now"). `elapsed_seconds` (computed
    at read time from `started_at`) is the only progress signal available -- an honest "still
    working, N seconds so far" rather than a fabricated percentage.
    """

    status: CandidatesWarmStatusLiteral = "idle"
    scan_generation: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


@dataclass(slots=True)
class AppState:
    """Single-process, in-memory application state.

    Reclaim is an explicitly single-user, localhost-only tool (spec: "FastAPI backend +
    single-page local web dashboard (localhost only)") — there is never more than one browser
    tab meaningfully driving one server process, so a plain in-memory dataclass guarded by a
    `threading.Lock` is an acceptable simplification here; it would NOT be for a multi-tenant
    service, which would need a durable, per-job status store instead of a process-local dict.

    Lives on `app.state.reclaim` (one instance per `create_app()` call), never a module-level
    global, so each test gets its own isolated instance and multiple `TestClient`s in the same
    pytest process never leak scan state into each other.
    """

    db_path: Path
    # RAW config — exactly what `config.load_config` parsed from config.toml (or built-in
    # defaults), with NO safe-mode category override applied. Kept raw deliberately: the live
    # mode can change mid-session via POST /api/mode/power|safe, and there is no way to
    # "un-override" an already-overridden category back to what config.toml actually requested
    # — see `effective_config`, which re-derives the mode-aware view fresh on every access
    # instead of baking a startup-time snapshot into this field.
    config: Config
    vault_dir: Path
    manifest_path: Path
    # Depends only on `config.safety` (protected roots/extensions/etc.), which is never
    # mode-dependent — safe to build once at startup, unlike `effective_config` below.
    safety: SafetyValidator
    # Per-process CSRF token (rule: local-API hardening) and the loopback host:port this
    # process is actually bound to, used by the Origin/Host DNS-rebinding guard — see
    # `reclaim.api.security`. Both required (no default) so a caller can never accidentally
    # construct an `AppState` without them and silently disable the guard.
    csrf_token: str
    host: str
    port: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    scan_status: ScanStatus = field(default_factory=ScanStatus)
    # Scan cancellation: the one cooperative-cancellation signal `service.run_scan` threads
    # through `scanner.count_entries_fast`/`scan_tree`. Cleared by the route handlers
    # (`POST /api/scan`, `POST /api/scan/full-drive`) synchronously, under the same `lock`
    # acquisition that flips `scan_status` to "running" -- BEFORE the background task is ever
    # scheduled -- rather than by `run_scan` itself at the top of its own body: Starlette only
    # runs a `BackgroundTasks` callable AFTER the HTTP response has already been sent to the
    # client, so a client that calls `POST /api/scan/cancel` immediately after receiving that
    # response could otherwise race an in-`run_scan` `.clear()` and have its cancel request
    # silently wiped. Set by `POST /api/scan/cancel`. A plain `threading.Event` (not a
    # `Lock`-guarded bool) since setting/checking it must never block a poller or the walk itself.
    cancel_scan_event: threading.Event = field(default_factory=threading.Event)
    # AN1 (2026-08-23 audit): a full-drive scan's confirmation dialog was, until this fix,
    # enforced ONLY in the frontend -- POST /api/scan/full-drive accepted any request carrying a
    # valid (session-lifetime, reusable) CSRF token, with no server-side proof the user had
    # actually seen or clicked through the dialog. Found after an unexplained full-drive scan ran
    # against a real account mid-session with no code path identified that should have been able
    # to trigger it. `POST /api/scan/full-drive/confirm-intent` mints a token into this set
    # (guarded by `lock`, same as `scan_status`) ONLY when called -- the frontend calls it
    # exactly when the user clicks the dialog's confirm button, never before.
    #
    # AO1 (2026-08-23 audit, same day): widened to cover `POST /api/scan` too, not just
    # `/full-drive` -- the plain scan endpoint accepted ANY caller-supplied path, including one
    # outside the user's home, with zero restriction at all (not even the weak CSRF-only check
    # `/full-drive` used to have). Both routes now require and consume (single-use) a token from
    # this same set whenever the resolved scan root is outside `Path.home()`; a within-home scan
    # (the common case for both routes) needs no token at all. The general CSRF token alone is
    # not sufficient for either route once the requested root leaves the user's own profile.
    #
    # Item-7 fix (2026-08-23, same audit): a token minted here used to live forever until
    # consumed -- no timestamp was ever recorded, so a minted-but-never-used token stayed valid
    # indefinitely for the rest of the process's lifetime. This is the residue of an unexplained
    # full-drive scan that occurred during a prior session and was never conclusively attributed;
    # closing this gap does not depend on ever identifying that incident's exact trigger. Maps
    # token -> mint time now (`routes._SCAN_CONFIRMATION_TOKEN_TTL_SECONDS`, currently 60s -- a
    # real click-to-scan round trip is milliseconds) so a token past its TTL is rejected exactly
    # like one that was never minted, whether or not it was ever consumed. Still single-use
    # (removed on any check, valid or not) and still unbounded in count with no cap needed --
    # `routes._prune_expired_scan_confirmation_tokens` sweeps stale entries on every mint, so an
    # unconsumed token no longer accumulates past its own TTL either.
    scan_outside_home_confirmation_tokens: dict[str, float] = field(default_factory=dict)
    # P0-2 fix (2026-08 audit): the exact path `config` above was loaded from (or would be
    # created at, if it didn't exist) — needed so `POST /api/settings/categories/{group}` can
    # persist a toggle to the same on-disk file the CLI/next server start will read, not just
    # mutate the in-memory `config` field for the life of this process. `create_app`/`_run_serve`
    # always set this to the real `--config` value; the default here only covers a caller (a
    # test, a future embedder) that never passed one explicitly.
    config_path: Path = field(default_factory=lambda: Path("config.toml"))
    mode_log_path: Path = field(default_factory=lambda: DEFAULT_MODE_LOG_PATH)
    first_run_state_path: Path = field(default_factory=lambda: DEFAULT_FIRST_RUN_STATE_PATH)
    # G25: the persistent rotating log file this process's `configure_logging` call actually
    # writes to — `create_app` always sets this to match, so `GET /api/diagnostics` reads the
    # tail of the real file, never a stale default that drifted from a `--log-path` override.
    log_path: Path = field(default_factory=lambda: DEFAULT_LOG_PATH)
    # ADR-0025: incremented once per successfully COMPLETED scan (`service.run_scan`'s success
    # branch) -- the AI analysis cache below is keyed to this value so a caller can tell a
    # cached analysis is stale (a newer scan completed since) without forcing a recompute on
    # every page load.
    scan_generation: int = 0
    ai_status: AIAnalysisStatus = field(default_factory=AIAnalysisStatus)
    # The last COMPLETED analysis's clusters -- valid only when `ai_status.scan_generation ==
    # scan_generation` (see above). In-memory only, like every other piece of this process's
    # session state (ADR-0025 decision 2): lost on restart, re-computed with one click.
    ai_clusters: list[AICluster] = field(default_factory=list)
    # fix/apply-progress-feedback: `POST /api/apply`/`POST /api/restore/{batch_id}` background
    # tasks -- one in-flight apply and one in-flight restore per process at a time (single-user,
    # single-browser-tab tool; see this class's own docstring), same single-flight posture
    # `scan_status`/`ai_status` already have.
    apply_status: ApplyStatus = field(default_factory=ApplyStatus)
    restore_status: RestoreStatus = field(default_factory=RestoreStatus)
    # perf/dedup-cache (docs/AUDIT-2026-08.md P0-3): `api.service._all_candidates` re-runs the
    # full detector + exact-duplicate-cluster pass (a whole-index BLAKE3 hash of every
    # size-duplicate candidate) on every call -- before this cache, 5 independent call sites in
    # `service.py` each triggered that pass independently, live-reproduced as 60+ second page
    # loads and two concurrent hash passes firing 12 seconds apart for a single page load. Cached
    # per `scan_generation` above (same staleness key ADR-0025 already uses for `ai_clusters`) --
    # a fresh COMPLETED scan invalidates it. `candidates_cache_lock` is a DEDICATED lock (not
    # `lock` above, which guards small/fast status-dataclass reads elsewhere) held across the
    # entire compute-or-fetch critical section in `service._cached_all_candidates` -- a second
    # caller racing the first for the same generation blocks on this lock rather than starting
    # its own redundant hash pass, and sees the first caller's now-cached result once it
    # acquires the lock. In-memory only, like every other piece of this process's session state
    # (ADR-0025 decision 2): lost on restart, rebuilt on the next call.
    candidates_cache: list[Candidate] | None = None
    candidates_cache_generation: int | None = None
    candidates_cache_lock: threading.Lock = field(default_factory=threading.Lock)
    # AE1: single-flight background-warm status for the cache above -- see
    # `CandidatesWarmStatus`'s own docstring for the real cold-start cost this exists to make
    # non-blocking/visible instead of silent.
    candidates_warm_status: CandidatesWarmStatus = field(default_factory=CandidatesWarmStatus)
    # R2: where the DPAPI-encrypted Anthropic API key blob lives, and where per-category
    # explanation cache entries are written -- overridable the same way every other path on
    # this dataclass is (test isolation; see `create_app`'s matching constructor parameters).
    anthropic_key_path: Path = field(default_factory=lambda: DEFAULT_ANTHROPIC_KEY_PATH)
    ai_explanation_cache_dir: Path = field(default_factory=lambda: DEFAULT_AI_EXPLANATION_CACHE_DIR)
    # R7 concurrency fix (docs/AUDIT-2026-08.md, adversarial re-verification of PR #39): a
    # single-flight guard for `reclaim.mcp.server.delete`, same "check-and-set under `lock`,
    # refuse if already running" idiom `POST /api/apply` uses for `apply_status` above --
    # deliberately its own field, NOT a reuse of `apply_status`, so an MCP-initiated delete never
    # overwrites the HTTP dashboard's own `GET /api/apply/status` view with a result it didn't
    # trigger. `reclaim.mcp.server.delete` is synchronous (an MCP tool call is one request/
    # response, not a background task), so unlike `apply_status` this is a plain bool, not a
    # richer progress-tracking dataclass: there is nothing to poll mid-flight, only "is one
    # already running" to check before starting another. Without this, two concurrent `delete()`
    # calls for the identical selection both pass hash validation and both reach `apply_batch`
    # -- live-reproduced: the second one fails at the file level (the first already moved it)
    # but the tool call itself still returns `isError=False`, a misleading "the call succeeded"
    # shape on a call that deleted nothing (`files_succeeded=0` is the only honest signal, easy
    # for a caller/agent to miss). Guarding here refuses the second call immediately, before it
    # ever re-derives candidates or computes a hash, with an unambiguous typed error instead.
    mcp_delete_in_progress: bool = False

    @property
    def live_mode(self) -> Mode:
        """Re-read from the mode-change log on every access, never cached — the mode can
        change mid-session via the API, and every request must see the CURRENT mode, not a
        snapshot from whenever this `AppState` was constructed."""
        return current_mode(self.mode_log_path)

    @property
    def effective_config(self) -> Config:
        """`self.config` (raw) with the live mode resolved and, when SAFE, its dangerous
        categories forced off — computed fresh on every access (see `live_mode`) rather than
        once at startup. Every request that generates candidates, applies, or purges must use
        this, never `self.config` directly."""
        live_mode = self.live_mode
        categories = (
            apply_safe_mode_category_overrides(self.config.categories)
            if live_mode == Mode.SAFE
            else self.config.categories
        )
        return self.config.model_copy(update={"mode": live_mode, "categories": categories})
