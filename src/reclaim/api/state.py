from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from reclaim.config import Config
from reclaim.safety import SafetyValidator

ScanStatusLiteral = Literal["idle", "running", "completed", "failed"]


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
    config: Config
    vault_dir: Path
    manifest_path: Path
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
