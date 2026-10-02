"""fix/warm-check-all-views: EVERY request-path reader of the candidate cache (summary, treemap,
candidates list, one-click summary) refuses a cold or stale cache with the typed 409
`candidates_not_warm` instead of silently recomputing it in the request thread, and starts the
single-flight courtesy warm-up -- the same flow `POST /api/apply`'s blanket path already had.

Cold = scanned, never warmed. Stale = warmed, then the scan / mode / config / scope changed
(`service.candidates_cache_stale_reason`'s four causes). `generate_candidates` is made to blow up
in the request path, and the warm-up job itself is replaced by a recording stub, so a passing test
proves no detector ran in the request thread.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.api.state import AppState, ScanStatus
from reclaim.config import (
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    DuplicatesConfig,
    LargeLogsConfig,
    SafetyConfig,
)
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NOW = 1_700_000_000.0
_HOST = "127.0.0.1"
_PORT = 8420
_ENDPOINTS = (
    "/api/summary",
    "/api/treemap",
    "/api/candidates?tier=both",
    "/api/clean/one-click-summary",
)


def _config(root: Path) -> Config:
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30),
            large_logs=LargeLogsConfig(enabled=True, min_size_bytes=1_000, stale_days=30),
            duplicates=DuplicatesConfig(enabled=False, min_reclaim_bytes=0),
        ),
    )


def _make_client(tmp_path: Path, root: Path) -> TestClient:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(root),
        config_path=tmp_path / "config.toml",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    token: str = app.state.reclaim.csrf_token
    return TestClient(
        app, base_url=f"http://{_HOST}:{_PORT}", headers={security.CSRF_HEADER_NAME: token}
    )


def _scan(client: TestClient, root: Path) -> None:
    assert client.post("/api/scan", json={"path": str(root)}).status_code == 202
    assert client.get("/api/scan/status").json()["status"] == "completed"


def _warm(client: TestClient) -> None:
    assert client.post("/api/candidates/warm").status_code == 202
    assert client.get("/api/candidates/warm-status").json()["status"] == "ready"


def _tree(root: Path) -> None:
    log = root / "Logs" / "old.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"a" * 2_000)
    os.utime(log, (_NOW - 45 * 86400, _NOW - 45 * 86400))
    pkg = root / "Project" / "node_modules" / "p" / "index.js"
    pkg.parent.mkdir(parents=True)
    pkg.write_bytes(b"x" * 5_000)
    (root / "Project" / "package.json").write_text('{"name": "d"}')


class _Env:
    def __init__(self, client: TestClient, state: AppState, root: Path) -> None:
        self.client = client
        self.state = state
        self.root = root
        self.warm_starts = 0


@pytest.fixture
def scanned(tmp_path: Path) -> _Env:
    root = tmp_path / "tree"
    _tree(root)
    client = _make_client(tmp_path, root)
    _scan(client, root)
    return _Env(client, client.app.state.reclaim, root)


@pytest.fixture
def no_request_thread_detection(scanned: _Env, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """Armed AFTER the real scan/warm setup of each test: any detector run in the request path
    fails loudly, and the warm-up job is a recording stub (the 409 route kicks it off)."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a detector ran in the request thread")

    def _stub_warm(_state: AppState) -> None:
        scanned.warm_starts += 1

    monkeypatch.setattr(service, "run_candidates_warm", _stub_warm)
    monkeypatch.setattr(service, "generate_candidates", _boom)
    monkeypatch.setattr(service, "generate_duplicate_candidates", _boom)
    return scanned


def _break_scope(state: AppState) -> None:
    with state.lock:
        state.scan_status = ScanStatus()
        state.scan_status.root = Path("D:/elsewhere")
        state.scan_status.finished_at = time.time()


def _cause_mode(env: _Env) -> None:
    assert env.client.post("/api/mode/safe").status_code == 200


def _cause_config(env: _Env) -> None:
    response = env.client.post("/api/settings/categories/duplicates", json={"enabled": True})
    assert response.status_code == 200


def _cause_scan(env: _Env) -> None:
    _scan(env.client, env.root)


def _cause_scope(env: _Env) -> None:
    _break_scope(env.state)


_CAUSES = {
    "mode": _cause_mode,
    "config": _cause_config,
    "scan": _cause_scan,
    "scope": _cause_scope,
}


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_cold_cache_gives_409_not_a_blocking_compute(
    scanned: _Env, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    starts: list[int] = []
    monkeypatch.setattr(service, "run_candidates_warm", lambda _s: starts.append(1))
    monkeypatch.setattr(
        service,
        "generate_candidates",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("detector ran in request")),
    )
    response = scanned.client.get(endpoint)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "candidates_not_warm"
    assert body["stale_reason"] == "cold"
    assert "not warm" in body["detail"]
    assert starts == [1]  # the courtesy warm-up was started exactly once


@pytest.mark.parametrize("cause", sorted(_CAUSES))
@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_stale_cache_gives_409_naming_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str, cause: str
) -> None:
    root = tmp_path / "tree"
    _tree(root)
    client = _make_client(tmp_path, root)
    _scan(client, root)
    _warm(client)
    env = _Env(client, client.app.state.reclaim, root)
    assert client.get(endpoint).status_code == 200  # ready before the cause
    _CAUSES[cause](env)

    starts: list[int] = []
    monkeypatch.setattr(service, "run_candidates_warm", lambda _s: starts.append(1))
    monkeypatch.setattr(
        service,
        "generate_candidates",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("detector ran in request")),
    )
    response = client.get(endpoint)
    assert response.status_code == 409
    assert response.json()["code"] == "candidates_not_warm"
    assert response.json()["stale_reason"] == cause
    assert starts == [1]


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_ready_cache_serves_without_running_a_detector(
    scanned: _Env, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    _warm(scanned.client)
    monkeypatch.setattr(
        service,
        "generate_candidates",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("detector ran in request")),
    )
    response = scanned.client.get(endpoint)
    assert response.status_code == 200
    assert response.json()["has_scan"] is True


def test_second_read_while_a_warm_up_is_running_does_not_start_another(
    no_request_thread_detection: _Env,
) -> None:
    env = no_request_thread_detection
    assert env.client.get("/api/summary").status_code == 409
    assert env.client.get("/api/treemap").status_code == 409
    assert env.client.get("/api/clean/one-click-summary").status_code == 409
    # The stub never finishes, so status stays "computing" after the first read: single-flight.
    assert env.warm_starts == 1
    reason = env.client.get("/api/summary").json()["stale_reason"]
    assert reason in ("cold", "computing")


def test_no_scan_yet_is_not_a_409(tmp_path: Path) -> None:
    """An empty index has nothing to compute -- the existing has_scan=False answers stay."""
    client = _make_client(tmp_path, tmp_path / "tree")
    for endpoint in _ENDPOINTS:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        assert response.json()["has_scan"] is False


def test_service_functions_keep_a_blocking_opt_out_for_the_mcp_server(scanned: _Env) -> None:
    """`require_warm=False` is the MCP `list_candidates` tool's unchanged (blocking) behaviour."""
    response = service.list_candidates(
        scanned.state, tier="both", category_group=None, require_warm=False
    )
    assert response.has_scan is True
    with pytest.raises(service.CandidatesNotWarmError):
        scanned.state.candidates_cache = None
        scanned.state.candidates_cache_key = None
        service.list_candidates(scanned.state, tier="both", category_group=None)
