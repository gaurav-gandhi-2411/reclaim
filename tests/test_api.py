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
from reclaim.index import InaccessibleEntry, ScanIndex
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
    # P0-5: honest defaults before any scan has ever run -- 0/None, never a fabricated number.
    assert body["inaccessible_path_count"] == 0
    assert body["inaccessible_known_bytes"] == 0
    assert body["inaccessible_unknown_count"] == 0
    assert body["reconciliation_volume"] is None
    assert body["reconciliation_delta_bytes"] is None
    assert body["reconciliation_delta_pct"] is None

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


def test_notices_route_serves_third_party_notices(tmp_path: Path) -> None:
    """Repo-root NOTICES.md (also copied to {app}\\NOTICES.md by the installer, see
    packaging/reclaim.iss) is reachable at /NOTICES, same pattern as the existing /LICENSE route
    -- linked from the dashboard footer's "Third-party notices" link."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/NOTICES")
    assert response.status_code == 200
    assert "Third-party notices" in response.text
    assert "onnxruntime" in response.text


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


# --- scan cancellation ---------------------------------------------------------------------


def test_scan_cancel_endpoint_is_a_no_op_when_nothing_is_running(tmp_path: Path) -> None:
    """Safe to call speculatively (e.g. a UI racing its own poll loop) -- never a 409, always
    the current (unchanged) status."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "idle"


def test_scan_cancel_endpoint_stops_a_running_scan_with_a_consistent_partial_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real cancellation, end to end through the actual route: `service.run_scan` is run in a
    real background `threading.Thread` (matching `test_run_apply_completes_and_leaves_a_durable_
    manifest_with_zero_status_polls`'s own established pattern -- TestClient runs BackgroundTasks
    SYNCHRONOUSLY as part of the request/response cycle, so driving the scan through
    `client.post("/api/scan", ...)` itself would block until the whole scan finished and leave no
    way to interleave a genuinely concurrent cancel), while the test's own thread issues a real
    `POST /api/scan/cancel` once the scan has demonstrably made progress -- not before it starts,
    not after it's already finished."""
    import threading

    import reclaim.scanner as scanner_module
    from reclaim.api import service
    from reclaim.api.state import ScanStatus

    # Deterministic, not a wall-clock race: fires on nearly every entry (this file's own
    # `test_full_drive_scan_completes_correctly_with_maximal_progress_callback_frequency` already
    # establishes this convention for exercising the live progress-republishing wiring).
    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)

    root = tmp_path / "tree"
    num_dirs, files_per_dir = 8, 300
    for d in range(num_dirs):
        subdir = root / f"dir_{d}"
        subdir.mkdir(parents=True)
        for f in range(files_per_dir):
            (subdir / f"f_{f}.txt").write_text("x", encoding="utf-8")
    total_entries = num_dirs + num_dirs * files_per_dir

    client = _make_app(tmp_path, config=_config(tmp_path / "other"))
    state = client.app.state.reclaim  # type: ignore[attr-defined]

    started_at = time.time()
    with state.lock:
        # Mirrors exactly what `POST /api/scan`'s route handler does before scheduling
        # `run_scan` -- see `AppState.cancel_scan_event`'s docstring for why the clear happens
        # here, synchronously, rather than inside `run_scan` itself.
        state.cancel_scan_event.clear()
        state.scan_status = ScanStatus(
            status="running",
            root=root,
            started_at=started_at,
            phase="estimating",
            current_drive=root.as_posix(),
            drives_total=1,
            drives_done=0,
        )

    thread = threading.Thread(target=service.run_scan, args=(state, [root], started_at))
    thread.start()

    deadline = time.monotonic() + 10.0
    made_progress = False
    while time.monotonic() < deadline:
        with state.lock:
            processed = state.scan_status.entries_processed or 0
            still_running = state.scan_status.status == "running"
        if not still_running:
            break
        if processed >= 20:
            made_progress = True
            break
        time.sleep(0.005)
    assert made_progress, "scan never reached the progress threshold before finishing/timing out"

    response = client.post("/api/scan/cancel")
    assert response.status_code == 200

    thread.join(timeout=30)
    assert not thread.is_alive(), "run_scan did not stop within 30s of being cancelled"

    status = client.get("/api/scan/status").json()
    assert status["status"] == "cancelled"
    assert status["error"] is None
    assert status["entries_total"] is not None and status["entries_total"] < total_entries
    assert status["files_pruned"] == 0

    with ScanIndex(state.db_path) as index:
        inventory = index.full_inventory(under=root)
    # No torn state: the index is consistent with exactly what the (cancelled) scan reported.
    assert len(inventory) == status["entries_total"]


# --- full-drive-scan-eta: fixed-drive enumeration + full-drive orchestration -------------------


