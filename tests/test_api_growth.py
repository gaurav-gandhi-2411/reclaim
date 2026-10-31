from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim.api import security
from reclaim.api.app import create_app
from reclaim.config import Config
from reclaim.growth import GIB, GrowthStore, Measurement, Root, Snapshot

pytestmark = pytest.mark.skipif(os.name != "nt", reason="volume paths are Windows paths")

_HOST = "127.0.0.1"
_PORT = 8422

# GET /api/growth (ADR-0046): read-only, from a temp growth store. Every path is under tmp_path;
# the real data dir, profile and index are never touched.

VOL = "C:\\"
SCRATCH = "C:\\Users\\g\\AppData\\Local\\Temp\\claude\\proj\\sess"
TOP = "C:\\Users\\g\\work"
OPAQUE = "C:\\Users\\g\\work\\app\\node_modules"
EXCLUDED = Root("<excluded projects>", "excluded", "Excluded projects")


def _client(tmp_path: Path, store: Path | None, config: Config | None = None) -> TestClient:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=config or Config(),
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        anthropic_key_path=tmp_path / "anthropic_key.bin",
        ai_explanation_cache_dir=tmp_path / "ai_explanations",
        growth_db_path=store,
        host=_HOST,
        port=_PORT,
    )
    return TestClient(
        app,
        base_url=f"http://{_HOST}:{_PORT}",
        headers={security.CSRF_HEADER_NAME: app.state.reclaim.csrf_token},
    )


def _write_series(path: Path, *, age_h: float = 0.0, extra_partial: bool = False) -> None:
    """Hourly snapshots over 26 h ending `age_h` + 0.5 h ago. At the 20th hour C: used and the
    scratch folder jump 14 / 9 GiB (so a 1d window sees +14 GiB on the volume), the excluded
    aggregate grows 3 GiB, the opaque root 2 GiB. `extra_partial` adds a final snapshot where one
    folder is cut off by the budget."""
    now = time.time() - age_h * 3600
    with GrowthStore(path) as store:
        for k in range(27):
            jumped = k >= 20
            sizes = [
                Measurement(Root(VOL, "volume", VOL), (114 if jumped else 100) * GIB),
                Measurement(Root(VOL + "|total", "volume_total", VOL), 500 * GIB),
                Measurement(
                    Root(SCRATCH, "scratch", "proj\\sess", "proj", "sess"),
                    (10 if jumped else 1) * GIB,
                ),
                Measurement(Root(TOP, "toplevel", "work"), (30 if jumped else 29) * GIB),
                Measurement(Root(OPAQUE, "opaque", "node_modules"), (7 if jumped else 5) * GIB),
                Measurement(EXCLUDED, (8 if jumped else 5) * GIB),
            ]
            store.write(Snapshot(now - 26.5 * 3600 + k * 3600, 120.0, True, sizes))
        if extra_partial:
            cut = Measurement(Root(TOP, "toplevel", "work"), 1 * GIB, partial=True)
            store.write(Snapshot(now - 0.2 * 3600, 120.0, False, [cut]))


def test_no_store_is_200_with_empty_rows_and_insufficient_data(tmp_path: Path) -> None:
    client = _client(tmp_path, tmp_path / "absent" / "growth.sqlite3")
    response = client.get("/api/growth")
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1d" and body["status"] == "insufficient data"
    assert body["rows"] == [] and body["volumes"] == [] and body["anomalies"] == []
    assert body["excluded_aggregate"] == {"label": "Excluded projects", "bytes": 0}
    assert not (tmp_path / "absent").exists()  # a GET never creates the store


