"""perf/path-scoped-apply-cache: a path-scoped `POST /api/apply` reuses the dashboard's warm
candidate cache instead of re-running every detector.

Three groups of tests, mirroring the conditions the cache reuse was approved on:

1. Invalidation -- the cache key covers scan generation, live mode, the effective config and the
   allowed roots, so every trigger that changes what `_all_candidates` returns makes a warmed
   cache read as cold (#43 lesson: a toggle that did not invalidate served stale tiers).
2. Teeth -- a cache entry for a file that was swapped / resized / deleted / replaced by a
   junction after the scan is still refused AT APPLY TIME by the unchanged executor checks.
3. Unchanged semantics -- a path the detectors never flagged (and a duplicate-cluster member)
   still resolves through `_build_user_selected_candidate`, exactly as before.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.api.state import AppState
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
_OLD_LOG_AGE_DAYS = 45
_HOST = "127.0.0.1"
_PORT = 8420


def _config(root: Path, *, stale_days: int = 30, retention_days: int = 30) -> Config:
    root_posix = root.as_posix()
    return Config(
        safety=SafetyConfig(protected_roots=[f"{root_posix}/Windows", f"{root_posix}/Windows/*"]),
        categories=CategoriesConfig(
            dev_artifacts=DevArtifactsConfig(enabled=True, retention_days=retention_days),
            large_logs=LargeLogsConfig(enabled=True, min_size_bytes=1_000, stale_days=stale_days),
            duplicates=DuplicatesConfig(enabled=False, min_reclaim_bytes=0),
        ),
    )


def _write(path: Path, content: bytes, *, mtime: float = _NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def _make_app(tmp_path: Path, root: Path) -> TestClient:
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


def _apply(client: TestClient, paths: list[str], *, dry_run: bool) -> dict[str, object]:
    response = client.post("/api/apply", json={"tier": "both", "paths": paths, "dry_run": dry_run})
    assert response.status_code == 202, response.text
    status = client.get("/api/apply/status").json()
    assert status["status"] == "completed", status
    return status["result"]  # type: ignore[no-any-return]


def _item(result: dict[str, object], path: Path) -> dict[str, object]:
    return next(i for i in result["items"] if i["path"] == path.as_posix())  # type: ignore[index]


class _Tree:
    """Fixture tree: two stale logs (large_logs candidates), a node_modules directory
    (dev_artifacts candidate), an ordinary never-flagged file, and a duplicate pair."""

    def __init__(self, root: Path) -> None:
        self.log_a = root / "Logs" / "a_old.log"
        self.log_b = root / "Logs" / "b_old.log"
        _write(self.log_a, b"a" * 2_000, mtime=_NOW - _OLD_LOG_AGE_DAYS * 86400)
        _write(self.log_b, b"b" * 2_000, mtime=_NOW - _OLD_LOG_AGE_DAYS * 86400)
        _write(root / "Project" / "package.json", b'{"name": "demo"}')
        nm_file = root / "Project" / "node_modules" / "pkg" / "index.js"
        _write(nm_file, b"x" * 5_000)
        self.node_modules = nm_file.parent.parent
        self.plain = root / "Documents" / "plain.txt"
        _write(self.plain, b"never flagged by any detector")
        self.dup_original = root / "Archive" / "report.bin"
        self.dup_copy = root / "Downloads" / "report_copy.bin"
        _write(self.dup_original, b"z" * 4_096)
        _write(self.dup_copy, b"z" * 4_096)


@pytest.fixture
def warm_env(tmp_path: Path) -> tuple[TestClient, AppState, _Tree, Path]:
    root = tmp_path / "tree"
    tree = _Tree(root)
    client = _make_app(tmp_path, root)
    _scan(client, root)
    _warm(client)
    state: AppState = client.app.state.reclaim
    assert service.is_candidates_cache_warm(state)
    return client, state, tree, root


# --- 1. the warm cache is actually used (and only when warm) ----------------------------------


def test_warm_cache_replaces_detector_recompute(
    warm_env: tuple[TestClient, AppState, _Tree, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _state, tree, _root = warm_env

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("generate_candidates re-ran although the cache was warm")

    monkeypatch.setattr(service, "generate_candidates", _boom)
    result = _apply(client, [tree.log_a.as_posix()], dry_run=True)
    assert _item(result, tree.log_a)["category_group"] == "large_logs"


def test_cold_cache_still_takes_the_detector_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    tree = _Tree(root)
    client = _make_app(tmp_path, root)
    _scan(client, root)  # scanned but never warmed
    calls: list[int] = []
    real = service.generate_candidates

    def _spy(*a: object, **k: object):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(service, "generate_candidates", _spy)
    result = _apply(client, [tree.log_a.as_posix()], dry_run=True)
    assert calls == [1]
    assert _item(result, tree.log_a)["category_group"] == "large_logs"


# --- 1b. invalidation triggers ----------------------------------------------------------------


def test_new_scan_invalidates(warm_env: tuple[TestClient, AppState, _Tree, Path]) -> None:
    client, state, _tree, root = warm_env
    _scan(client, root)
    assert not service.is_candidates_cache_warm(state)


def test_category_toggle_invalidates(warm_env: tuple[TestClient, AppState, _Tree, Path]) -> None:
    client, state, _tree, _root = warm_env
    assert (
        client.post("/api/settings/categories/duplicates", json={"enabled": True}).status_code
        == 200
    )
    assert not service.is_candidates_cache_warm(state)


def test_category_toggle_never_serves_the_pre_toggle_category(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    """Behavioral #43 guard on the apply path: after disabling large_logs, the warmed
    `large_logs` entry for a stale log must not be what the scoped apply selects."""
    client, _state, tree, _root = warm_env
    before = _apply(client, [tree.log_a.as_posix()], dry_run=True)
    assert _item(before, tree.log_a)["tier"] == "A"  # large_logs enabled, from the warm cache
    response = client.post("/api/settings/categories/large_logs", json={"enabled": False})
    assert response.status_code == 200
    after = _apply(client, [tree.log_a.as_posix()], dry_run=True)
    assert _item(after, tree.log_a)["tier"] == "B"  # a stale cache would still say A


def test_mode_switch_invalidates_in_both_directions(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, state, _tree, _root = warm_env
    assert client.post("/api/mode/safe").status_code == 200
    assert not service.is_candidates_cache_warm(state)  # POWER -> SAFE
    _warm(client)
    assert service.is_candidates_cache_warm(state)
    response = client.post(
        "/api/mode/power", json={"confirmation_text": REQUIRED_POWER_MODE_CONFIRMATION}
    )
    assert response.status_code == 200
    assert not service.is_candidates_cache_warm(state)  # SAFE -> POWER


def test_mode_switch_made_outside_the_api_invalidates(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    """The mode log is shared with the CLI -- no route runs, so only a key that re-reads the
    live mode (not an invalidation call in a handler) can catch this."""
    from reclaim.mode import switch_to_safe_mode

    _client, state, _tree, _root = warm_env
    switch_to_safe_mode(log_path=state.mode_log_path)
    assert not service.is_candidates_cache_warm(state)


def test_config_reload_min_age_and_retention_invalidate(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    _client, state, _tree, root = warm_env
    with state.lock:
        state.config = _config(root, stale_days=400)  # min-age setting
    assert not service.is_candidates_cache_warm(state)

    with state.candidates_cache_lock:  # re-warm under the new config without a scan
        state.candidates_cache = []
        state.candidates_cache_key = service._candidates_cache_key(state)
    assert service.is_candidates_cache_warm(state)
    with state.lock:
        state.config = _config(root, stale_days=400, retention_days=7)  # retention setting
    assert not service.is_candidates_cache_warm(state)


def test_allowed_roots_change_invalidates(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    from reclaim.api.state import ScanStatus

    _client, state, _tree, _root = warm_env
    with state.lock:
        state.scan_status = ScanStatus()  # scan root gone -> allowed roots may shrink/grow
        state.scan_status.root = Path("D:/elsewhere")
        state.scan_status.finished_at = time.time()
    assert not service.is_candidates_cache_warm(state)


# --- 2. teeth: stale cache entry + mutated file is still refused at apply time ----------------


def _untouched(*paths: Path) -> None:
    for p in paths:
        assert p.exists(), f"{p} was touched"


def test_swapped_file_is_skipped_identity_changed_and_nothing_else_touched(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, state, tree, _root = warm_env
    tree.log_a.unlink()
    _write(tree.log_a, b"Z" * 2_000, mtime=_NOW - _OLD_LOG_AGE_DAYS * 86400)  # new inode, same name
    assert service.is_candidates_cache_warm(state)  # the cache entry is stale but still "warm"

    result = _apply(client, [tree.log_a.as_posix()], dry_run=False)

    item = _item(result, tree.log_a)
    assert item["succeeded"] is False
    assert item["skip_reason"] == "identity_changed_since_scan"
    assert tree.log_a.read_bytes() == b"Z" * 2_000
    _untouched(tree.log_b, tree.plain, tree.dup_copy, tree.dup_original, tree.node_modules)


@pytest.mark.parametrize("warm", [False, True])
def test_in_place_append_outcome_is_identical_warm_and_cold(tmp_path: Path, warm: bool) -> None:
    """PRE-EXISTING behavior, pinned for parity rather than endorsed: the top-level file
    identity check (`preflight.check_identity_unchanged_since_scan`) compares only `(dev, ino)`;
    size/mtime are recorded for logging. An in-place append keeps the inode, so the file is NOT
    skipped -- on the cold (detector) path and on the cached path alike. The cache neither adds
    nor removes this gap."""
    root = tmp_path / "tree"
    tree = _Tree(root)
    client = _make_app(tmp_path, root)
    _scan(client, root)
    if warm:
        _warm(client)
        assert service.is_candidates_cache_warm(client.app.state.reclaim)
    with tree.log_a.open("ab") as fh:
        fh.write(b"more")
    result = _apply(client, [tree.log_a.as_posix()], dry_run=False)
    item = _item(result, tree.log_a)
    assert (item["succeeded"], item["skip_reason"]) == (True, None)
    _untouched(tree.log_b, tree.plain)


def test_deleted_file_is_not_reported_as_freed(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, tree, _root = warm_env
    tree.log_a.unlink()
    result = _apply(client, [tree.log_a.as_posix()], dry_run=False)
    item = _item(result, tree.log_a)
    assert item["succeeded"] is False
    assert result["bytes_freed"] == 0
    _untouched(tree.log_b, tree.plain, tree.node_modules)


def test_directory_replaced_by_a_junction_is_skipped(
    warm_env: tuple[TestClient, AppState, _Tree, Path], tmp_path: Path
) -> None:
    client, _state, tree, _root = warm_env
    outside = tmp_path / "outside_target"
    _write(outside / "precious.txt", b"precious")
    import shutil

    shutil.rmtree(tree.node_modules)
    done = subprocess.run(  # noqa: S603 -- fixed test args
        ["cmd", "/c", "mklink", "/J", str(tree.node_modules), str(outside)],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"could not create NTFS junction: {done.stderr or done.stdout}")

    result = _apply(client, [tree.node_modules.as_posix()], dry_run=False)

    item = _item(result, tree.node_modules)
    assert item["succeeded"] is False
    assert item["skip_reason"] is not None
    assert (outside / "precious.txt").read_bytes() == b"precious"
    _untouched(tree.log_a, tree.log_b, tree.plain)


def test_unmutated_cached_file_is_still_applied(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    """Control for the teeth tests: the same flow with NO mutation really applies, so the skips
    above are caused by the mutation, not by the cache path refusing everything."""
    client, _state, tree, _root = warm_env
    result = _apply(client, [tree.log_a.as_posix()], dry_run=False)
    assert _item(result, tree.log_a)["succeeded"] is True
    assert not tree.log_a.exists()
    _untouched(tree.log_b, tree.plain)


# --- 3. unchanged semantics for paths the cache does not cover --------------------------------


def test_path_not_in_the_cache_still_uses_the_fresh_user_selected_path(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, tree, _root = warm_env
    result = _apply(client, [tree.plain.as_posix()], dry_run=True)
    assert _item(result, tree.plain)["category_group"] == "user_selected"


def test_duplicate_member_requested_by_path_keeps_the_user_selected_category(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, tree, _root = warm_env
    result = _apply(client, [tree.dup_copy.as_posix()], dry_run=True)
    assert _item(result, tree.dup_copy)["category_group"] == "user_selected"


# --- 4. warm-status recomputes the key: "ready" only while the cache is actually valid --------


def _warm_status(client: TestClient) -> dict[str, object]:
    response = client.get("/api/candidates/warm-status")
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def test_warm_status_ready_reports_no_stale_reason(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, _tree, _root = warm_env
    body = _warm_status(client)
    assert body["status"] == "ready"
    assert body["stale_reason"] is None


def test_warm_status_goes_stale_with_reason_mode_in_both_directions(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, _tree, _root = warm_env
    assert client.post("/api/mode/safe").status_code == 200
    body = _warm_status(client)
    assert (body["status"], body["stale_reason"]) == ("stale", "mode")  # POWER -> SAFE
    _warm(client)
    assert _warm_status(client)["stale_reason"] is None
    response = client.post(
        "/api/mode/power", json={"confirmation_text": REQUIRED_POWER_MODE_CONFIRMATION}
    )
    assert response.status_code == 200
    body = _warm_status(client)
    assert (body["status"], body["stale_reason"]) == ("stale", "mode")  # SAFE -> POWER
    _warm(client)
    assert _warm_status(client)["status"] == "ready"


def test_warm_status_goes_stale_with_reason_config_on_category_toggle(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, _tree, _root = warm_env
    response = client.post("/api/settings/categories/duplicates", json={"enabled": True})
    assert response.status_code == 200
    body = _warm_status(client)
    assert (body["status"], body["stale_reason"]) == ("stale", "config")
    _warm(client)
    assert _warm_status(client)["status"] == "ready"


def test_warm_status_goes_stale_with_reason_scan_on_new_scan(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    client, _state, _tree, root = warm_env
    _scan(client, root)
    body = _warm_status(client)
    assert (body["status"], body["stale_reason"]) == ("stale", "scan")
    _warm(client)
    assert _warm_status(client)["status"] == "ready"


def test_warm_status_goes_stale_with_reason_scope_on_allowed_roots_change(
    warm_env: tuple[TestClient, AppState, _Tree, Path],
) -> None:
    from reclaim.api.state import ScanStatus

    client, state, _tree, _root = warm_env
    with state.lock:
        state.scan_status = ScanStatus()
        state.scan_status.root = Path("D:/elsewhere")
        state.scan_status.finished_at = time.time()
    body = _warm_status(client)
    assert (body["status"], body["stale_reason"]) == ("stale", "scope")


def test_warm_status_recompute_never_runs_detectors(
    warm_env: tuple[TestClient, AppState, _Tree, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _state, _tree, _root = warm_env

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("warm-status ran a detector -- it must only recompute the key")

    monkeypatch.setattr(service, "generate_candidates", _boom)
    monkeypatch.setattr(service, "generate_duplicate_candidates", _boom)
    assert _warm_status(client)["status"] == "ready"
    assert client.post("/api/mode/safe").status_code == 200
    assert _warm_status(client)["status"] == "stale"


def test_warm_status_non_ready_states_pass_through_without_a_reason(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    _Tree(root)
    client = _make_app(tmp_path, root)
    body = _warm_status(client)  # never warmed
    assert (body["status"], body["stale_reason"]) == ("idle", None)