def test_scan_fixed_drives_endpoint_returns_a_real_drive_list(tmp_path: Path) -> None:
    """Real machine, real Win32 call (this test file already targets Windows/NTFS only) -- every
    CI runner and real dev machine has at least a fixed C:\\ drive."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/scan/fixed-drives")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["drives"], list)
    assert len(body["drives"]) >= 1
    assert all(d.endswith(":/") or d.endswith(":\\") for d in body["drives"])


def test_scan_fixed_drives_endpoint_returns_500_when_none_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.api import service
    from reclaim.drives import NoFixedDrivesFoundError

    def fake_list_fixed_drives() -> list[Path]:
        raise NoFixedDrivesFoundError("no fixed drives on this fixture machine")

    monkeypatch.setattr(service, "list_fixed_drives", fake_list_fixed_drives)

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.get("/api/scan/fixed-drives")
    assert response.status_code == 500
    assert "no fixed drives" in response.json()["detail"]


def _build_small_tree(root: Path, *, file_count: int) -> None:
    root.mkdir(parents=True)
    for i in range(file_count):
        (root / f"f{i}.txt").write_text("x" * (i + 1), encoding="utf-8")


def test_full_drive_scan_orchestrates_sequentially_across_fixture_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /api/scan/full-drive` never touches a real Windows drive in this test -- real drive
    enumeration is `list_fixed_drives`'s own job, unit-tested in isolation in test_drives.py.
    Here, `reclaim.api.service.list_fixed_drives` is monkeypatched to two small real fixture
    trees standing in for "the machine's fixed drives" (same dependency-injection pattern
    `_scan_and_wait` already relies on for the single-path case: TestClient runs
    BackgroundTasks synchronously, so this needs no polling loop)."""
    from reclaim.api import service

    drive_a = tmp_path / "drive_a"
    drive_b = tmp_path / "drive_b"
    _build_small_tree(drive_a, file_count=3)
    _build_small_tree(drive_b, file_count=5)

    monkeypatch.setattr(service, "list_fixed_drives", lambda: [drive_a, drive_b])

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/full-drive")
    assert response.status_code == 202, response.text

    status = client.get("/api/scan/status").json()
    assert status["status"] == "completed", status
    assert status["phase"] == "done"
    assert status["drives_total"] == 2
    assert status["drives_done"] == 2
    assert status["root"] is None  # no single root is honest for a real multi-drive scan
    assert status["current_drive"] is None
    assert status["entries_total"] == 3 + 5  # summed across both fixture "drives"
    assert status["eta_seconds"] == 0.0
    assert status["entries_processed"] == status["entries_estimated_total"] == 8


def test_full_drive_scan_with_exactly_one_fixed_drive_reports_it_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.api import service

    only_drive = tmp_path / "only_drive"
    _build_small_tree(only_drive, file_count=2)
    monkeypatch.setattr(service, "list_fixed_drives", lambda: [only_drive])

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/full-drive")
    assert response.status_code == 202

    status = client.get("/api/scan/status").json()
    assert status["status"] == "completed"
    assert status["drives_total"] == 1
    assert status["root"] == only_drive.as_posix()  # single-root case reports it, unlike multi


def test_full_drive_scan_already_running_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.api import service
    from reclaim.api.state import ScanStatus

    monkeypatch.setattr(service, "list_fixed_drives", lambda: [tmp_path / "drive_a"])

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    app_state = client.app.state.reclaim
    with app_state.lock:
        app_state.scan_status = ScanStatus(status="running", root=tmp_path, started_at=time.time())

    response = client.post("/api/scan/full-drive")
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_full_drive_scan_completes_correctly_with_maximal_progress_callback_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces `_HEARTBEAT_INTERVAL_SECONDS` down to 0 so `run_scan`'s `on_count_progress`/
    `on_scan_progress` closures fire on nearly every entry instead of the real 5s-gated cadence
    -- proves the live status-republishing wiring itself (not just the pure `_compute_eta_seconds`
    function) runs correctly under realistic call volume without deadlocking or corrupting
    `scan_status`."""
    import reclaim.scanner as scanner_module
    from reclaim.api import service

    monkeypatch.setattr(scanner_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)

    drive_a = tmp_path / "drive_a"
    drive_b = tmp_path / "drive_b"
    _build_small_tree(drive_a, file_count=10)
    _build_small_tree(drive_b, file_count=10)
    monkeypatch.setattr(service, "list_fixed_drives", lambda: [drive_a, drive_b])

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/full-drive")
    assert response.status_code == 202

    status = client.get("/api/scan/status").json()
    assert status["status"] == "completed"
    assert status["entries_total"] == 20
    assert status["drives_done"] == 2


def test_full_drive_scan_failure_on_one_root_aborts_the_whole_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real failure partway through a multi-drive scan must abort the WHOLE scan
    (`status="failed"`), not silently report success having skipped a drive -- whatever partial
    aggregate stats already accumulated (from `drive_a`, scanned first) are kept, not discarded."""
    from reclaim.api import service

    drive_a = tmp_path / "drive_a"
    drive_b = tmp_path / "drive_b"  # never actually scanned -- count_entries_fast fails first
    _build_small_tree(drive_a, file_count=3)
    drive_b.mkdir()
    monkeypatch.setattr(service, "list_fixed_drives", lambda: [drive_a, drive_b])

    real_count_entries_fast = service.count_entries_fast

    def fake_count_entries_fast(root: Path, **kwargs: object) -> int:
        if root == drive_b:
            raise OSError("simulated I/O fault on drive_b")
        return real_count_entries_fast(root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "count_entries_fast", fake_count_entries_fast)

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/full-drive")
    assert response.status_code == 202

    status = client.get("/api/scan/status").json()
    assert status["status"] == "failed"
    assert status["error"] is not None and "simulated I/O fault" in status["error"]
    assert status["entries_total"] == 3  # drive_a's real work, kept -- not discarded
    assert status["drives_total"] == 2


