from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.ai import category_explainer
from reclaim.ai.category_explainer import AnthropicAPIError, CategoryExplanation
from reclaim.anthropic_key_store import load_key
from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.config import Config
from reclaim.index import ScanIndex
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.models import Candidate, Tier, Verdict

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8421

# R2: endpoint-level coverage for the Settings tab's key-management routes and the per-category
# explanation route. Mirrors tests/test_api_ai.py's exact `_make_app` fixture pattern (real
# CSRF/Host guard exercised, power mode pre-seeded so these tests stay focused on R2's own
# wiring). Never makes a real Anthropic API call -- every test that would otherwise reach the
# network monkeypatches `reclaim.api.service.category_explainer`'s functions directly, the same
# "zero live API calls in CI" convention `tests/test_update_check.py` already documents.


def _make_app(tmp_path: Path) -> TestClient:
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
        anthropic_key_path=tmp_path / "anthropic_key.bin",
        ai_explanation_cache_dir=tmp_path / "ai_explanations",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    csrf_token: str = app.state.reclaim.csrf_token
    return TestClient(
        app,
        base_url=f"http://{_TEST_HOST}:{_TEST_PORT}",
        headers={security.CSRF_HEADER_NAME: csrf_token},
    )


# --- Key status / set / delete ------------------------------------------------------------------


