from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

# ADR-0025's critical safety proof: an AI-suggestion-shaped path (an ordinary file no rule
# detector ever flags — the common case for a real AI suggestion) submitted through the real
# `POST /api/apply` still gets exactly the same safety treatment as a hand-picked deterministic
# candidate — safe mode's recycle-bin/Tier-B guarantee included — and a BLOCKED path is silently
# excluded, never force-applied. `reclaim.ai` itself is never imported anywhere in this file: the
# whole point is that this works without any AI code in the loop, proving the apply-time bridge
# (`service._build_user_selected_candidate`) is a general "explicit path" safety capability, not
# something that trusts an AI-labeled path any differently than any other explicit selection.

_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8420


def _config(root: Path) -> Config:
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30)
        ),
    )


def _make_app(tmp_path: Path, *, config: Config) -> TestClient:
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
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


def _make_app_safe_mode(tmp_path: Path, *, config: Config) -> TestClient:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
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
    assert client.get("/api/scan/status").json()["status"] == "completed"


# --- The core AI-suggestion-shaped path: not a deterministic candidate at all -------------------


def test_explicit_path_not_a_deterministic_candidate_still_applies_dry_run(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "Pictures" / "vacation.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"not a real jpeg, just fixture bytes")

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    # No rule detector flags an ordinary photo -- it is NOT in the deterministic candidate list.
    tier_both = client.get("/api/candidates?tier=both").json()["candidates"]
    assert photo.as_posix() not in {c["path"] for c in tier_both}

    response = client.post("/api/apply", json={"tier": "both", "paths": [photo.as_posix()]})

    assert response.status_code == 200
    body = response.json()
    assert body["apply"] is False
    assert body["files_processed"] == 1
    item = body["items"][0]
    assert item["path"] == photo.as_posix()
    assert item["category_group"] == "user_selected"
    assert item["tier"] == "B"
    assert photo.exists()  # dry-run -- nothing touched


def test_explicit_path_not_a_deterministic_candidate_really_applies_and_restores(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "Pictures" / "vacation.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"not a real jpeg, just fixture bytes")

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.post(
        "/api/apply",
        json={"tier": "both", "paths": [photo.as_posix()], "method": "vault", "dry_run": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["apply"] is True
    assert body["files_succeeded"] == 1
    assert not photo.exists()  # really moved

    batch_id = body["batch_id"]
    restore_response = client.post(f"/api/restore/{batch_id}")
    assert restore_response.status_code == 200
    assert restore_response.json()["files_succeeded"] == 1
    assert photo.exists()  # real, working restore round-trip


# --- Safe mode: the same explicit path still gets recycle-bin-only, Tier B --------------------


def test_explicit_path_in_safe_mode_is_forced_to_recycle_bin_and_tier_b(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "Pictures" / "vacation.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"not a real jpeg, just fixture bytes")

    client = _make_app_safe_mode(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    # method="vault" is explicitly REQUESTED but must be silently overridden to recycle_bin --
    # the exact same safe-mode guarantee every other apply path already has (ADR-0023).
    response = client.post(
        "/api/apply",
        json={"tier": "both", "paths": [photo.as_posix()], "method": "vault", "dry_run": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "recycle_bin"
    assert body["items"][0]["tier"] == "B"
    assert body["items"][0]["method"] == "recycle_bin"
    assert body["files_succeeded"] == 1
    assert not photo.exists()  # really moved (to the Recycle Bin)


# --- A BLOCKED path is silently excluded, never force-applied ---------------------------------


def test_explicit_path_under_a_protected_root_is_silently_excluded_never_applied(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    protected = root / "Windows"
    protected.mkdir(parents=True)
    protected_file = protected / "system.dll"
    protected_file.write_bytes(b"do not touch")
    scannable_marker = root / "notes.log"
    scannable_marker.write_text("something scannable")

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.post(
        "/api/apply",
        json={
            "tier": "both",
            "paths": [protected_file.as_posix()],
            "method": "vault",
            "dry_run": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    # The BLOCKED path matched no deterministic candidate AND failed the fresh safety check --
    # it is silently dropped from the batch entirely, not force-applied and not erroring the
    # whole request.
    assert body["files_processed"] == 0
    assert protected_file.exists()
    assert protected_file.read_bytes() == b"do not touch"


# --- Regression: an explicit path that IS already a deterministic candidate is unaffected -------


def test_explicit_path_that_is_already_a_deterministic_candidate_keeps_its_own_category(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    (root / "Project").mkdir(parents=True)
    (root / "Project" / "package.json").write_bytes(b"{}")  # manifest-adjacency gate --
    # `detect_dev_artifacts` only ever proposes node_modules when a package.json sits alongside.
    node_modules_file = root / "Project" / "node_modules" / "pkg" / "index.js"
    node_modules_file.parent.mkdir(parents=True)
    node_modules_file.write_bytes(b"x")

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    dev_artifact_path = node_modules_file.parent.parent.as_posix()  # the node_modules dir itself
    response = client.post(
        "/api/apply", json={"tier": "both", "paths": [dev_artifact_path], "dry_run": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["files_processed"] == 1
    assert body["items"][0]["category_group"] == "dev_artifacts"  # NOT overwritten to
    # "user_selected" -- the deterministic-candidate path is completely unaffected by the new
    # explicit-path bridge.
