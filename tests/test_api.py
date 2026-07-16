from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api.app import create_app
from reclaim.config import (
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    DuplicatesConfig,
    LargeLogsConfig,
    SafetyConfig,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_NOW = 1_700_000_000.0
_OLD_LOG_AGE_DAYS = 45


def _config(root: Path, *, duplicates_enabled: bool = False) -> Config:
    """Fixture-relative protected roots (same pattern as the other stages' tests) so real
    C:\\Windows is never touched; a low `large_logs` threshold keeps the fixture small.

    ADR-0001 changed `dev_artifacts`'s default retention to `None` (direct permanent delete),
    which would make this file's whole-batch vault+restore round-trip tests (see
    `test_apply_with_dry_run_false_really_quarantines_and_restore_round_trips`) impossible — a
    `direct_delete` entry can never be restored. Those tests exist to prove the API's
    apply/restore wiring, not to pin `dev_artifacts`' retention default, so `retention_days=30`
    is set explicitly here to keep that proof intact.
    """
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=30),
            large_logs=LargeLogsConfig(enabled=True, min_size_bytes=1_000, stale_days=30),
            duplicates=DuplicatesConfig(enabled=duplicates_enabled),
        ),
    )


def _write(path: Path, content: bytes, *, mtime: float = _NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def _make_app(tmp_path: Path, *, config: Config) -> TestClient:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
    )
    return TestClient(app)


# --- Empty state (no scan yet) ---------------------------------------------------------------


