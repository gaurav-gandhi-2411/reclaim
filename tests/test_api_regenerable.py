from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim import regenerable
from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.config import Config
from reclaim.regenerable import CommandResult, RegenerableEnv

_HOST = "127.0.0.1"
_PORT = 8765
NOW = 2_000_000_000.0
DAY = 86400.0


def _client(tmp_path: Path) -> TestClient:
    # Deliberately SAFE mode (empty mode log): the whole point of ADR-0034 is that the
    # regenerable tier frees real space even in the default mode, where apply_batch cannot.
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    return TestClient(
        app,
        base_url=f"http://{_HOST}:{_PORT}",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )


@pytest.fixture
def fake_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    temp = home / "AppData" / "Local" / "Temp"
    temp.mkdir(parents=True)
    old = temp / "old.bin"
    old.write_bytes(b"x" * 2048)
    os.utime(old, (NOW - 30 * DAY, NOW - 30 * DAY))
    fresh = temp / "fresh.bin"
    fresh.write_bytes(b"y" * 100)
    os.utime(fresh, (NOW - 1 * DAY, NOW - 1 * DAY))

    def env() -> RegenerableEnv:
        return RegenerableEnv(
            home=home,
            local_appdata=home / "AppData" / "Local",
            temp_roots=(temp,),
            crash_dump_roots=(),
            now=lambda: NOW,
            running_process_names=lambda: frozenset(),
            which=lambda _name: None,
            run_command=lambda *_a: CommandResult(0, "", ""),
            has_open_handle=lambda _p: False,
            disk_anchor=tmp_path,
        )

    monkeypatch.setattr(service, "regenerable_clean_env", env)
    monkeypatch.setattr(regenerable, "DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    return temp


def test_one_click_frees_space_in_safe_mode_and_reports_it(
    tmp_path: Path, fake_machine: Path
) -> None:
    client = _client(tmp_path)

    response = client.post("/api/clean/regenerable", json={"apply": True})

    assert response.status_code == 200
    body = response.json()
    assert body["apply"] is True
    assert body["bytes_removed"] == 2048
    assert body["bytes_removed_human"] == "2.0 KB"
    temp_item = next(i for i in body["items"] if i["key"] == "temp0")
    assert temp_item["status"] == "cleaned" and temp_item["files_removed"] == 1
    assert not (fake_machine / "old.bin").exists(), "really deleted, not moved to a Recycle Bin"
    assert (fake_machine / "fresh.bin").exists()
    assert body["disk_free_delta_bytes"] is not None
    assert body["percent_used_after"] is not None
    assert (tmp_path / "audit.jsonl").exists()


def test_preview_deletes_nothing(tmp_path: Path, fake_machine: Path) -> None:
    client = _client(tmp_path)

    body = client.post("/api/clean/regenerable", json={"apply": False}).json()

    assert body["apply"] is False
    assert body["bytes_removed"] == 2048
    assert (fake_machine / "old.bin").exists()
    assert body["disk_free_delta_bytes"] is None


def test_endpoint_takes_no_paths_from_the_client(tmp_path: Path, fake_machine: Path) -> None:
    client = _client(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")

    response = client.post("/api/clean/regenerable", json={"apply": True, "paths": [str(victim)]})

    assert response.status_code == 422, "extra fields are forbidden, not silently ignored"
    assert victim.exists()


def test_requires_csrf_token(tmp_path: Path, fake_machine: Path) -> None:
    client = _client(tmp_path)
    client.headers.pop(security.CSRF_HEADER_NAME)

    response = client.post("/api/clean/regenerable", json={"apply": True})

    assert response.status_code == 403
    assert (fake_machine / "old.bin").exists()


def test_second_concurrent_clean_is_refused_with_409(tmp_path: Path, fake_machine: Path) -> None:
    client = _client(tmp_path)
    assert service._regenerable_clean_lock.acquire(blocking=False)
    try:
        response = client.post("/api/clean/regenerable", json={"apply": True})
    finally:
        service._regenerable_clean_lock.release()

    assert response.status_code == 409
    assert (fake_machine / "old.bin").exists()
