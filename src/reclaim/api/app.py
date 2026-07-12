from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from reclaim.api.routes import router
from reclaim.api.state import AppState
from reclaim.config import Config
from reclaim.safety import SafetyValidator

_PACKAGE_DIR = Path(__file__).parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"

_DEFAULT_VAULT_DIR = Path("data/quarantine")
_DEFAULT_MANIFEST_PATH = _DEFAULT_VAULT_DIR / "manifest.jsonl"

_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def create_app(
    *,
    db_path: Path,
    config: Config,
    vault_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> FastAPI:
    """Builds one self-contained Reclaim dashboard app instance.

    `config` is a fully-built `Config`, not a path — callers (the `reclaim serve` CLI command,
    or a test) are responsible for calling `reclaim.config.load_config` themselves first, which
    keeps this factory pure and trivially testable with hand-built `Config` objects (no need to
    write a temp `config.toml` per test).

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
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return _templates.TemplateResponse(request, "index.html", {})

    return app
