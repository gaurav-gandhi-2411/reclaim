from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim import autoclean_schedule
from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.autoclean_schedule import SchtasksOutcome
from reclaim.config import Config, load_config

_HOST = "127.0.0.1"
_PORT = 8765

_QUERY_OUTPUT = """
TaskName:                             \\Reclaim Weekly Auto-Clean (someone)
Next Run Time:                        10/4/2026 10:00:00 AM
Status:                               Ready
Last Run Time:                        9/27/2026 10:00:01 AM
Last Result:                          0
Scheduled Task State:                 Enabled
"""


class FakeSchtasks:
    """A tiny in-memory Task Scheduler: create registers, delete removes, query reports."""

    def __init__(self, *, fail_create: bool = False) -> None:
        self.registered = False
        self.fail_create = fail_create
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> SchtasksOutcome:
        self.calls.append(list(argv))
        verb = argv[0]
        if verb == "/create":
            if self.fail_create:
                return SchtasksOutcome(1, "ERROR: Access is denied.")
            self.registered = True
            return SchtasksOutcome(0, "SUCCESS")
        if verb == "/delete":
            if not self.registered:
                return SchtasksOutcome(1, "ERROR: The system cannot find the file specified.")
            self.registered = False
            return SchtasksOutcome(0, "SUCCESS")
        if verb == "/query":
            if not self.registered:
                return SchtasksOutcome(1, "ERROR: The system cannot find the file specified.")
            return SchtasksOutcome(0, _QUERY_OUTPUT)
        raise AssertionError(f"unexpected schtasks call {argv}")


def _client(tmp_path: Path, config_path: Path) -> TestClient:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=Config(),
        config_path=config_path,
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
def fake_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSchtasks:
    fake = FakeSchtasks()
    monkeypatch.setattr(service, "autoclean_schtasks_runner", lambda: fake)
    monkeypatch.setattr(service, "autoclean_exe_path", lambda: Path(r"C:\Apps\reclaim.exe"))
    monkeypatch.setattr(
        autoclean_schedule, "default_diagnostic_log_path", lambda: tmp_path / "diag.log"
    )
    return fake


def test_get_defaults_to_off_with_no_task(tmp_path: Path, fake_tasks: FakeSchtasks) -> None:
    client = _client(tmp_path, tmp_path / "config.toml")

    body = client.get("/api/settings/autoclean").json()

    assert body["enabled"] is False
    assert body["task_registered"] is False
    assert body["task_name"].startswith("Reclaim Weekly Auto-Clean (")
    assert body["task_state"] is None and body["next_run_time"] is None


def test_enabling_persists_registers_and_reports_task_state(
    tmp_path: Path, fake_tasks: FakeSchtasks
) -> None:
    config_path = tmp_path / "config.toml"
    client = _client(tmp_path, config_path)

    response = client.post("/api/settings/autoclean", json={"enabled": True})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["task_registered"] is True
    assert body["task_state"] == "Ready"
    assert body["last_result"] == 0
    assert body["next_run_time"] == "10/4/2026 10:00:00 AM"
    assert fake_tasks.registered is True
    assert load_config(config_path).autoclean.enabled is True
    assert client.get("/api/settings/autoclean").json()["enabled"] is True


def test_disabling_unregisters_and_persists(tmp_path: Path, fake_tasks: FakeSchtasks) -> None:
    config_path = tmp_path / "config.toml"
    client = _client(tmp_path, config_path)
    client.post("/api/settings/autoclean", json={"enabled": True})

    body = client.post("/api/settings/autoclean", json={"enabled": False}).json()

    assert body["enabled"] is False and body["task_registered"] is False
    assert fake_tasks.registered is False
    assert load_config(config_path).autoclean.enabled is False


def test_registration_failure_is_409_and_config_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tasks: FakeSchtasks
) -> None:
    fake_tasks.fail_create = True
    config_path = tmp_path / "config.toml"
    client = _client(tmp_path, config_path)

    response = client.post("/api/settings/autoclean", json={"enabled": True})

    assert response.status_code == 409
    assert "Access is denied" in response.json()["detail"]
    assert load_config(config_path).autoclean.enabled is False, "never 'on' without a task"
    assert client.get("/api/settings/autoclean").json()["enabled"] is False


def test_dev_build_gets_actionable_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tasks: FakeSchtasks
) -> None:
    # No exe seam: a source run has no reclaim.exe to schedule (compiled_exe_dir() is None).
    monkeypatch.setattr(service, "autoclean_exe_path", lambda: None)
    config_path = tmp_path / "config.toml"
    client = _client(tmp_path, config_path)

    response = client.post("/api/settings/autoclean", json={"enabled": True})

    assert response.status_code == 409
    assert "installed Reclaim app" in response.json()["detail"]
    assert fake_tasks.calls == [], "no schtasks call is made for a non-installed build"
    assert not config_path.exists() or load_config(config_path).autoclean.enabled is False


def test_post_requires_csrf_and_rejects_extra_fields(
    tmp_path: Path, fake_tasks: FakeSchtasks
) -> None:
    client = _client(tmp_path, tmp_path / "config.toml")

    assert (
        client.post("/api/settings/autoclean", json={"enabled": True, "exe": "x"}).status_code
        == 422
    )
    client.headers.pop(security.CSRF_HEADER_NAME)
    assert client.post("/api/settings/autoclean", json={"enabled": True}).status_code == 403
    assert fake_tasks.registered is False