def test_key_status_starts_unconfigured(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    response = client.get("/api/settings/anthropic-key")
    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_set_key_then_status_reports_configured(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    response = client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-real-key"})
    assert response.status_code == 200
    assert response.json() == {"configured": True}
    assert client.get("/api/settings/anthropic-key").json() == {"configured": True}


def test_set_key_response_never_echoes_the_key(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    response = client.post(
        "/api/settings/anthropic-key", json={"api_key": "sk-ant-should-never-appear"}
    )
    assert "sk-ant-should-never-appear" not in response.text


def test_set_key_actually_persists_encrypted_via_dpapi(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-persisted"})
    key_path = tmp_path / "anthropic_key.bin"
    assert key_path.exists()
    assert b"sk-ant-persisted" not in key_path.read_bytes()  # never plaintext on disk
    assert load_key(key_path) == "sk-ant-persisted"


def test_delete_key_clears_configured_status(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-to-remove"})
    response = client.delete("/api/settings/anthropic-key")
    assert response.status_code == 200
    assert response.json() == {"configured": False}
    assert (tmp_path / "anthropic_key.bin").exists() is False


def test_delete_key_is_a_no_op_when_nothing_configured(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    response = client.delete("/api/settings/anthropic-key")
    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_replacing_a_key_overwrites_the_old_one(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-first"})
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-second"})
    assert load_key(tmp_path / "anthropic_key.bin") == "sk-ant-second"


# --- Test key (validate before/without saving) --------------------------------------------------


def test_test_key_returns_valid_true_for_a_good_candidate_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(category_explainer, "validate_api_key", lambda api_key, **_: True)
    client = _make_app(tmp_path)
    response = client.post("/api/settings/anthropic-key/test", json={"api_key": "sk-ant-good"})
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_test_key_returns_valid_false_for_a_rejected_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(category_explainer, "validate_api_key", lambda api_key, **_: False)
    client = _make_app(tmp_path)
    response = client.post("/api/settings/anthropic-key/test", json={"api_key": "sk-ant-bad"})
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_test_key_never_500s_on_a_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(api_key: str, **_: object) -> bool:
        raise AnthropicAPIError("simulated network failure")

    monkeypatch.setattr(category_explainer, "validate_api_key", _boom)
    client = _make_app(tmp_path)
    response = client.post("/api/settings/anthropic-key/test", json={"api_key": "sk-ant-fake"})
    assert response.status_code == 200  # degrades to valid=False, never a 500
    assert response.json()["valid"] is False


def test_test_key_without_a_body_key_tests_the_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def _record(api_key: str, **_: object) -> bool:
        seen.append(api_key)
        return True

    monkeypatch.setattr(category_explainer, "validate_api_key", _record)
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-stored"})
    response = client.post("/api/settings/anthropic-key/test", json={})
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert seen == ["sk-ant-stored"]


def test_test_key_with_no_stored_key_and_no_body_key_reports_invalid_without_calling_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        category_explainer, "validate_api_key", lambda api_key, **_: calls.append(api_key) or True
    )
    client = _make_app(tmp_path)
    response = client.post("/api/settings/anthropic-key/test", json={})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert calls == []  # never calls Anthropic when there is nothing to test


# --- Category explanation: degrade gracefully in every state -------------------------------------


def _seed_dev_artifacts_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypasses a real scan (this file is about R2's wiring, not the scan pipeline) --
    monkeypatches the same two seams `service.build_category_explanation` reads from: whether
    the index has any records, and what `_all_candidates` returns."""
    monkeypatch.setattr(ScanIndex, "has_any_records", lambda self: True)
    candidate = Candidate(
        path=tmp_path / "node_modules",
        is_dir=True,
        category="dev_artifacts",
        category_group="dev_artifacts",
        size_bytes=5 * 1024**3,
        tier=Tier.A,
        rationale="rebuildable via npm install",
        rebuild_instruction="npm install",
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="ok",
        retention_days=None,
    )
    monkeypatch.setattr(service, "_all_candidates", lambda index, state: [candidate])


def test_category_explanation_unavailable_with_no_scan(tmp_path: Path) -> None:
    client = _make_app(tmp_path)
    response = client.get("/api/ai/category-explanation/dev_artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["explanation"] is None


def test_category_explanation_unavailable_for_a_category_not_in_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dev_artifacts_candidate(tmp_path, monkeypatch)
    client = _make_app(tmp_path)
    response = client.get("/api/ai/category-explanation/crash_dumps")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["explanation"] is None


def test_category_explanation_unavailable_without_a_configured_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dev_artifacts_candidate(tmp_path, monkeypatch)
    client = _make_app(tmp_path)
    response = client.get("/api/ai/category-explanation/dev_artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert "Anthropic" in body["message"] or "API key" in body["message"]
    assert body["explanation"] is None


def test_category_explanation_ok_with_key_and_mocked_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dev_artifacts_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        category_explainer,
        "explain_category",
        lambda descriptor, **_: CategoryExplanation(
            category_group=descriptor.category_group,
            explanation="Prose-only explanation, no path anywhere.",
            cached=False,
        ),
    )
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-configured"})

    response = client.get("/api/ai/category-explanation/dev_artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["explanation"] == "Prose-only explanation, no path anywhere."
    assert body["cached"] is False


def test_category_explanation_never_500s_on_an_anthropic_api_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dev_artifacts_candidate(tmp_path, monkeypatch)

    def _boom(descriptor: object, **_: object) -> CategoryExplanation:
        raise AnthropicAPIError("simulated Anthropic outage")

    monkeypatch.setattr(category_explainer, "explain_category", _boom)
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-configured"})

    response = client.get("/api/ai/category-explanation/dev_artifacts")
    assert response.status_code == 200  # never a 500
    body = response.json()
    assert body["status"] == "error"
    assert body["explanation"] is None
    # The message shown to the user must never contain the API key.
    assert "sk-ant-configured" not in response.text


def test_category_explanation_response_never_contains_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dev_artifacts_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        category_explainer,
        "explain_category",
        lambda descriptor, **_: CategoryExplanation(
            category_group=descriptor.category_group, explanation="ok", cached=False
        ),
    )
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-must-not-leak"})
    response = client.get("/api/ai/category-explanation/dev_artifacts")
    assert "sk-ant-must-not-leak" not in response.text


def test_diagnostics_response_never_contains_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRIVACY: `GET /api/diagnostics` (G25's bug-report endpoint) must never surface the
    Anthropic key, directly or via the log tail — see `reclaim.anthropic_key_store`'s "never
    logged" module docstring."""
    client = _make_app(tmp_path)
    client.post("/api/settings/anthropic-key", json={"api_key": "sk-ant-must-never-appear"})
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    assert "sk-ant-must-never-appear" not in response.text
