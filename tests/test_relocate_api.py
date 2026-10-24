from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import reclaim.api.relocate_routes as routes
import reclaim.relocate as relocate
from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.config import Config, ExclusionsConfig

_HOST, _PORT = "127.0.0.1", 8420


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path, Path]:
    app_dir = tmp_path / "app"
    app = create_app(
        db_path=app_dir / "index.sqlite3",
        config=Config(exclusions=ExclusionsConfig(project_names=["zzexclproj"])),
        config_path=app_dir / "config.toml",
        vault_dir=app_dir / "data" / "quarantine",
        manifest_path=app_dir / "data" / "quarantine" / "manifest.jsonl",
        mode_log_path=app_dir / "mode_log.jsonl",
        first_run_state_path=app_dir / "first_run_state.json",
        log_path=app_dir / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    d = tmp_path / "D"
    d.mkdir()
    d_key = relocate._key(d)
    # One test volume: the tmp "D" folder stands in for a second fixed drive.
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )
    monkeypatch.setattr(routes, "_target_drives", lambda _anchor: [d])
    client = TestClient(
        app,
        base_url=f"http://{_HOST}:{_PORT}",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )
    source = tmp_path / "models"
    source.mkdir()
    (source / "w.bin").write_bytes(b"w" * 1000)
    return client, source, d


def _wait(client: TestClient) -> dict[str, object]:
    for _ in range(200):
        job = client.get("/api/relocate/status").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job never finished")


def test_plan_then_move_then_roll_back(setup: tuple[TestClient, Path, Path]) -> None:
    client, source, d = setup
    plan = client.post("/api/relocate/plan", json={"source": str(source)}).json()
    assert plan["ok"], plan
    assert plan["bytes"] == 1000
    assert Path(plan["target"]).is_relative_to(d / "reclaim-relocated")

    started = client.post(
        "/api/relocate/move",
        json={
            "source": plan["source"],
            "target": plan["target"],
            "confirm_token": plan["confirm_token"],
        },
    )
    assert started.status_code == 202
    job = _wait(client)
    assert job["status"] == "done", job
    assert relocate._is_reparse(str(source))

    (move,) = client.get("/api/relocate/moves").json()["moves"]
    assert move["stage"] == "done"
    assert move["delete_old_allowed_at"] == pytest.approx(
        move["swapped_at"] + relocate.KEEP_OLD_COPY_DAYS * 86400
    )
    assert client.post(f"/api/relocate/{move['id']}/rollback").status_code == 202
    assert _wait(client)["status"] == "done"
    assert not relocate._is_reparse(str(source))
    assert (source / "w.bin").read_bytes() == b"w" * 1000


def test_a_move_needs_the_token_of_the_plan_the_user_saw(
    setup: tuple[TestClient, Path, Path],
) -> None:
    client, source, _d = setup
    plan = client.post("/api/relocate/plan", json={"source": str(source)}).json()
    (source / "more.bin").write_bytes(b"m" * 10)  # the folder changed after the review
    stale = client.post(
        "/api/relocate/move",
        json={
            "source": plan["source"],
            "target": plan["target"],
            "confirm_token": plan["confirm_token"],
        },
    )
    assert stale.status_code == 409
    assert "changed since you reviewed it" in stale.json()["detail"]
    forged = client.post(
        "/api/relocate/move",
        json={"source": plan["source"], "target": plan["target"], "confirm_token": "0" * 64},
    )
    assert forged.status_code == 409
    assert not relocate._is_reparse(str(source))


def test_without_the_csrf_token_nothing_starts(setup: tuple[TestClient, Path, Path]) -> None:
    client, source, _d = setup
    plan = client.post("/api/relocate/plan", json={"source": str(source)}).json()
    response = client.post(
        "/api/relocate/move",
        json={
            "source": plan["source"],
            "target": plan["target"],
            "confirm_token": plan["confirm_token"],
        },
        headers={security.CSRF_HEADER_NAME: "wrong"},
    )
    assert response.status_code == 403
    assert not relocate._is_reparse(str(source))


def test_only_folders_in_the_profile_and_targets_on_another_drive(
    setup: tuple[TestClient, Path, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    client, source, _d = setup
    outside = Path(tmp_path_factory.getbasetemp().anchor) / "Windows"
    assert client.post("/api/relocate/plan", json={"source": str(outside)}).status_code == 400
    same_drive = source.parent / "elsewhere"
    response = client.post(
        "/api/relocate/plan", json={"source": str(source), "target": str(same_drive)}
    )
    assert response.status_code == 400
    assert "another fixed drive" in response.json()["detail"]


def test_an_excluded_project_is_refused_with_its_reason(
    setup: tuple[TestClient, Path, Path],
) -> None:
    client, source, _d = setup
    excluded = source.parent / "zzexclproj" / "cache"
    excluded.mkdir(parents=True)
    (excluded / "f").write_bytes(b"x")
    plan = client.post("/api/relocate/plan", json={"source": str(excluded)}).json()
    assert not plan["ok"]
    assert plan["confirm_token"] is None
    assert any("excluded project" in r for r in plan["reasons"])


def test_candidates_are_read_only(setup: tuple[TestClient, Path, Path]) -> None:
    client, _source, d = setup
    data = client.get("/api/relocate/candidates").json()
    assert data["target_drive"] == str(d)
    assert isinstance(data["candidates"], list)
    assert client.get("/api/relocate/moves").json() == {"moves": []}


def test_one_job_at_a_time_and_unknown_ids(setup: tuple[TestClient, Path, Path]) -> None:
    client, _source, _d = setup
    assert client.post("/api/relocate/nope/rollback").status_code == 404
    assert client.post("/api/relocate/..%2F..%2Fx/rollback").status_code in {400, 404}
    assert client.get("/api/relocate/status").json()["status"] == "idle"


def test_rollback_with_the_old_copy_and_the_kept_flag(setup: tuple[TestClient, Path, Path]) -> None:
    client, source, _d = setup
    plan = client.post("/api/relocate/plan", json={"source": str(source)}).json()
    client.post(
        "/api/relocate/move",
        json={
            "source": plan["source"],
            "target": plan["target"],
            "confirm_token": plan["confirm_token"],
        },
    )
    assert _wait(client)["status"] == "done"
    (source / "w.bin").write_bytes(b"edited through the junction")
    (move,) = client.get("/api/relocate/moves").json()["moves"]
    assert move["old_copy_kept"] is True
    assert client.post(f"/api/relocate/{move['id']}/rollback?use_old_copy=true").status_code == 202
    assert _wait(client)["status"] == "done"
    assert (source / "w.bin").read_bytes() == b"w" * 1000  # the copy from before the move
    (move,) = client.get("/api/relocate/moves").json()["moves"]
    assert move["stage"] == "rolled_back" and move["old_copy_kept"] is False
    assert Path(plan["target"], "w.bin").read_bytes() == b"edited through the junction"
