from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from reclaim.ai.models import AICluster
from reclaim.config import Config, apply_safe_mode_category_overrides
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.mode import DEFAULT_MODE_LOG_PATH, current_mode
from reclaim.models import Mode
from reclaim.safety import SafetyValidator

ScanStatusLiteral = Literal["idle", "running", "completed", "failed"]
AIAnalysisStatusLiteral = Literal["idle", "running", "completed", "failed"]


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
    mode_log_path: Path = field(default_factory=lambda: DEFAULT_MODE_LOG_PATH)
    first_run_state_path: Path = field(default_factory=lambda: DEFAULT_FIRST_RUN_STATE_PATH)
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