def test_window_report_contract(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    _write_series(store, extra_partial=True)
    body = _client(tmp_path, store).get("/api/growth?window=1d").json()
    assert set(body) == {
        "window",
        "generated_at",
        "status",
        "volumes",
        "rows",
        "excluded_aggregate",
        "anomalies",
    }
    assert body["status"] == "ok" and body["generated_at"].endswith("+00:00")
    (vol,) = body["volumes"]
    # +14 GiB used. The opaque root (+2) lies inside the top-level folder (+1): only the larger
    # of the nested pair counts, so attributed = scratch 9 + opaque 2 = 11, not attributed = 3.
    assert vol["volume"] == VOL and vol["status"] == "ok" and vol["change_bytes"] == 14 * GIB
    assert (vol["attributed_bytes"], vol["not_attributed_bytes"]) == (11 * GIB, 3 * GIB)
    rows = {r["path"]: r for r in body["rows"]}
    assert [r["path"] for r in body["rows"]] == [SCRATCH, OPAQUE, TOP]  # largest growth first
    scratch = rows[SCRATCH]
    assert (scratch["kind"], scratch["project"], scratch["session"]) == ("scratch", "proj", "sess")
    assert scratch["growth_bytes"] == 9 * GIB and scratch["partial"] is False
    assert scratch["review_link"] == "/api/candidates?tier=both&category=temp_and_browser_caches"
    assert rows[OPAQUE]["review_link"] == "/api/candidates?tier=both&category=dev_artifacts"
    assert rows[TOP]["review_link"] is None  # no category owns a top-level folder
    assert rows[TOP]["partial"] is True  # its newest measurement was cut off by the budget
    # excluded projects: one aggregate line, never a name
    assert body["excluded_aggregate"] == {"label": "Excluded projects", "bytes": 3 * GIB}
    assert "<excluded projects>" not in str(body["rows"])
    (anomaly,) = body["anomalies"]
    assert anomaly["scope"] == "volume" and anomaly["attributed"] == SCRATCH
    assert anomaly["growth_bytes"] == 14 * GIB


def test_seven_day_window_and_volume_filter(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    _write_series(store)
    client = _client(tmp_path, store)
    week = client.get("/api/growth?window=7d").json()
    assert week["window"] == "7d" and week["rows"] == []  # only 26 h of history
    assert week["status"] == "insufficient data"
    assert [v["status"] for v in week["volumes"]] == ["insufficient data"]
    d = client.get("/api/growth?window=1d&volume=C:").json()
    assert [v["volume"] for v in d["volumes"]] == [VOL] and len(d["rows"]) == 3
    other = client.get("/api/growth?volume=D:").json()
    assert other["rows"] == [] and other["status"] == "insufficient data"
    assert [(v["volume"], v["status"]) for v in other["volumes"]] == [("D:", "insufficient data")]


def test_stale_store_says_insufficient_data_not_a_number(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    _write_series(store, age_h=48.0)  # the laptop was off: the newest sample is 2 days old
    body = _client(tmp_path, store).get("/api/growth").json()
    assert body["rows"] == [] and body["anomalies"] == []
    assert [v["status"] for v in body["volumes"]] == ["insufficient data"]
    assert body["status"] == "insufficient data"


@pytest.mark.parametrize("query", ["window=3d", "window=", "volume=C:%5Cx", "volume=CC"])
def test_invalid_parameters_are_422(tmp_path: Path, query: str) -> None:
    response = _client(tmp_path, None).get(f"/api/growth?{query}")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)  # FastAPI's standard validation shape


def test_get_never_modifies_the_store(tmp_path: Path) -> None:
    store = tmp_path / "growth.sqlite3"
    _write_series(store)
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    client = _client(tmp_path, store)
    assert client.get("/api/growth?window=1d").status_code == 200
    assert client.get("/api/growth?window=7d").status_code == 200
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    assert client.post("/api/growth").status_code == 405  # read-only: no mutating verb exists


# ---------------------------------------------------------------- exclusions on the read side

EXCLUDED_VENV = "C:\\Users\\g\\ml-projects\\secret-app\\.venv"


def _write_opaque_under_secret_app(path: Path) -> None:
    """Three snapshots 13 h apart: C: +14 GiB, an opaque root under the project `secret-app`
    +9 GiB, i.e. the one that would be named as the cause of the volume's growth."""
    now = time.time()
    with GrowthStore(path) as store:
        for k in range(3):
            t = now - 26 * 3600 + k * 13 * 3600
            store.write(
                Snapshot(
                    t,
                    120.0,
                    True,
                    [
                        Measurement(Root(VOL, "volume", VOL), (100 + 14 * (k > 0)) * GIB),
                        Measurement(Root(VOL + "|total", "volume_total", VOL), 500 * GIB),
                        Measurement(
                            Root(EXCLUDED_VENV, "opaque", ".venv"),
                            (1 + 9 * (k > 0)) * GIB,
                            False,
                            t,
                        ),
                    ],
                )
            )


def _excluding(*names: str) -> Config:
    cfg = Config()
    cfg.exclusions.project_names = list(names)
    return cfg


def test_opaque_row_under_an_excluded_project_is_never_named(tmp_path: Path) -> None:
    # F2a (verifier probe test_api_names_excluded_opaque): the row, the volume anomaly's
    # `attributed` path and its explanation all came from the stored opaque key.
    store = tmp_path / "growth.sqlite3"
    _write_opaque_under_secret_app(store)
    control = _client(tmp_path, store).get("/api/growth?window=1d").json()
    assert "secret-app" in str(control).lower()  # the scenario does name it without exclusions
    body = _client(tmp_path, store, _excluding("Secret-App")).get("/api/growth?window=1d").json()
    assert "secret-app" not in str(body).lower()
    assert body["rows"] == []
    (vol,) = body["volumes"]
    assert vol["change_bytes"] == 14 * GIB and vol["not_attributed_bytes"] == 14 * GIB


def test_exclusion_added_after_snapshots_exist_hides_old_keys(tmp_path: Path) -> None:
    # the keys were stored while nothing was excluded; the user then excluded the project
    store = tmp_path / "growth.sqlite3"
    _write_series(store)
    names = {r["path"] for r in _client(tmp_path, store).get("/api/growth").json()["rows"]}
    assert {SCRATCH, TOP, OPAQUE} <= names
    body = _client(tmp_path, store, _excluding("work")).get("/api/growth").json()
    assert {r["path"] for r in body["rows"]} == {SCRATCH}
    assert "work" not in str(body["anomalies"]).lower()
    # the aggregate line is untouched: it is its own series, never itemised
    assert body["excluded_aggregate"]["bytes"] == 3 * GIB


@pytest.mark.parametrize(
    "content", [b"", b"this is not an sqlite database " * 20], ids=["empty", "foreign"]
)
def test_get_on_an_empty_or_foreign_file_is_insufficient_data_and_leaves_it_untouched(
    tmp_path: Path, content: bytes
) -> None:
    # D5: the API used to open every existing file read-write and give it the schema, so a GET
    # turned a 0-byte placeholder into a database (and failed on a foreign file).
    store = tmp_path / "growth.sqlite3"
    store.write_bytes(content)
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    body = _client(tmp_path, store).get("/api/growth?window=1d&volume=C:").json()
    assert body["status"] == "insufficient data" and body["rows"] == []
    assert [v["status"] for v in body["volumes"]] == ["insufficient data"]
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    assert store.stat().st_size == len(content)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("growth")) == [
        "growth.sqlite3"
    ]  # no -journal / -wal sidecar either