def test_full_drive_scan_returns_500_when_no_fixed_drives_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.api import service
    from reclaim.drives import NoFixedDrivesFoundError

    def fake_list_fixed_drives() -> list[Path]:
        raise NoFixedDrivesFoundError("no fixed drives on this fixture machine")

    monkeypatch.setattr(service, "list_fixed_drives", fake_list_fixed_drives)

    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))
    response = client.post("/api/scan/full-drive")
    assert response.status_code == 500
    assert "no fixed drives" in response.json()["detail"]


def test_single_path_scan_status_carries_the_new_phase_and_eta_fields(tmp_path: Path) -> None:
    """`POST /api/scan`'s original contract (`status`/`root`/`entries_total`/...) is unchanged --
    this only proves the new full-drive-scan-eta fields are ALSO populated correctly for the
    single-path case (`drives_total=1`), not just for `POST /api/scan/full-drive`."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    client = _make_app(tmp_path, config=_config(root))
    status = _scan_and_wait(client, root)

    assert status["phase"] == "done"
    assert status["drives_total"] == 1
    assert status["drives_done"] == 1
    assert status["root"] == root.as_posix()
    assert status["current_drive"] is None
    assert status["entries_processed"] == status["entries_total"]
    assert status["eta_seconds"] == 0.0


# --- full-drive-scan-eta: pure ETA-computing function ------------------------------------------


def test_compute_eta_seconds_is_none_without_an_estimated_total() -> None:
    from reclaim.api.service import _compute_eta_seconds

    assert _compute_eta_seconds(10, None, 5.0) is None


def test_compute_eta_seconds_is_none_on_the_very_first_tick() -> None:
    from reclaim.api.service import _compute_eta_seconds

    assert _compute_eta_seconds(0, 100, 5.0) is None
    assert _compute_eta_seconds(10, 100, 0.0) is None


def test_compute_eta_seconds_computes_remaining_time_at_the_observed_rate() -> None:
    from reclaim.api.service import _compute_eta_seconds

    # 50 entries in 5s -> rate 10/s; 50 remaining of 100 -> 5s left.
    assert _compute_eta_seconds(50, 100, 5.0) == pytest.approx(5.0)


def test_compute_eta_seconds_clamps_negative_remaining_to_zero() -> None:
    from reclaim.api.service import _compute_eta_seconds

    # The real walk visited more than the fast estimate predicted.
    assert _compute_eta_seconds(120, 100, 5.0) == 0.0


def test_compute_eta_seconds_is_none_when_the_observed_rate_is_effectively_zero() -> None:
    from reclaim.api.service import _compute_eta_seconds

    # 1 entry over an absurdly long elapsed time -> a rate far below the trust threshold.
    assert _compute_eta_seconds(1, 100, 1e12) is None


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


def test_summary_surfaces_inaccessible_paths_from_a_persisted_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-5, end to end through the real API: a directory the scan cannot list is surfaced in
    `/api/summary` as a distinct, counted entity (not silently dropped from every total), and
    persists there even though this field is sourced from the index -- not the in-memory,
    last-scan-only `skipped_unreadable_*` fields."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "readable.txt").write_bytes(b"x" * 100)
    blocked_dir = root / "blocked_dir"
    blocked_dir.mkdir()
    (blocked_dir / "inner.txt").write_bytes(b"y" * 500)

    real_scandir = os.scandir

    def fake_scandir(path: object, *args: object, **kwargs: object) -> object:
        if "blocked_dir" in str(path):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", fake_scandir)
    client = _make_app(tmp_path, config=_config(root))

    _scan_and_wait(client, root)

    summary = client.get("/api/summary").json()
    assert summary["has_scan"] is True
    assert summary["inaccessible_path_count"] == 1
    assert summary["inaccessible_known_bytes"] == 0
    assert summary["inaccessible_unknown_count"] == 1
    # The blocked subtree's 500 bytes never made it into the visible total -- exactly the P0-5
    # gap this field exists to make legible instead of silent.
    assert summary["total_indexed_bytes"] == 100


def test_treemap_includes_a_non_deletable_inaccessible_node_with_byte_total_and_explanation(
    tmp_path: Path,
) -> None:
    """P0-5 follow-up acceptance criterion, encoded directly: given a scan with known-
    inaccessible-bytes and unknown-size inaccessible paths, `/api/treemap` -- the treemap itself,
    not just the `/api/summary` banner -- contains a distinct `inaccessible` node whose
    `size_bytes` is the correct known-bytes total and whose `explanation` is a non-empty reason
    string. That node must never be indistinguishable from a real, deletable candidate: it always
    carries `is_candidate=False` (nothing real to select -- these are, by definition, paths
    Reclaim could not read) and `is_inaccessible=True`."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "readable.txt").write_bytes(b"x" * 1_000)
    client = _make_app(tmp_path, config=_config(root))

    _scan_and_wait(client, root)

    state = client.app.state.reclaim  # type: ignore[attr-defined]
    with ScanIndex(state.db_path) as index:
        index.replace_inaccessible_under_root(
            root,
            [
                InaccessibleEntry(
                    path=(root / "blocked_known").as_posix(),
                    error="Access is denied",
                    size_estimate_bytes=5_000,
                    size_estimate_is_lower_bound=True,
                ),
                InaccessibleEntry(
                    path=(root / "blocked_unknown").as_posix(),
                    error="Access is denied",
                    size_estimate_bytes=None,
                    size_estimate_is_lower_bound=False,
                ),
            ],
            scanned_at=time.time(),
        )

    treemap = client.get("/api/treemap").json()
    inaccessible_nodes = [n for n in treemap["nodes"] if n["category_group"] == "inaccessible"]
    assert len(inaccessible_nodes) == 1, treemap["nodes"]
    node = inaccessible_nodes[0]

    # The known-bytes total (only the blocked_known entry's 5,000 bytes -- the unknown-size
    # entry contributes nothing to this number by design, see InaccessibleSummary's docstring).
    assert node["size_bytes"] == 5_000
    assert node["size_human"] == "4.9 KB"
    assert node["is_candidate"] is False
    assert node["is_inaccessible"] is True
    assert node["is_dir"] is False

    # A real, non-empty explanation -- not just present, but naming the actual counts so a user
    # sees WHY this figure is an estimate, directly in the treemap, not only in the summary
    # banner.
    explanation = node["explanation"]
    assert explanation
    assert "2 path(s)" in explanation
    assert "1 of those have no size estimate" in explanation

    # Never confusable with a real scanned directory: no ordinary node shares its category_group,
    # and it never carries a real filesystem path.
    assert node["path"] != root.as_posix()
    assert all(n["category_group"] != "inaccessible" for n in treemap["nodes"] if n is not node)


