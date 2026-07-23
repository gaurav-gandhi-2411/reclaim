from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from reclaim.api.routes import router
from reclaim.api.security import LocalOriginPolicy, generate_csrf_token, local_origin_violation
from reclaim.api.state import AppState
from reclaim.config import Config
from reclaim.first_run import DEFAULT_FIRST_RUN_STATE_PATH
from reclaim.mode import DEFAULT_MODE_LOG_PATH
from reclaim.safety import SafetyValidator

_PACKAGE_DIR = Path(__file__).parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"

_DEFAULT_VAULT_DIR = Path("data/quarantine")
_DEFAULT_MANIFEST_PATH = _DEFAULT_VAULT_DIR / "manifest.jsonl"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420

_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def create_app(
    *,
    db_path: Path,
    config: Config,
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
    mode_log_path: Path | None = None,
    first_run_state_path: Path | None = None,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
) -> FastAPI:
    """Builds one self-contained Reclaim dashboard app instance.

    `config` is a fully-built, RAW `Config` (exactly `reclaim.config.load_config`'s output — no
    safe-mode category override baked in), not a path — callers (the `reclaim serve` CLI
    command, or a test) are responsible for calling `load_config` themselves first, which keeps
    this factory pure and trivially testable with hand-built `Config` objects (no need to write
    a temp `config.toml` per test). The live mode (and, when SAFE, the category override) is
    resolved fresh on every request via `AppState.effective_config` — see its docstring for why
    `config` must stay raw here rather than pre-resolved once at startup.

    `host`/`port` must be the exact loopback address this process will actually be bound to
    (`cli.py::_run_serve` passes its already-`_loopback_host`-validated `args.host`/`args.port`
    straight through) — every `/api/*` request's `Host`/`Origin` headers are checked against
    this exact authority by the middleware registered below (see `reclaim.api.security`), so a
    mismatch here would silently make that DNS-rebinding guard check the wrong thing.

    State lives on `app.state.reclaim` (an `AppState`), never a module-level global — see
    `AppState`'s docstring for why that's the right call for this single-user, localhost tool.
    """
    app = FastAPI(title="Reclaim", version="0.1.0")
    # Created eagerly (not lazily inside a route) so every read-only endpoint (summary,
    # treemap, candidates) can open `ScanIndex(db_path)` even before the first scan has run —
    # `sqlite3.connect` fails outright if the parent directory doesn't exist yet.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.state.reclaim = AppState(
        db_path=db_path,
        config=config,
        vault_dir=vault_dir if vault_dir is not None else _DEFAULT_VAULT_DIR,
        manifest_path=manifest_path if manifest_path is not None else _DEFAULT_MANIFEST_PATH,
        safety=SafetyValidator(config),
        csrf_token=generate_csrf_token(),
        host=host,
        port=port,
        mode_log_path=mode_log_path if mode_log_path is not None else DEFAULT_MODE_LOG_PATH,
        first_run_state_path=(
            first_run_state_path
            if first_run_state_path is not None
            else DEFAULT_FIRST_RUN_STATE_PATH
        ),
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)

    policy = LocalOriginPolicy(host=host, port=port)

    @app.middleware("http")
    async def _local_origin_guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Scoped to /api — the DNS-rebinding/CSRF threat this guards against is "a remote page
        # reads or mutates real scan/quarantine data through the API", not "a remote page can
        # fetch the static HTML/JS/CSS shell", which carries no per-user data and is identical
        # for anyone who requests it.
        if request.url.path.startswith("/api"):
            violation = local_origin_violation(request, policy)
            if violation is not None:
                return JSONResponse(status_code=403, content={"detail": violation})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return _templates.TemplateResponse(
            request, "index.html", {"csrf_token": app.state.reclaim.csrf_token}
        )

    @app.get("/LICENSE", include_in_schema=False)
    def license_text() -> Response:
        # Repo-root LICENSE, read fresh on every request (this file never changes at runtime,
        # but there's no reason to cache it either) — the first-run screen's "License &
        # no-warranty terms" link. 404s gracefully rather than crashing if the file is missing
        # (e.g. a dev checkout mid-Stage-2, before the license lands).
        license_path = Path("LICENSE")
        if not license_path.exists():
            return JSONResponse(status_code=404, content={"detail": "LICENSE file not found"})
        return Response(content=license_path.read_text(encoding="utf-8"), media_type="text/plain")

    return app
