from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _warming_client import WarmingTestClient
from fastapi.testclient import TestClient

from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.api.state import AppState
from reclaim.config import Config
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.safety_env import RealProfileAccessError

# A background job whose body raises a BaseException that is NOT an Exception (the hermetic-test
# `RealProfileAccessError`) used to skip every `except Exception` handler: the job's status was
# left "running" (or "failed" with error=None) and the exception died in the worker thread. Each
# job below must now end "failed" with a non-empty error, release whatever gates a retry, still
# let the refusal propagate, and accept a follow-up request. Hermetic: tmp_path for every root,
# and the refusal is raised by a stand-in for the job's body, nothing real is reachable.

_HOST = "127.0.0.1"
_PORT = 8766


def _refuse(*_args: Any, **_kwargs: Any) -> Any:
    raise RealProfileAccessError("refusing to touch the pretend real profile")


class _Env:
    def __init__(self, client: TestClient, state: AppState, root: Path, photo: Path) -> None:
        self.client = client
        self.state = state
        self.root = root
        self.photo = photo


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "vacation.jpg"
    photo.write_bytes(b"fixture bytes")
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    state: AppState = app.state.reclaim
    client = WarmingTestClient(
        app,
        base_url=f"http://{_HOST}:{_PORT}",
        headers={security.CSRF_HEADER_NAME: state.csrf_token},
    )
    return _Env(client, state, root, photo)


def _scan(env: _Env) -> None:
    # TestClient runs BackgroundTasks inside the request, so the scan is finished on return.
    assert env.client.post("/api/scan", json={"path": str(env.root)}).status_code == 202
    assert env.client.get("/api/scan/status").json()["status"] == "completed"


def _assert_failed_with_message(status: Any, error: str | None) -> None:
    assert status == "failed"
    assert error, "a failed job must say why, never error=None"
    assert "RealProfileAccessError" in error


def test_scan_refusal_ends_failed_with_error_and_a_new_scan_is_accepted(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(service, "count_entries_fast", _refuse)
        env.state.scan_status = service.ScanStatus(status="running", started_at=time.time())
        with pytest.raises(RealProfileAccessError):
            service.run_scan(env.state, [env.root], time.time())
    _assert_failed_with_message(env.state.scan_status.status, env.state.scan_status.error)
    body = env.client.get("/api/scan/status").json()
    assert body["status"] == "failed" and body["error"]
    _scan(env)  # not wedged: a follow-up scan is accepted and completes


def test_apply_refusal_ends_failed_with_error_and_a_new_apply_is_accepted(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scan(env)
    selected, method, apply_flag = service.resolve_apply_selection(
        env.state, service.ApplyRequest(tier="both", paths=[env.photo.as_posix()], dry_run=True)
    )
    with monkeypatch.context() as patch:
        patch.setattr(service, "apply_batch", _refuse)
        env.state.apply_status = service.ApplyStatus(status="running", started_at=time.time())
        with pytest.raises(RealProfileAccessError):
            service.run_apply(env.state, selected, method, apply_flag, time.time())
    _assert_failed_with_message(env.state.apply_status.status, env.state.apply_status.error)
    body = env.client.get("/api/apply/status").json()
    assert body["status"] == "failed" and body["error"]
    follow_up = {"tier": "both", "paths": [env.photo.as_posix()], "dry_run": True}
    assert env.client.post("/api/apply", json=follow_up).status_code == 202
    assert env.client.get("/api/apply/status").json()["status"] == "completed"


def test_restore_refusal_ends_failed_with_error_and_a_new_restore_is_accepted(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scan(env)
    real = {"tier": "both", "paths": [env.photo.as_posix()], "method": "vault", "dry_run": False}
    assert env.client.post("/api/apply", json=real).status_code == 202
    batch_id = env.client.get("/api/apply/status").json()["result"]["batch_id"]
    with monkeypatch.context() as patch:
        patch.setattr(service, "restore_batch", _refuse)
        env.state.restore_status = service.RestoreStatus(status="running", started_at=time.time())
        with pytest.raises(RealProfileAccessError):
            service.run_restore(env.state, batch_id, time.time())
    _assert_failed_with_message(env.state.restore_status.status, env.state.restore_status.error)
    body = env.client.get("/api/restore/status").json()
    assert body["status"] == "failed" and body["error"]
    assert env.client.post(f"/api/restore/{batch_id}").status_code == 202
    assert env.client.get("/api/restore/status").json()["status"] == "completed"
    assert env.photo.exists()


def test_ai_analysis_refusal_ends_failed_with_error_and_a_new_run_is_not_blocked(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scan(env)
    with monkeypatch.context() as patch:
        patch.setattr(service.ai_orchestration, "run_ai_analysis", _refuse)
        env.state.ai_status = service.AIAnalysisStatus(
            status="running", scan_generation=env.state.scan_generation, started_at=time.time()
        )
        with pytest.raises(RealProfileAccessError):
            service.run_ai_analysis(env.state, env.state.scan_generation, time.time())
    _assert_failed_with_message(env.state.ai_status.status, env.state.ai_status.error)
    # 202 (started) with the [ai] extra, 200 (typed "unavailable") without it; never a 409.
    assert env.client.post("/api/ai/analyze").status_code in (200, 202)


class _InlineThread:
    """Runs the target in `start()` on the caller's thread, so a BaseException escaping the job
    reaches the test instead of dying in a worker thread's excepthook."""

    def __init__(self, target: Callable[[], None], **_kw: Any) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


def test_regenerable_refusal_ends_failed_with_error_and_releases_the_lock(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Threading:
        Thread = _InlineThread

        def __getattr__(self, name: str) -> Any:
            return getattr(threading, name)

    monkeypatch.setattr(service, "threading", _Threading())
    monkeypatch.setattr(service, "_regenerable_job", None)
    monkeypatch.setattr(service, "_execute_regenerable_clean", _refuse)

    with pytest.raises(RealProfileAccessError):
        service.start_regenerable_clean(env.state)

    status = service.get_regenerable_status()
    _assert_failed_with_message(status.status, status.error)
    assert service._regenerable_clean_lock.acquire(blocking=False), "lock must be released"
    service._regenerable_clean_lock.release()
    # A follow-up start is accepted (not RegenerableCleanBusyError) and ends failed the same way.
    with pytest.raises(RealProfileAccessError):
        service.start_regenerable_clean(env.state)
    assert service.get_regenerable_status().status == "failed"


def test_candidates_warm_refusal_ends_failed_and_a_new_warm_is_accepted(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already correct since #134's finished flag; pinned here so it stays matched to the rest."""
    _scan(env)
    outcome, _ = service.begin_candidates_warm(env.state, source="manual")
    assert outcome == "started"
    with monkeypatch.context() as patch:
        patch.setattr(service, "_cached_all_candidates", _refuse)
        with pytest.raises(RealProfileAccessError):
            service.run_candidates_warm(env.state)
    assert env.state.candidates_warm_status.status == "failed"
    assert env.state.candidates_warm_status.error
    outcome, _ = service.begin_candidates_warm(env.state, source="manual")
    assert outcome == "started"