def test_treemap_omits_the_inaccessible_node_when_nothing_was_inaccessible(
    tmp_path: Path,
) -> None:
    """No fabricated node: a scan with zero inaccessible paths gets no `inaccessible` entry at
    all in the treemap response, matching `/api/summary`'s own "0/[] never a fabricated number"
    posture for the same underlying data."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "readable.txt").write_bytes(b"x" * 1_000)
    client = _make_app(tmp_path, config=_config(root))

    _scan_and_wait(client, root)

    treemap = client.get("/api/treemap").json()
    assert all(n["category_group"] != "inaccessible" for n in treemap["nodes"])


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

    # P0-2 (2026-08 audit): model_caches gained a real mapping (used by the new Settings tab) --
    # no longer the fallback example this test used before.
    label, reason = plain_language_category("model_caches")
    assert label == "ML model weight caches"
    assert reason is not None and "re-downloadable" in reason

    # Unmapped id (e.g. "other", or a future ai_-namespaced group) falls back to the technical
    # label with no fabricated safety reason, never a crash or a raw snake_case id.
    label, reason = plain_language_category("other")
    assert label == "Uncategorized"
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


def test_apply_response_surfaces_skip_reason_for_a_preflight_skipped_item(
    tmp_path: Path,
) -> None:
    """Audit P0-1, API-boundary follow-up: `ItemApplyResult.skip_reason` must reach the real
    HTTP response body, not just structlog -- a caller of `POST /api/apply` (dashboard, CLI, a
    future MCP tool) needs to tell "file was locked" apart from "hardlink-shared into another
    active install" without reading server logs. Locks `old_log` for real (a plain Python
    `open()` held in this test process, across the actual `POST /api/apply` call) -- not a mock
    of the internal `ItemApplyResult` object -- and asserts the skip reason round-trips through
    the full HTTP response, while the OTHER Tier A candidate in the same batch still succeeds
    (the skip must not abort the rest of the batch)."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    tier_a_paths = [c["path"] for c in client.get("/api/candidates?tier=A").json()["candidates"]]
    assert paths["old_log"].as_posix() in tier_a_paths
    assert paths["node_modules_dir"].as_posix() in tier_a_paths

    handle = open(paths["old_log"], "r+b")  # noqa: SIM115, PTH123 -- held across the real HTTP call
    try:
        report = _apply_and_wait(client, {"tier": "A", "paths": tier_a_paths, "dry_run": False})
    finally:
        handle.close()

    items_by_path = {item["path"]: item for item in report["items"]}

    locked_item = items_by_path[paths["old_log"].as_posix()]
    assert locked_item["succeeded"] is False
    assert locked_item["skip_reason"] == "file_in_use"
    assert locked_item["error"] is None  # never attempted -- not an OS error
    assert paths["old_log"].exists()  # untouched at its original location

    unlocked_item = items_by_path[paths["node_modules_dir"].as_posix()]
    assert unlocked_item["succeeded"] is True
    assert unlocked_item["skip_reason"] is None
    assert not paths["node_modules_dir"].exists()  # the skip did not abort the rest of the batch

    assert report["files_processed"] == len(tier_a_paths)
    assert report["files_failed"] == 1
    assert report["files_succeeded"] == len(tier_a_paths) - 1


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


