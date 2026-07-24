from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.config import (
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    DuplicatesConfig,
    LargeLogsConfig,
    SafetyConfig,
)
from reclaim.executor import QuarantineManifestEntry, append_manifest_entries
from reclaim.mode import REQUIRED_POWER_MODE_CONFIRMATION, switch_to_power_mode
from reclaim.models import Tier

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
            # min_reclaim_bytes=0: this fixture's duplicate pair is a 4KB file (kept small
            # deliberately, same reasoning as large_logs' low threshold above) — the real
            # default (1MB) materiality gate is tested in isolation in test_index.py, not here.
            duplicates=DuplicatesConfig(enabled=duplicates_enabled, min_reclaim_bytes=0),
        ),
    )


def _write(path: Path, content: bytes, *, mtime: float = _NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8420


def _make_app(tmp_path: Path, *, config: Config) -> TestClient:
    """Every test in this file exercises the real `local_origin_violation` guard (rule: local-
    API hardening), not a bypassed one — `base_url` makes httpx send a `Host` header matching
    what `create_app` was told it's bound to, and the default `headers=` carries the real
    per-process CSRF token, so every `client.get`/`client.post` call site below needs no
    changes at all. Tests that want to exercise a *rejected* request build their own client
    (or override a header) explicitly — see the `local_origin_violation`-specific tests at the
    end of this file.

    Isolated to a pre-seeded POWER-mode log (Stage 2): this whole file predates safe mode and
    exercises the pre-Stage-2 "full" apply/restore/vault/direct-delete behavior deliberately —
    every test here is really testing power-mode behavior, now made explicit rather than
    implicit. Safe-mode's own behavior is covered by its own dedicated tests
    (tests/test_safe_mode.py), which construct their own isolated mode log with no POWER entry
    (or an explicit SAFE one) instead of using this helper.
    """
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        first_run_state_path=tmp_path / "first_run_state.json",
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
    """Same as `_make_app`, but leaves the mode log empty — SAFE, the honest default for an
    install that has never switched modes — for the small number of tests that specifically
    exercise Stage 2's safe-mode behavior at the API layer."""
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    csrf_token: str = app.state.reclaim.csrf_token
    return TestClient(
        app,
        base_url=f"http://{_TEST_HOST}:{_TEST_PORT}",
        headers={security.CSRF_HEADER_NAME: csrf_token},
    )


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


def test_apply_already_running_returns_409(tmp_path: Path) -> None:
    """fix/apply-progress-feedback: `POST /api/apply` became a background-task + single-flight
    pattern, same guard `ScanStatus`/`AIAnalysisStatus` already have -- mirrors
    `test_scan_already_running_returns_409` above."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    app_state = client.app.state.reclaim
    from reclaim.api.state import ApplyStatus

    with app_state.lock:
        app_state.apply_status = ApplyStatus(status="running", started_at=time.time())

    response = client.post("/api/apply", json={"tier": "A"})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_restore_already_running_returns_409(tmp_path: Path) -> None:
    """Same single-flight guard as `test_apply_already_running_returns_409` above, for `POST
    /api/restore/{batch_id}`. A batch id that fails `validate_restorable_batch`'s synchronous
    pre-check would return 404 before this guard is ever reached, so this test needs a real,
    valid batch first."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)
    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]
    report = _apply_and_wait(client, {"tier": "A", "paths": tier_a_paths, "dry_run": False})
    assert paths["kept_file"].exists()  # sanity: fixture still intact for this test's own use

    app_state = client.app.state.reclaim
    from reclaim.api.state import RestoreStatus

    with app_state.lock:
        app_state.restore_status = RestoreStatus(status="running", started_at=time.time())

    response = client.post(f"/api/restore/{report['batch_id']}")
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