def test_empty_state_before_any_scan(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    status = client.get("/api/scan/status")
    assert status.status_code == 200
    assert status.json()["status"] == "idle"

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["has_scan"] is False
    assert body["total_indexed_bytes"] == 0
    assert body["categories"] == []

    treemap = client.get("/api/treemap")
    assert treemap.status_code == 200
    assert treemap.json() == {
        "has_scan": False,
        "root": None,
        "total_bytes": 0,
        "total_bytes_human": "0 B",
        "nodes": [],
    }

    candidates = client.get("/api/candidates")
    assert candidates.status_code == 200
    assert candidates.json()["has_scan"] is False
    assert candidates.json()["candidates"] == []

    quarantine = client.get("/api/quarantine")
    assert quarantine.status_code == 200
    assert quarantine.json() == {"batches": []}


def test_index_page_serves_html(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Reclaim" in response.text


# --- Error paths -------------------------------------------------------------------------------


def test_scan_nonexistent_path_returns_400(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan", json={"path": str(tmp_path / "does_not_exist")})
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_scan_already_running_returns_409(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    app_state = client.app.state.reclaim
    from reclaim.api.state import ScanStatus

    with app_state.lock:
        app_state.scan_status = ScanStatus(status="running", root=tmp_path, started_at=time.time())

    target = tmp_path / "tree"
    target.mkdir()
    response = client.post("/api/scan", json={"path": str(target)})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_restore_nonexistent_batch_returns_404(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/restore/does-not-exist")
    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


def test_candidates_bad_tier_returns_400(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/candidates?tier=Z")
    assert response.status_code == 400


def test_apply_bad_tier_returns_400(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/apply", json={"tier": "Z"})
    assert response.status_code == 400


# --- Fixture tree used by the full-pipeline tests below ----------------------------------------


def _build_tree(root: Path) -> dict[str, Path]:
    package_json = root / "Project" / "package.json"
    _write(package_json, b'{"name": "demo"}')

    node_modules_file = root / "Project" / "node_modules" / "pkg" / "index.js"
    _write(node_modules_file, b"x" * 5_000)

    old_log = root / "Logs" / "old_big.log"
    _write(old_log, b"y" * 2_000, mtime=_NOW - _OLD_LOG_AGE_DAYS * 86400)

    dup_content = b"z" * 4_096
    dup_original = root / "Archive" / "report.bin"
    dup_copy = root / "Downloads" / "report_copy.bin"
    _write(dup_original, dup_content)
    _write(dup_copy, dup_content)

    kept_file = root / "Documents" / "keep_me.txt"
    _write(kept_file, b"do-not-touch")

    return {
        "package_json": package_json,
        "node_modules_dir": node_modules_file.parent.parent,
        "old_log": old_log,
        "dup_original": dup_original,
        "dup_copy": dup_copy,
        "kept_file": kept_file,
    }


def _scan_and_wait(client: TestClient, root: Path) -> dict[str, object]:
    response = client.post("/api/scan", json={"path": str(root)})
    assert response.status_code == 202
    # TestClient's ASGI transport runs FastAPI BackgroundTasks synchronously as part of the
    # same request/response cycle, so the scan has already finished by the time `.post()`
    # returns — no polling loop needed in tests (a real browser client does poll, see app.js).
    status = client.get("/api/scan/status").json()
    assert status["status"] == "completed", status
    return status


# --- Full pipeline: scan -> summary/treemap/candidates -> dry-run apply -> real apply -> restore


def test_full_pipeline_scan_summary_treemap_candidates(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))

    _scan_and_wait(client, root)

    summary = client.get("/api/summary").json()
    assert summary["has_scan"] is True
    assert summary["total_indexed_bytes"] > 0
    category_groups = {c["category_group"] for c in summary["categories"]}
    assert "dev_artifacts" in category_groups
    assert "large_logs" in category_groups
    assert "duplicates" in category_groups  # default-disabled -> lands in Tier B, still listed

    treemap = client.get("/api/treemap").json()
    assert treemap["has_scan"] is True
    assert treemap["root"] == root.as_posix()
    node_labels = {n["label"] for n in treemap["nodes"]}
    assert "Project" in node_labels
    assert "Logs" in node_labels

    tier_a = client.get("/api/candidates?tier=A").json()
    tier_a_paths = {c["path"] for c in tier_a["candidates"]}
    assert paths["node_modules_dir"].as_posix() in tier_a_paths
    assert paths["old_log"].as_posix() in tier_a_paths
    assert paths["kept_file"].as_posix() not in tier_a_paths  # negative control

    node_modules_posix = paths["node_modules_dir"].as_posix()
    dev_artifact = next(c for c in tier_a["candidates"] if c["path"] == node_modules_posix)
    assert dev_artifact["category_group"] == "dev_artifacts"
    assert "rebuild" in dev_artifact["rationale"].lower()

    tier_b = client.get("/api/candidates?tier=B&category=duplicates").json()
    assert tier_b["count"] == 1
    dup_candidate = tier_b["candidates"][0]
    assert dup_candidate["path"] == paths["dup_copy"].as_posix()  # under Downloads -> not kept
    cluster = dup_candidate["duplicate_cluster"]
    assert cluster is not None
    member_paths = {m["path"] for m in cluster["members"]}
    assert member_paths == {paths["dup_original"].as_posix(), paths["dup_copy"].as_posix()}
    keep_members = [m for m in cluster["members"] if m["is_keep"]]
    assert len(keep_members) == 1
    assert keep_members[0]["path"] == paths["dup_original"].as_posix()


def test_apply_category_group_filter_scopes_selection(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.post("/api/apply", json={"tier": "A", "category_group": "large_logs"})
    assert response.status_code == 200
    body = response.json()
    assert body["apply"] is False
    assert body["files_processed"] == 1
    assert body["items"][0]["path"] == paths["old_log"].as_posix()
    assert body["items"][0]["category_group"] == "large_logs"


def test_apply_defaults_to_dry_run_when_field_omitted(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]

    response = client.post("/api/apply", json={"tier": "A", "paths": tier_a_paths})
    assert response.status_code == 200
    body = response.json()
    assert body["apply"] is False
    assert body["files_succeeded"] == len(tier_a_paths)
    assert paths["node_modules_dir"].exists()  # nothing on disk touched
    assert paths["old_log"].exists()


def test_apply_defaults_to_dry_run_when_field_explicitly_true(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]

    response = client.post("/api/apply", json={"tier": "A", "paths": tier_a_paths, "dry_run": True})
    assert response.status_code == 200
    assert response.json()["apply"] is False
    assert paths["node_modules_dir"].exists()
    assert paths["old_log"].exists()


def test_apply_with_dry_run_false_really_quarantines_and_restore_round_trips(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    original_log_bytes = paths["old_log"].read_bytes()
    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]

    apply_response = client.post(
        "/api/apply", json={"tier": "A", "paths": tier_a_paths, "dry_run": False}
    )
    assert apply_response.status_code == 200
    report = apply_response.json()
    assert report["apply"] is True
    assert report["files_succeeded"] == len(tier_a_paths)
    assert report["files_failed"] == 0
    assert not paths["node_modules_dir"].exists()
    assert not paths["old_log"].exists()
    assert paths["kept_file"].exists()  # negative control, never touched

    quarantine = client.get("/api/quarantine").json()
    assert len(quarantine["batches"]) == 1
    batch = quarantine["batches"][0]
    assert batch["batch_id"] == report["batch_id"]
    assert batch["item_count"] == len(tier_a_paths)
    assert batch["can_restore"] is True
    assert batch["restore_blocked_reason"] is None

    restore_response = client.post(f"/api/restore/{report['batch_id']}")
    assert restore_response.status_code == 200
    restore_body = restore_response.json()
    assert restore_body["files_succeeded"] == len(tier_a_paths)
    assert paths["node_modules_dir"].exists()
    assert paths["old_log"].exists()
    assert paths["old_log"].read_bytes() == original_log_bytes  # byte-identical, ground truth

    # Idempotent: restoring the same batch again reports already_restored, not an error.
    second_restore = client.post(f"/api/restore/{report['batch_id']}")
    assert second_restore.status_code == 200
    assert all(item["already_restored"] for item in second_restore.json()["items"])


def test_recycle_bin_batch_restore_is_blocked_with_real_executor_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reclaim.executor as executor_module

    monkeypatch.setattr(executor_module.send2trash, "send2trash", lambda path: None)

    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]
    apply_response = client.post(
        "/api/apply",
        json={"tier": "A", "paths": tier_a_paths, "method": "recycle_bin", "dry_run": False},
    )
    assert apply_response.status_code == 200
    batch_id = apply_response.json()["batch_id"]

    quarantine = client.get("/api/quarantine").json()
    batch = next(b for b in quarantine["batches"] if b["batch_id"] == batch_id)
    assert batch["can_restore"] is False
    assert "Recycle-Bin-quarantined" in batch["restore_blocked_reason"]
    assert "Windows Explorer" in batch["restore_blocked_reason"]

    restore_response = client.post(f"/api/restore/{batch_id}")
    assert restore_response.status_code == 409
    # The real exception message from executor.RecycleBinRestoreUnsupportedError, not a
    # separately-worded UI string — identical wording to what the listing endpoint already
    # showed (same recycle_bin entry count feeds both).
    assert restore_response.json()["detail"] == batch["restore_blocked_reason"]