def test_run_apply_completes_and_leaves_a_durable_manifest_with_zero_status_polls(
    tmp_path: Path,
) -> None:
    """A browser disconnect mid-poll (closed tab, lost network, whatever) must never orphan a
    running apply or leave the manifest unrecoverable -- proven here by never polling
    `GET /api/apply/status` AT ALL while the background task runs, which is the literal worst
    case ("nobody is watching this task, ever, for its whole lifetime"). `service.run_apply` is
    Starlette's actual background-task body (scheduled via `BackgroundTasks.add_task`, which
    Starlette itself decouples from the request/response and any client connection -- this test
    doesn't re-prove Starlette's own framework guarantee, it proves THIS app's specific use of it
    doesn't accidentally depend on a poller ever running, e.g. by only flushing/finalizing state
    from inside a code path a status-check happens to trigger). Runs `run_apply` in a real
    `threading.Thread` (matching how Starlette's own worker-thread-pool dispatch behaves) with
    no polling until after the thread has fully finished, then confirms both the in-memory
    status AND the on-disk manifest are correct -- the manifest write happens inside
    `apply_batch` itself regardless of whether anyone ever reads `apply_status`."""
    import threading

    from reclaim.api import service
    from reclaim.executor import read_manifest_entries

    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "vacation.jpg"
    photo.write_bytes(b"not a real jpeg, just fixture bytes")

    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    state = client.app.state.reclaim  # type: ignore[attr-defined]
    from reclaim.api.schemas import ApplyRequest

    selected, method, apply = service.resolve_apply_selection(
        state,
        ApplyRequest(tier="both", paths=[photo.as_posix()], method="vault", dry_run=False),
    )
    assert selected, "fixture setup bug: expected the explicit path to resolve to one candidate"

    thread = threading.Thread(
        target=service.run_apply, args=(state, selected, method, apply, time.time())
    )
    thread.start()
    # Deliberately NO client.get("/api/apply/status") call anywhere in this window -- the point
    # is that nothing about task completion or manifest durability depends on one ever happening.
    thread.join(timeout=30)
    assert not thread.is_alive(), "run_apply did not finish within 30s"

    assert not photo.exists()  # the real move genuinely happened, unattended
    assert state.apply_status.status == "completed"
    assert state.apply_status.error is None

    entries = read_manifest_entries(state.manifest_path)
    done_entries = [e for e in entries if e.phase == "done" and e.original_path == photo]
    assert len(done_entries) == 1, (
        f"expected exactly one durable phase='done' manifest entry for {photo}, found "
        f"{[e.phase for e in entries if e.original_path == photo]}"
    )


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
        log_path=tmp_path / "reclaim.log",
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
        log_path=tmp_path / "reclaim.log",
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
        log_path=tmp_path / "reclaim.log",
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
        log_path=tmp_path / "reclaim.log",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    bare_client = TestClient(app, base_url=f"http://{_TEST_HOST}:{_TEST_PORT}")

    response = bare_client.get("/", headers={"host": "evil.example.com"})
    assert response.status_code == 200


# --- G25: bug-report diagnostics ---------------------------------------------------------------