def test_duplicate_cluster_review_bad_limit_returns_400(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/duplicate-clusters/review?limit=0")
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


def _apply_and_wait(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    """`POST /api/apply` + `GET /api/apply/status` (fix/apply-progress-feedback: `POST /api/apply`
    became a background-task + polling pattern, same shape as `_scan_and_wait` above) -- returns
    the `result` (the same `ApplyResponse` shape the POST itself used to return synchronously).
    Only used for requests expected to be ACCEPTED (202) and actually run; a request refused
    synchronously (bad tier, safe mode's blanket-selection gate) still asserts its own status
    code directly against `client.post(...)`, never through this helper."""
    response = client.post("/api/apply", json=payload)
    assert response.status_code == 202, response.text
    status = client.get("/api/apply/status").json()
    assert status["status"] == "completed", status
    assert status["result"] is not None
    return status["result"]  # type: ignore[no-any-return]


def _restore_and_wait(client: TestClient, batch_id: str) -> dict[str, object]:
    """Same helper as `_apply_and_wait`, for `POST /api/restore/{batch_id}` + `GET
    /api/restore/status`. Only used when the restore is expected to be ACCEPTED (202) -- a
    synchronously-refused restore (unknown batch id, recycle_bin-only batch) still asserts its
    status code directly against `client.post(...)`."""
    response = client.post(f"/api/restore/{batch_id}")
    assert response.status_code == 202, response.text
    status = client.get("/api/restore/status").json()
    assert status["status"] == "completed", status
    assert status["result"] is not None
    return status["result"]  # type: ignore[no-any-return]


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


def test_duplicate_cluster_review_shows_keep_vs_delete_side_by_side(tmp_path: Path) -> None:
    """ADR-0007: the dashboard's review endpoint for the largest duplicate clusters — GG's
    "eyeball the survivor before applying" gate. `_build_tree`'s one duplicate pair
    (Archive/report.bin kept, Downloads/report_copy.bin proposed for deletion) is unaffected by
    hardlinks (both written independently, no shared inode), so reclaimable_bytes == size."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.get("/api/duplicate-clusters/review")
    assert response.status_code == 200
    body = response.json()
    assert body["has_scan"] is True
    assert len(body["clusters"]) == 1

    row = body["clusters"][0]
    assert row["needs_review"] is False
    assert row["reclaimable_bytes"] == 4_096
    member_paths = {m["path"] for m in row["cluster"]["members"]}
    assert member_paths == {paths["dup_original"].as_posix(), paths["dup_copy"].as_posix()}
    keep_members = [m for m in row["cluster"]["members"] if m["is_keep"]]
    assert len(keep_members) == 1
    assert keep_members[0]["path"] == paths["dup_original"].as_posix()


def test_duplicate_cluster_review_never_displays_an_adr_0008_excluded_member(
    tmp_path: Path,
) -> None:
    """ADR-0008 excludes a duplicate from `generate_duplicate_candidates` per-member (not
    whole-cluster) when it sits in an HF-style cache layout. The review endpoint's member LIST
    must reflect that exclusion too -- showing an excluded path as if it were still proposed for
    deletion would mislead the exact "eyeball the survivor" review this endpoint exists for."""
    root = tmp_path / "tree"
    content = b"same-bytes-" * 10_000

    keep_path = root / "Archive" / "report.bin"
    keep_path.parent.mkdir(parents=True)
    keep_path.write_bytes(content)

    eligible_duplicate = root / "Downloads" / "report_copy.bin"
    eligible_duplicate.parent.mkdir(parents=True)
    eligible_duplicate.write_bytes(content)

    hf_duplicate = (
        root
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--org--name"
        / "snapshots"
        / "rev"
        / "report.bin"
    )
    hf_duplicate.parent.mkdir(parents=True)
    hf_duplicate.write_bytes(content)

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.get("/api/duplicate-clusters/review")
    assert response.status_code == 200
    body = response.json()
    assert len(body["clusters"]) == 1

    member_paths = {m["path"] for m in body["clusters"][0]["cluster"]["members"]}
    assert member_paths == {keep_path.as_posix(), eligible_duplicate.as_posix()}
    assert hf_duplicate.as_posix() not in member_paths


def test_duplicate_cluster_review_empty_before_any_scan(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/duplicate-clusters/review")
    assert response.status_code == 200
    body = response.json()
    assert body == {"has_scan": False, "clusters": []}


# --- Stage: launch-UX one-click clean + suggested scan roots ---------------------------------


def test_one_click_summary_empty_before_any_scan(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/clean/one-click-summary")
    assert response.status_code == 200
    assert response.json() == {
        "has_scan": False,
        "groups": [],
        "total_bytes": 0,
        "total_bytes_human": "0 B",
        "total_file_count": 0,
    }


def test_one_click_summary_groups_only_categorically_safe_categories_in_plain_language(
    tmp_path: Path,
) -> None:
    """`_build_tree` also produces a `large_logs` and a `duplicates` candidate — both must be
    absent here even though they're real Tier A/B candidates elsewhere, since one-click clean is
    scoped to `dev_artifacts`/`package_caches`/`temp_and_browser_caches`/`crash_dumps` only (see
    `service._ONE_CLICK_SAFE_CATEGORY_GROUPS`)."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.get("/api/clean/one-click-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["has_scan"] is True

    group_ids = {g["category_group"] for g in body["groups"]}
    assert group_ids == {"dev_artifacts"}  # large_logs/duplicates never one-click-eligible

    dev_group = next(g for g in body["groups"] if g["category_group"] == "dev_artifacts")
    assert dev_group["plain_label"] == "Rebuildable developer files"
    assert dev_group["safety_reason"] == (
        "Safe — your build tools recreate these automatically (e.g. npm install)."
    )
    assert dev_group["paths"] == [paths["node_modules_dir"].as_posix()]
    assert dev_group["file_count"] == 1
    assert dev_group["total_bytes"] == body["total_bytes"] == 5_000
    assert body["total_file_count"] == 1


def test_one_click_apply_uses_explicit_paths_from_the_summary_and_moves_to_recycle_bin(
    tmp_path: Path,
) -> None:
    """Proves the one-click apply flow end to end: the group's enumerated `paths` (never a
    blanket tier/category-group selection) sent through the SAME `/api/apply` endpoint and
    `resolve_apply_selection` safe-mode guard every other apply path uses — with `tier="both"`
    since safe mode forces every candidate's tier to B (ADR-0023 guarantee 3)."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app_safe_mode(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    summary = client.get("/api/clean/one-click-summary").json()
    all_paths = [p for group in summary["groups"] for p in group["paths"]]
    assert paths["node_modules_dir"].as_posix() in all_paths

    body = _apply_and_wait(
        client, {"tier": "both", "paths": all_paths, "method": "vault", "dry_run": False}
    )
    assert body["apply"] is True
    assert body["method"] == "recycle_bin"  # safe mode forces this regardless of the request
    assert body["bytes_freed"] == 5_000
    assert not paths["node_modules_dir"].exists()  # really moved, not just previewed


def test_scan_suggested_roots_endpoint_returns_a_label_path_list(tmp_path: Path) -> None:
    """API-level smoke test only — real Downloads/home folder presence is machine-dependent,
    so the content assertions live in `test_suggested_scan_roots_only_lists_existing_folders`
    below against an injected `home=`, not against this process's real `Path.home()`."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/scan/suggested-roots")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["roots"], list)
    for root in body["roots"]:
        assert set(root) == {"label", "path"}


def test_suggested_scan_roots_only_lists_existing_folders(tmp_path: Path) -> None:
    from reclaim.api.service import suggested_scan_roots

    home_with_downloads = tmp_path / "home_with_downloads"
    (home_with_downloads / "Downloads").mkdir(parents=True)
    result = suggested_scan_roots(home=home_with_downloads)
    labels = {root.label for root in result.roots}
    assert labels == {"Downloads", "Home folder"}

    home_without_downloads = tmp_path / "home_without_downloads"
    home_without_downloads.mkdir()
    result_no_downloads = suggested_scan_roots(home=home_without_downloads)
    labels_no_downloads = {root.label for root in result_no_downloads.roots}
    assert labels_no_downloads == {"Home folder"}  # Downloads omitted, never shown disabled


def test_plain_language_category_matches_the_spec_mapping_and_falls_back_gracefully() -> None:
    from reclaim.api.schemas import plain_language_category

    label, reason = plain_language_category("dev_artifacts")
    assert label == "Rebuildable developer files"
    assert reason is not None and "npm install" in reason

    label, reason = plain_language_category("large_logs")
    assert label == "Large log files"
    assert reason is None

    # Unmapped id (e.g. model_caches, or a future ai_-namespaced group) falls back to the
    # technical label with no fabricated safety reason, never a crash or a raw snake_case id.
    label, reason = plain_language_category("model_caches")
    assert label == "Model Weight Caches"
    assert reason is None


def test_apply_category_group_filter_scopes_selection(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    body = _apply_and_wait(client, {"tier": "A", "category_group": "large_logs"})
    assert body["apply"] is False
    assert body["files_processed"] == 1
    assert body["items"][0]["path"] == paths["old_log"].as_posix()
    assert body["items"][0]["category_group"] == "large_logs"


def test_safe_mode_apply_requires_explicit_paths_no_blanket_tier_selection(tmp_path: Path) -> None:
    """Stage 2: a blanket tier/category-group apply with no `paths` — exactly the one-click
    "apply everything this tier matches" flow — is refused outright while the live mode is
    safe (the default for this app instance: `_make_app_safe_mode` never switches to power).
    Refused even as a dry run, since a dry-run response that implies a real apply would succeed
    the same way would be misleading."""
    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app_safe_mode(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    response = client.post("/api/apply", json={"tier": "B"})
    assert response.status_code == 400
    assert "explicit paths list" in response.json()["detail"]

    # The same request WITH explicit paths succeeds (dry-run) — the gate is specifically about
    # the blanket-selection shape, not a blanket "safe mode can never apply anything" refusal.
    tier_b_paths = [c["path"] for c in client.get("/api/candidates?tier=B").json()["candidates"]]
    assert tier_b_paths, "expected at least one Tier B candidate in this fixture"
    scoped_body = _apply_and_wait(client, {"tier": "B", "paths": tier_b_paths})
    assert scoped_body["method"] == "recycle_bin"


def test_apply_defaults_to_dry_run_when_field_omitted(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]

    body = _apply_and_wait(client, {"tier": "A", "paths": tier_a_paths})
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

    body = _apply_and_wait(client, {"tier": "A", "paths": tier_a_paths, "dry_run": True})
    assert body["apply"] is False
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

    report = _apply_and_wait(client, {"tier": "A", "paths": tier_a_paths, "dry_run": False})
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

    restore_body = _restore_and_wait(client, report["batch_id"])
    assert restore_body["files_succeeded"] == len(tier_a_paths)
    assert paths["node_modules_dir"].exists()
    assert paths["old_log"].exists()
    assert paths["old_log"].read_bytes() == original_log_bytes  # byte-identical, ground truth

    # Idempotent: restoring the same batch again reports already_restored, not an error.
    second_restore = _restore_and_wait(client, report["batch_id"])
    assert all(item["already_restored"] for item in second_restore["items"])


def test_restore_status_items_total_reflects_only_restorable_entries_in_mixed_batch(
    tmp_path: Path,
) -> None:
    """A verifier pass on fix/apply-progress-feedback found `RestoreStatus.items_total` was set
    to the vault-entry count at the START of a restore (correct -- `restore_batch`'s per-item
    progress loop only ever iterates vault entries, never the pre-classified `direct_delete`/
    `recycle_bin` ones), but silently overwritten with `report.files_processed` (the WHOLE
    batch, every method) at completion -- a real contract break for any mixed-method batch,
    exactly the shape a real batch takes in production (see executor.py's own ADR-0004 comment:
    "23,565 direct_delete entries alongside 7 vault ones", one `batch_id`). This constructs that
    exact shape directly against the manifest (bypassing a real apply, which is simpler here)
    and confirms `items_total` stays at the vault-only count end to end, never jumping to the
    full batch size on completion."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    batch_id = "batch_mixed_methods"
    manifest_path = tmp_path / "manifest.jsonl"
    vault_dir = tmp_path / "vault"

    vault_entries = []
    for i in range(2):
        vault_path = vault_dir / batch_id / f"vault_item_{i}.bin"
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_bytes(f"vault-payload-{i}".encode())
        vault_entries.append(
            QuarantineManifestEntry(
                batch_id=batch_id,
                original_path=tmp_path / f"restored_target_{i}.bin",
                size_bytes=len(f"vault-payload-{i}".encode()),
                is_dir=False,
                category="test_category",
                category_group="test_group",
                rationale="test",
                rebuild_instruction=None,
                tier=Tier.A,
                method="vault",
                vault_path=vault_path,
                retention_days=30,
                quarantined_at=_NOW,
                retention_until=_NOW + 29 * 86400,
            )
        )
    direct_delete_entries = [
        QuarantineManifestEntry(
            batch_id=batch_id,
            original_path=tmp_path / f"gone_{i}.bin",
            size_bytes=10,
            is_dir=False,
            category="test_category",
            category_group="test_group",
            rationale="test",
            rebuild_instruction=None,
            tier=Tier.A,
            method="direct_delete",
            vault_path=None,
            retention_days=None,
            quarantined_at=_NOW,
            retention_until=None,
        )
        for i in range(3)
    ]
    append_manifest_entries(manifest_path, [*vault_entries, *direct_delete_entries])

    response = client.post(f"/api/restore/{batch_id}")
    assert response.status_code == 202, response.text
    status = client.get("/api/restore/status").json()
    assert status["status"] == "completed", status
    assert status["items_total"] == 2  # vault-restorable entries only, not the 5-item batch
    assert status["items_processed"] == 2
    assert status["result"]["files_succeeded"] == 2
    assert status["result"]["files_unsupported"] == 3


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
    apply_body = _apply_and_wait(
        client, {"tier": "A", "paths": tier_a_paths, "method": "recycle_bin", "dry_run": False}
    )
    batch_id = apply_body["batch_id"]

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


# --- Local-origin guard: CSRF token + Host/Origin (DNS-rebinding) hardening -------------------


def test_mutating_request_without_csrf_token_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(tmp_path / "tree"),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    # No default headers at all — simulates any request that never read the dashboard's own
    # <meta> tag (a cross-origin page has no way to read it; this is the exact case CSRF
    # protection exists for).
    bare_client = TestClient(app, base_url=f"http://{_TEST_HOST}:{_TEST_PORT}")

    response = bare_client.post("/api/scan", json={"path": str(tmp_path)})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_mutating_request_with_wrong_csrf_token_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(tmp_path / "tree"),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    client = TestClient(
        app,
        base_url=f"http://{_TEST_HOST}:{_TEST_PORT}",
        headers={security.CSRF_HEADER_NAME: "not-the-real-token"},
    )

    response = client.post("/api/scan", json={"path": str(tmp_path)})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_read_only_request_needs_no_csrf_token(tmp_path: Path) -> None:
    """GET is never mutating — a bare client (no CSRF header at all) must still be able to read,
    as long as its Host header matches (see the DNS-rebinding tests below for what does gate
    reads)."""
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(tmp_path / "tree"),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    bare_client = TestClient(app, base_url=f"http://{_TEST_HOST}:{_TEST_PORT}")

    response = bare_client.get("/api/summary")
    assert response.status_code == 200


def test_request_with_mismatched_host_header_is_rejected(tmp_path: Path) -> None:
    """DNS-rebinding defense: a request whose `Host` header doesn't name the exact loopback
    authority this process is bound to is refused outright, even for a read-only GET — this is
    exactly the shape of a successful DNS-rebinding attack (the browser's `fetch` genuinely
    connects to 127.0.0.1, but the `Host` header it sends still carries the attacker's original
    hostname)."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    response = client.get("/api/summary", headers={"host": "evil.example.com"})
    assert response.status_code == 403
    assert "Host header" in response.json()["detail"]


def test_request_with_mismatched_origin_header_is_rejected(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    response = client.get("/api/summary", headers={"origin": "http://evil.example.com"})
    assert response.status_code == 403
    assert "Origin header" in response.json()["detail"]


def test_request_with_matching_origin_header_is_accepted(tmp_path: Path) -> None:
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    response = client.get("/api/summary", headers={"origin": f"http://{_TEST_HOST}:{_TEST_PORT}"})
    assert response.status_code == 200


def test_non_api_paths_are_not_guarded(tmp_path: Path) -> None:
    """The static dashboard shell (`/`, `/static/*`) carries no per-user data — the guard is
    deliberately scoped to `/api` only, so a mismatched Host there is not itself a 403 (the
    browser still can't do anything useful with it without a valid CSRF token on the API)."""
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(tmp_path / "tree"),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    bare_client = TestClient(app, base_url=f"http://{_TEST_HOST}:{_TEST_PORT}")

    response = bare_client.get("/", headers={"host": "evil.example.com"})
    assert response.status_code == 200
