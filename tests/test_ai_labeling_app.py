from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from reclaim.ai.labeling import LabelStore
from reclaim.ai.labeling_app import create_labeling_app
from reclaim.ai.models import AICluster, AIClusterMember, AITrack

_HOST = "127.0.0.1"
_PORT = 8421


def _make_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color=(120, 60, 200)).save(path, format="PNG")


def _make_client(tmp_path: Path) -> tuple[TestClient, list[AICluster], Path]:
    img_a = tmp_path / "a.png"
    img_b = tmp_path / "b.png"
    _make_image(img_a)
    _make_image(img_b)
    cluster = AICluster(
        cluster_id="cluster-0",
        track=AITrack.NEAR_IDENTICAL_IMAGE,
        members=(
            AIClusterMember(path=img_a, size_bytes=img_a.stat().st_size),
            AIClusterMember(path=img_b, size_bytes=img_b.stat().st_size),
        ),
        raw_score=1.0,
        score_kind="hamming_distance",
        rationale="test cluster",
    )
    label_path = tmp_path / "labels.jsonl"
    app = create_labeling_app([cluster], label_store_path=label_path, host=_HOST, port=_PORT)
    client = TestClient(app, base_url=f"http://{_HOST}:{_PORT}")
    return client, [cluster], label_path


def _csrf_token(client: TestClient) -> str:
    token: str = client.app.state.reclaim.csrf_token  # type: ignore[union-attr]
    return token


def test_index_page_lists_the_pending_cluster(tmp_path: Path) -> None:
    client, _clusters, _ = _make_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "cluster-0" in response.text
    assert "1 pending" in response.text


def test_image_route_serves_a_known_member(tmp_path: Path) -> None:
    client, _clusters, _ = _make_client(tmp_path)
    response = client.get("/image/cluster-0/0")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_image_route_404s_for_unknown_cluster(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    response = client.get("/image/does-not-exist/0")
    assert response.status_code == 404


def test_image_route_404s_for_out_of_range_index(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    response = client.get("/image/cluster-0/99")
    assert response.status_code == 404


def test_image_route_cannot_be_used_as_a_generic_path_traversal(tmp_path: Path) -> None:
    """The closed-allowlist design: there is no query parameter or path segment that lets a
    caller name an arbitrary local file — only a literal (cluster_id, member_index) pair
    that was part of THIS run's candidate set is ever servable."""
    client, _, _ = _make_client(tmp_path)
    response = client.get("/image/cluster-0/../../../../Windows/System32/drivers/etc/hosts")
    assert response.status_code in (404, 422)


def test_label_route_records_a_confirmed_decision(tmp_path: Path) -> None:
    client, clusters, label_path = _make_client(tmp_path)
    csrf_token = _csrf_token(client)
    response = client.post(
        "/api/label",
        json={
            "cluster_id": "cluster-0",
            "decision": "confirmed_near_duplicates",
            "keep_path": clusters[0].members[0].path.as_posix(),
        },
        headers={"X-Reclaim-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200

    store = LabelStore(label_path)
    decisions = store.read_all()
    assert len(decisions) == 1
    assert decisions[0].decision == "confirmed_near_duplicates"


def test_label_route_rejects_a_keep_path_not_in_the_cluster(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    csrf_token = _csrf_token(client)
    response = client.post(
        "/api/label",
        json={
            "cluster_id": "cluster-0",
            "decision": "confirmed_near_duplicates",
            "keep_path": "/some/unrelated/path.jpg",
        },
        headers={"X-Reclaim-CSRF-Token": csrf_token},
    )
    assert response.status_code == 400


def test_label_route_rejects_an_invalid_decision_value(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    csrf_token = _csrf_token(client)
    response = client.post(
        "/api/label",
        json={"cluster_id": "cluster-0", "decision": "delete_immediately", "keep_path": None},
        headers={"X-Reclaim-CSRF-Token": csrf_token},
    )
    assert response.status_code == 400


def test_label_route_without_csrf_token_is_rejected(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    response = client.post(
        "/api/label",
        json={"cluster_id": "cluster-0", "decision": "skipped", "keep_path": None},
    )
    assert response.status_code == 403


def test_mismatched_host_header_is_rejected(tmp_path: Path) -> None:
    client, _, _ = _make_client(tmp_path)
    response = client.get("/", headers={"host": "evil.example.com"})
    assert response.status_code == 403