def test_diagnostics_endpoint_reports_paths_and_metadata_only(tmp_path: Path) -> None:
    """The dashboard's "Copy diagnostics" button reads this endpoint -- every field must be a
    path, a version, a mode name, or the log tail itself (paths/counts/errors only, never file
    contents -- see `DiagnosticsResponse`'s docstring and PRIVACY.md)."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "power"  # _make_app pre-seeds a POWER-mode log
    assert isinstance(body["reclaim_version"], str) and body["reclaim_version"]
    assert isinstance(body["ai_extra_installed"], bool)
    assert isinstance(body["os_version"], str) and body["os_version"]
    assert body["log_path"] == str(tmp_path / "reclaim.log")


def test_diagnostics_log_tail_reflects_the_real_log_file(tmp_path: Path) -> None:
    """A request made through this same app must actually be logged (via
    `reclaim.api.security`'s local-origin guard, which logs nothing content-derived) and then
    show up in the next call's `log_tail` -- proof this reads the real file `configure_logging`
    is writing to, not a hardcoded placeholder."""
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    # Any request at all is enough to guarantee the log file exists and is non-empty by the
    # time diagnostics reads it (mode.switched_to_power from _make_app's setup already wrote at
    # least one line before this).
    client.get("/api/summary")

    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    log_tail = response.json()["log_tail"]
    assert log_tail
    assert log_tail != "(no log file yet — nothing has been logged this install)"


def test_diagnostics_log_tail_placeholder_when_log_file_missing(tmp_path: Path) -> None:
    """A fresh log path that has genuinely never been written to (a log path override pointed
    somewhere new mid-session) must return an explanatory placeholder, never a crash or an empty
    string a user could mistake for "nothing happened."""
    from reclaim.api.service import _read_log_tail

    missing_path = tmp_path / "never-written.log"
    expected = "(no log file yet — nothing has been logged this install)"
    assert _read_log_tail(missing_path) == expected


# --- Update check (opt-in; see PRIVACY.md's "Updates" section) ---------------------------------


def test_update_check_endpoint_disabled_by_default_makes_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UpdateCheckConfig.enabled` defaults to `False` (see `config.py` / PRIVACY.md) -- the
    endpoint must report `enabled=False`, `status="disabled"`, and never even attempt to build an
    `httpx.Client`. Patches `httpx.Client` to raise if called at all, so this test would fail
    loudly (not silently pass) if that guarantee ever regressed."""
    from reclaim import update_check as update_check_module

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("httpx.Client must never be constructed when update_check is disabled")

    monkeypatch.setattr(update_check_module.httpx, "Client", _fail_if_called)
    client = _make_app(tmp_path, config=_config(tmp_path / "tree"))

    response = client.get("/api/update-check")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["status"] == "disabled"
    assert body["update_available"] is False
    assert body["latest_version"] is None
    assert isinstance(body["current_version"], str) and body["current_version"]


def test_update_check_endpoint_reports_available_update_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `[update_check] enabled = true`, the endpoint delegates to
    `reclaim.update_check.check_for_update` -- this test injects a `MockTransport`-backed client
    (via monkeypatching `httpx.Client` itself) so it never reaches the real network, matching this
    project's zero-live-API-calls-in-CI convention."""
    import httpx

    from reclaim import update_check as update_check_module
    from reclaim.config import UpdateCheckConfig

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"tag_name": "v99.0.0", "html_url": "https://example.test/r"}
        )

    real_client_cls = httpx.Client
    monkeypatch.setattr(
        update_check_module.httpx,
        "Client",
        lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)),
    )
    update_check_module._cache.clear()

    config = _config(tmp_path / "tree")
    config = config.model_copy(update={"update_check": UpdateCheckConfig(enabled=True)})
    client = _make_app(tmp_path, config=config)

    response = client.get("/api/update-check")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["status"] == "ok"
    assert body["update_available"] is True
    assert body["latest_version"] == "v99.0.0"
    assert body["release_url"] == "https://example.test/r"


# --- P0-2 fix (2026-08 audit): in-app category settings tab ------------------------------------


def _make_app_with_config_path(tmp_path: Path, *, config: Config, config_path: Path) -> TestClient:
    """Same shape as `_make_app`, but exposes `config_path` explicitly -- these tests write to
    disk (`POST /api/settings/categories/{group}`), and `create_app`'s own default `config_path`
    is the repo-relative `config.toml` (matching the real CLI default), which must never be what
    a test writes to."""
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config,
        config_path=config_path,
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


def test_settings_categories_lists_every_category_with_real_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    client = _make_app_with_config_path(
        tmp_path, config=_config(tmp_path / "tree"), config_path=config_path
    )

    response = client.get("/api/settings/categories")

    assert response.status_code == 200
    body = response.json()
    by_group = {c["category_group"]: c for c in body["categories"]}
    assert set(by_group) == set(CategoriesConfig.model_fields)
    # dev_artifacts is explicitly overridden to enabled=True in this file's `_config` fixture.
    assert by_group["dev_artifacts"]["enabled"] is True
    assert by_group["dev_artifacts"]["forced_off_in_safe_mode"] is True
    assert by_group["package_caches"]["forced_off_in_safe_mode"] is False
    # Every category needs a non-empty, real description -- never a raw snake_case fallback with
    # no context (the Settings tab renders this directly).
    for entry in body["categories"]:
        assert entry["description"]
        assert entry["category_label"]


def test_post_settings_category_persists_to_disk_and_takes_effect_without_restart(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    client = _make_app_with_config_path(
        tmp_path, config=_config(tmp_path / "tree"), config_path=config_path
    )

    # old_installers defaults to False (see config.py's CategoriesConfig docstring).
    before = client.get("/api/settings/categories").json()
    by_group = {c["category_group"]: c for c in before["categories"]}
    assert by_group["old_installers"]["enabled"] is False

    response = client.post("/api/settings/categories/old_installers", json={"enabled": True})

    assert response.status_code == 200
    by_group = {c["category_group"]: c for c in response.json()["categories"]}
    assert by_group["old_installers"]["enabled"] is True

    # Persisted to the real file, not just in-memory.
    from reclaim.config import load_config

    on_disk = load_config(config_path)
    assert on_disk.categories.old_installers.enabled is True

    # Takes effect immediately -- a second GET without restarting the process reflects it too.
    after = client.get("/api/settings/categories").json()
    by_group = {c["category_group"]: c for c in after["categories"]}
    assert by_group["old_installers"]["enabled"] is True


def test_post_settings_category_unknown_group_returns_404(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    client = _make_app_with_config_path(
        tmp_path, config=_config(tmp_path / "tree"), config_path=config_path
    )

    response = client.post("/api/settings/categories/not_a_real_category", json={"enabled": True})

    assert response.status_code == 404
    assert "not_a_real_category" in response.json()["detail"]


def test_post_settings_category_without_csrf_token_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    mode_log = tmp_path / "mode_log.jsonl"
    switch_to_power_mode(REQUIRED_POWER_MODE_CONFIRMATION, log_path=mode_log)
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(tmp_path / "tree"),
        config_path=config_path,
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=mode_log,
        log_path=tmp_path / "reclaim.log",
        host=_TEST_HOST,
        port=_TEST_PORT,
    )
    bare_client = TestClient(app, base_url=f"http://{_TEST_HOST}:{_TEST_PORT}")

    response = bare_client.post("/api/settings/categories/old_installers", json={"enabled": True})

    assert response.status_code == 403
    assert not config_path.exists()


def test_post_settings_category_invalidates_a_warmed_candidates_cache(tmp_path: Path) -> None:
    """Cross-PR regression (2026-08 audit): P0-2's `update_category_setting` (this file's
    `test_post_settings_category_persists_to_disk_and_takes_effect_without_restart` above) and
    perf/dedup-cache's `_cached_all_candidates` (below) ship in separate PRs and, merged
    together, interact badly -- `_cached_all_candidates` is keyed ONLY on `scan_generation`,
    which a category toggle never bumps. Without invalidating the cache in
    `update_category_setting` too, a dashboard that already warmed the cache with one
    `GET /api/candidates` call keeps serving the pre-toggle tier classification after a category
    is toggled on, directly contradicting `update_category_setting`'s own "takes effect
    immediately without a restart" docstring/API contract for the common real-world case (the
    dashboard is loaded before Settings is touched). Live-reproduced with a real duplicate pair:
    `duplicates.enabled=False` puts it at Tier B; warm the cache; toggle `duplicates` on; the
    re-fetched candidate must now report Tier A, not the stale Tier B."""
    root = tmp_path / "tree"
    paths = _build_tree(root)
    config_path = tmp_path / "config.toml"
    client = _make_app_with_config_path(
        tmp_path, config=_config(root, duplicates_enabled=False), config_path=config_path
    )
    _scan_and_wait(client, root)
    dup_copy_posix = paths["dup_copy"].as_posix()

    # Warm the cache -- this is the call `_cached_all_candidates` memoizes.
    before = client.get("/api/candidates?category=duplicates").json()
    before_by_path = {c["path"]: c for c in before["candidates"]}
    assert before_by_path[dup_copy_posix]["tier"] == "B"

    response = client.post("/api/settings/categories/duplicates", json={"enabled": True})
    assert response.status_code == 200

    # Re-fetch without restarting the process -- must reflect the toggle immediately, not the
    # cache's stale pre-toggle classification.
    after = client.get("/api/candidates?category=duplicates").json()
    after_by_path = {c["path"]: c for c in after["candidates"]}
    assert after_by_path[dup_copy_posix]["tier"] == "A", (
        "candidates cache was not invalidated by the category toggle -- still serving the "
        "pre-toggle Tier B classification"
    )


# --- perf/dedup-cache (docs/AUDIT-2026-08.md P0-3): cached, concurrency-guarded
# `_cached_all_candidates` -----------------------------------------------------------------------


def test_cached_all_candidates_avoids_redundant_dedup_recompute_within_one_scan_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the cache: `generate_duplicate_candidates` (the expensive whole-index
    BLAKE3 hash pass `_all_candidates` -> `_cached_all_candidates` wraps) must run exactly ONCE
    for a scan generation, no matter how many of the 5 call sites (summary, treemap, candidates,
    one-click summary, blanket apply) are hit afterward -- not once per call site, as it was
    before this fix."""
    from reclaim.api import service

    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    call_count = 0
    real = service.generate_duplicate_candidates

    def _counting(*args: object, **kwargs: object) -> list[object]:
        nonlocal call_count
        call_count += 1
        return real(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(service, "generate_duplicate_candidates", _counting)

    assert client.get("/api/summary").status_code == 200
    assert client.get("/api/treemap").status_code == 200
    assert client.get("/api/candidates?tier=A").status_code == 200
    assert client.get("/api/clean/one-click-summary").status_code == 200

    assert call_count == 1, "each of 4 call sites recomputed dedup independently -- cache miss"


def test_cached_all_candidates_invalidates_on_a_new_scan_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh COMPLETED scan must never leave a caller looking at stale, pre-rescan candidates
    -- the cache is keyed to `state.scan_generation` (ADR-0025's existing staleness key, bumped
    once per completed scan) specifically so a rescan busts it."""
    from reclaim.api import service

    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)

    call_count = 0
    real = service.generate_duplicate_candidates

    def _counting(*args: object, **kwargs: object) -> list[object]:
        nonlocal call_count
        call_count += 1
        return real(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(service, "generate_duplicate_candidates", _counting)

    assert client.get("/api/summary").status_code == 200
    assert call_count == 1

    assert client.get("/api/summary").status_code == 200
    assert call_count == 1, "second call within the same scan generation should hit the cache"

    _scan_and_wait(client, root)  # a new COMPLETED scan -- bumps state.scan_generation

    assert client.get("/api/summary").status_code == 200
    assert call_count == 2, "a new scan generation must invalidate the cache, not serve stale data"


def test_cached_all_candidates_serializes_concurrent_recompute_for_the_same_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concurrency guard this cache exists to add: two requests racing for the SAME scan
    generation must never both trigger a full recompute -- exactly the "two independent
    concurrent dedup passes fired 12 seconds apart for one page load" bug docs/AUDIT-2026-08.md's
    P0-3 live-reproduced. Deterministic (not a timing race that could pass by luck), following
    this file's own `test_two_serialized_batches_...` pattern in tests/test_executor.py: a
    `threading.Event` proves the second caller only starts once the first has demonstrably begun
    (and is holding `candidates_cache_lock`), then a real `.is_alive()` check proves the second
    caller is genuinely blocked, not merely racing and happening to lose."""
    import threading

    from reclaim.api import service
    from reclaim.index import ScanIndex

    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root))
    _scan_and_wait(client, root)
    state = client.app.state.reclaim  # type: ignore[attr-defined]

    call_count = 0
    first_call_started = threading.Event()
    first_call_may_finish = threading.Event()
    real = service.generate_duplicate_candidates

    def _blocking(*args: object, **kwargs: object) -> list[object]:
        nonlocal call_count
        call_count += 1
        first_call_started.set()
        assert first_call_may_finish.wait(timeout=30), "first caller never released"
        return real(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(service, "generate_duplicate_candidates", _blocking)

    results: list[list[object]] = []

    def _run() -> None:
        with ScanIndex(state.db_path) as index:
            results.append(service._cached_all_candidates(index, state))

    first_thread = threading.Thread(target=_run)
    second_thread = threading.Thread(target=_run)

    first_thread.start()
    assert first_call_started.wait(timeout=10), "first caller never reached the hash pass"

    second_thread.start()
    time.sleep(0.5)  # give the second caller a real chance to attempt-and-block
    assert second_thread.is_alive(), (
        "second caller returned while the first still holds candidates_cache_lock -- the lock "
        "did not actually serialize the two callers"
    )

    first_call_may_finish.set()
    first_thread.join(timeout=30)
    second_thread.join(timeout=30)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()

    assert call_count == 1, "the second caller recomputed instead of reusing the first's result"
    assert len(results) == 2
    assert results[0] == results[1]


def test_category_cards_use_hardlink_aware_reclaimable_bytes_not_logical_size(
    tmp_path: Path,
) -> None:
    """P1 fix (docs/AUDIT-2026-08.md): `_category_cards`'s top-line total must sum
    `reclaimable_bytes` (hardlink-aware, ADR-0006) when a candidate has it populated, never the
    naive logical `size_bytes` -- a hardlinked 'duplicate' shares its blocks with the kept copy
    and reclaims 0 bytes on delete, so summing `size_bytes` would overstate real reclaimable
    space by the hardlinked file's full logical size."""
    from reclaim.api.service import _category_cards
    from reclaim.models import Candidate, Verdict

    hardlinked_duplicate = Candidate(
        path=tmp_path / "dup.bin",
        is_dir=False,
        category="exact_duplicate",
        category_group="duplicates",
        size_bytes=5_000,  # logical size -- what a naive sum would (wrongly) report
        tier=Tier.B,
        rationale="test fixture",
        rebuild_instruction=None,
        safety_verdict=Verdict.ELIGIBLE,
        safety_reason_code="ok",
        retention_days=30,
        reclaimable_bytes=0,  # hardlink-aware: shares blocks with the kept copy, reclaims 0
    )

    cards = _category_cards([hardlinked_duplicate])

    assert len(cards) == 1
    assert cards[0].total_bytes == 0
    assert cards[0].total_bytes_human == "0 B"
