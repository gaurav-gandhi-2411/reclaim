from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.ai.models import AICluster, AIClusterMember, AITrack
from reclaim.api import ai_orchestration, security
from reclaim.api.app import create_app
from reclaim.api.state import AIAnalysisStatus
from reclaim.config import Config
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8420

# ADR-0025: endpoint-level coverage for POST /api/ai/analyze, GET /api/ai/status, and
# GET /api/ai/suggestions -- degraded mode, precondition checks, status transitions, caching/
# staleness, and the presentation-layer mapping. Orchestration-internals coverage lives in
# tests/test_ai_orchestration.py; the critical apply-safety proof lives in
# tests/test_api_ai_apply_safety.py.


def _make_app(tmp_path: Path) -> TestClient:
    """Mirrors `tests/test_api.py::_make_app` exactly (power mode pre-seeded, real CSRF/Host
    guard exercised) -- these tests aren't about safe/power mode, so power mode (this project's
    pre-Stage-2 "full" behavior) keeps them focused on the AI wiring itself."""
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
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    csrf_token: str = app.state.reclaim.csrf_token
    return TestClient(
        app,
        base_url=f"http://{_TEST_HOST}:{_TEST_PORT}",
        headers={security.CSRF_HEADER_NAME: csrf_token},
    )


def _scan_and_wait(client: TestClient, root: Path) -> None:
    response = client.post("/api/scan", json={"path": str(root)})
    assert response.status_code == 202
    status = client.get("/api/scan/status").json()
    assert status["status"] == "completed", status


# --- Degraded mode (no `ai` extra installed) ----------------------------------------------------


def test_ai_status_is_unavailable_when_extra_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: False)
    client = _make_app(tmp_path)

    response = client.get("/api/ai/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert "pip install reclaim[ai]" in body["unavailable_reason"]


def test_ai_analyze_returns_unavailable_without_starting_a_background_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: False)
    client = _make_app(tmp_path)

    response = client.post("/api/ai/analyze")

    assert response.status_code == 200  # never 202 -- nothing was accepted for background work
    assert response.json()["status"] == "unavailable"
    # Nothing was scheduled: the in-memory status never left "idle".
    assert client.app.state.reclaim.ai_status.status == "idle"


def test_ai_suggestions_is_unavailable_even_with_a_cached_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degraded mode wins over any stale in-memory cache -- a core-only install must never leak
    a previous power-mode/dev process's cached suggestions."""
    client = _make_app(tmp_path)
    client.app.state.reclaim.ai_clusters = [
        AICluster(
            cluster_id="c1",
            track=AITrack.SEMANTIC_IMAGE,
            members=(AIClusterMember(path=Path("a.jpg"), size_bytes=1),),
            raw_score=0.1,
            score_kind="max_pairwise_cosine_distance",
            rationale="test",
        )
    ]
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: False)

    response = client.get("/api/ai/suggestions")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["suggestions"] == []


# --- Preconditions / concurrency -----------------------------------------------------------------


def test_ai_analyze_requires_a_scan_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: True)
    client = _make_app(tmp_path)

    response = client.post("/api/ai/analyze")

    assert response.status_code == 400
    assert "scan" in response.json()["detail"]


def test_ai_analyze_already_running_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: True)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "notes.log").write_text("something scannable")
    client = _make_app(tmp_path)
    _scan_and_wait(client, root)

    app_state = client.app.state.reclaim
    with app_state.lock:
        app_state.ai_status = AIAnalysisStatus(status="running", started_at=time.time())

    response = client.post("/api/ai/analyze")

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


# --- Full lifecycle: analyze -> status -> suggestions, no files -----------------------------------


def test_ai_analyze_completes_synchronously_over_an_empty_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No image/document files ever triggers a heavy optional import (see
    test_ai_orchestration.py's equivalent unit proof) -- this exercises the real
    `ai_orchestration.run_ai_analysis` end to end through the actual endpoints, in an
    environment with no `ai` extra installed."""
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: True)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "notes.log").write_text("not an image or document extension")
    client = _make_app(tmp_path)
    _scan_and_wait(client, root)

    analyze_response = client.post("/api/ai/analyze")
    assert analyze_response.status_code == 202
    assert analyze_response.json()["status"] == "running"  # the response reflects the snapshot
    # taken BEFORE the background task runs -- same as POST /api/scan's own response shape.

    # TestClient runs BackgroundTasks synchronously as part of the request/response cycle, so
    # by the time the POST call above returned, the analysis has already finished -- this GET
    # observes that completed state (mirrors tests/test_api.py::_scan_and_wait's own comment).
    status_response = client.get("/api/ai/status")
    status_body = status_response.json()
    assert status_body["status"] == "completed"
    assert status_body["stale"] is False
    assert status_body["files_considered"] == {
        "images": 0,
        "documents": 0,
        "screenshot_candidates": 0,
    }
    assert set(status_body["tracks_run"]) == {
        "near_identical_image",
        "semantic_image",
        "near_dup_document_and_version_chain",
        "screenshot_burst",
    }

    suggestions_response = client.get("/api/ai/suggestions")
    suggestions_body = suggestions_response.json()
    assert suggestions_body["status"] == "completed"
    assert suggestions_body["suggestions"] == []


def test_ai_status_reports_stale_after_a_newer_scan_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: True)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "notes.log").write_text("something scannable")
    client = _make_app(tmp_path)
    _scan_and_wait(client, root)
    client.post("/api/ai/analyze")
    assert client.get("/api/ai/status").json()["stale"] is False

    _scan_and_wait(client, root)  # a second, newer scan completes

    assert client.get("/api/ai/status").json()["stale"] is True


# --- Presentation-layer mapping (GET /api/ai/suggestions never leaks a raw AICluster) -----------


def test_ai_suggestions_maps_a_cached_cluster_through_the_presentation_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_orchestration, "ai_extra_available", lambda: True)
    client = _make_app(tmp_path)
    app_state = client.app.state.reclaim
    keep = AIClusterMember(
        path=Path("C:/Photos/img1.jpg"), size_bytes=100, is_recommended_keep=True
    )
    drop = AIClusterMember(path=Path("C:/Photos/img2.jpg"), size_bytes=90)
    cluster = AICluster(
        cluster_id="near-identical-0",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(keep, drop),
        raw_score=4.0,
        score_kind="max_pairwise_hamming_distance",
        rationale="test fixture",
    )
    with app_state.lock:
        app_state.ai_clusters = [cluster]
        app_state.ai_status = AIAnalysisStatus(
            status="completed", scan_generation=app_state.scan_generation
        )

    response = client.get("/api/ai/suggestions")

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    assert len(body["suggestions"]) == 1
    suggestion = body["suggestions"][0]
    assert suggestion["track"] == "near_identical_image"
    assert suggestion["is_suggestion"] is True
    assert "Hamming distance 4" in suggestion["technical_detail"]
    assert "%" not in suggestion["technical_detail"]  # no invented confidence percentage
    member_paths = {m["path"] for m in suggestion["members"]}
    assert member_paths == {"C:/Photos/img1.jpg", "C:/Photos/img2.jpg"}
    keep_members = [m for m in suggestion["members"] if m["is_recommended_keep"]]
    assert len(keep_members) == 1
    assert keep_members[0]["path"] == "C:/Photos/img1.jpg"
