"""`GET /api/duplicate-clusters/review` draws from the warm candidates cache (ADR-0037 addendum
"review clusters"; incident 2026-10-08).

The endpoint used to run `find_duplicate_clusters` + `generate_duplicate_candidates` itself on
every request: an uncached whole-index BLAKE3 pass (~30 min on the owner's 1.6 M-candidate
index) that competed with the warm-up for the same index and killed it with
`database table is locked`. Now the warm pass computes the clusters ONCE, caches them with the
candidates, and this endpoint answers 409 `candidates_not_warm` until then.

Every test is hermetic (tmp_path only, no sleeps for timing; threads synchronise on Events).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest
from test_api import _build_tree, _config, _make_app, _scan_and_wait, _write

from reclaim import index as index_module
from reclaim.api import service
from reclaim.api.schemas import DuplicateClusterReviewResponse
from reclaim.index import ScanIndex

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


def _cold_client(tmp_path: Path, root: Path):  # type: ignore[no-untyped-def]
    """A scanned app whose candidates cache is verifiably COLD (no auto-warm has run), with the
    409 NOT auto-retried by the test client."""
    client = _make_app(tmp_path, config=_config(root))
    client.retry_not_warm = False  # type: ignore[attr-defined]
    _scan_and_wait(client, root)
    state = client.app.state.reclaim  # type: ignore[attr-defined]
    assert state.candidates_cache is None, "fixture must start cold"
    assert state.candidates_clusters_cache is None
    return client, state


def _count_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Wraps `service.<name>`, counting calls; returns a one-element list holding the count."""
    calls = [0]
    real = getattr(service, name)

    def _counting(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(service, name, _counting)
    return calls


# (i) -------------------------------------------------------------------------------------------


def test_review_during_an_in_flight_warm_is_409_and_never_starts_a_second_dedup_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, _state = _cold_client(tmp_path, root)

    calls = [0]
    started = threading.Event()
    release = threading.Event()
    real = service.find_duplicate_clusters

    def _blocking(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        started.set()
        assert release.wait(timeout=30), "warm-up never released"
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "find_duplicate_clusters", _blocking)

    # TestClient runs a route's background task inside the request: the warm needs its own thread
    # for the review request to overlap it (same pattern as tests/test_api.py's blocked-warm test).
    warm_thread = threading.Thread(target=lambda: client.post("/api/candidates/warm"))
    warm_thread.start()
    assert started.wait(timeout=10), "warm-up never reached the dedup pass"

    response = client.get("/api/duplicate-clusters/review")
    release.set()
    warm_thread.join(timeout=60)
    assert not warm_thread.is_alive()

    assert response.status_code == 409
    assert response.json()["code"] == "candidates_not_warm"
    assert calls[0] == 1, "only the warm-up's own dedup pass may run"


# (ii) ------------------------------------------------------------------------------------------


def test_review_after_warm_serves_from_cache_without_recomputing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, state = _cold_client(tmp_path, root)
    clusters_calls = _count_calls(monkeypatch, "find_duplicate_clusters")
    candidates_calls = _count_calls(monkeypatch, "generate_duplicate_candidates")

    assert client.post("/api/candidates/warm").status_code == 202
    assert state.candidates_warm_status.status == "ready"
    assert clusters_calls[0] == 1
    assert state.candidates_clusters_cache is not None

    first = client.get("/api/duplicate-clusters/review")
    second = client.get("/api/duplicate-clusters/review")
    assert first.status_code == second.status_code == 200
    assert len(first.json()["clusters"]) == 1
    assert first.json() == second.json()
    assert clusters_calls[0] == 1, "the review endpoint ran its own dedup pass"
    assert candidates_calls[0] == 1, "the review endpoint regenerated candidates"


# (iii) -----------------------------------------------------------------------------------------


def _legacy_list_duplicate_cluster_review(
    state: service.AppState, *, limit: int = 15
) -> DuplicateClusterReviewResponse:
    """The implementation this PR replaced, verbatim apart from the name: its own uncached
    `find_duplicate_clusters` + `generate_duplicate_candidates` pass on a private connection."""
    with ScanIndex(state.db_path) as index:
        if not index.has_any_records():
            return DuplicateClusterReviewResponse(has_scan=False, clusters=[])
        config = state.effective_config
        clusters = service.find_duplicate_clusters(
            index,
            min_reclaim_bytes=config.categories.duplicates.min_reclaim_bytes,
            exclusion_patterns=state.safety.exclusion_patterns,
        )
        duplicate_candidates = service.generate_duplicate_candidates(
            index, config, state.safety, clusters=clusters
        )
        candidate_by_path = {c.path: c for c in duplicate_candidates}

    rows: list[service.DuplicateClusterReviewOut] = []
    for cluster in clusters:
        surviving = tuple(d for d in cluster.duplicates if d.path in candidate_by_path)
        if not surviving:
            continue
        member_candidates = [candidate_by_path[d.path] for d in surviving]
        display = service._dataclass_replace(cluster, duplicates=surviving)
        total = sum(service._effective_reclaimable_bytes(c) for c in member_candidates)
        rows.append(
            service.DuplicateClusterReviewOut(
                cluster=service._duplicate_cluster_out(display),
                reclaimable_bytes=total,
                reclaimable_bytes_human=service.format_bytes(total),
                needs_review=service.cluster_needs_manual_review(display),
                rationale=member_candidates[0].rationale,
            )
        )
    rows.sort(key=lambda row: row.reclaimable_bytes, reverse=True)
    return DuplicateClusterReviewResponse(has_scan=True, clusters=rows[:limit])


def _build_golden_tree(root: Path) -> None:
    _build_tree(root)  # Archive/report.bin + Downloads/report_copy.bin (genuine copies)

    # Hardlinks: two names of one inode plus an independent copy -> reclaimable 0 for the link.
    hard = root / "Archive" / "hard.bin"
    _write(hard, b"h" * 8_192)
    (root / "Other").mkdir(parents=True, exist_ok=True)
    os.link(hard, root / "Other" / "hard_link.bin")
    _write(root / "Downloads" / "hard_copy.bin", b"h" * 8_192)

    # ADR-0008: an HF-cache member is dropped per-member and must never be displayed.
    hf_content = b"hf-bytes-" * 1_000
    _write(root / "Archive" / "model.bin", hf_content)
    _write(root / "Downloads" / "model_copy.bin", hf_content)
    _write(
        root / ".cache" / "huggingface" / "hub" / "models--o--n" / "snapshots" / "rev" / "m.bin",
        hf_content,
    )

    # ADR-0007: a protected (BLOCKED) member excludes the whole cluster.
    protected = b"protected-" * 700
    # The shallow copy wins keep, so the protected one is a non-kept BLOCKED member.
    _write(root / "blocked_top.bin", protected)
    _write(root / "Windows" / "sub" / "blocked.bin", protected)
    _write(root / "Archive" / "blocked_a.bin", protected)


@pytest.mark.parametrize("force_needs_review", [False, True])
def test_review_output_equals_the_previous_implementation_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_needs_review: bool
) -> None:
    root = tmp_path / "tree"
    _build_golden_tree(root)
    client, state = _cold_client(tmp_path, root)
    if force_needs_review:
        # The `True` branch of `cluster_needs_manual_review` is unreachable through the real
        # pipeline (see its docstring); force it for the hardlink cluster only so the
        # `needs_review` field is covered in both states. Both implementations see the patch.
        monkeypatch.setattr(
            service, "cluster_needs_manual_review", lambda c: "hard" in c.keep.path.as_posix()
        )
    assert client.post("/api/candidates/warm").status_code == 202

    new = client.get("/api/duplicate-clusters/review?limit=15")
    assert new.status_code == 200
    old = _legacy_list_duplicate_cluster_review(state, limit=15)

    assert new.content == client.get("/api/duplicate-clusters/review?limit=15").content
    assert new.json() == old.model_dump(mode="json")
    rows = new.json()["clusters"]
    members = {m["path"] for row in rows for m in row["cluster"]["members"]}
    assert len(rows) == 3, "teeth: plain copies + hardlink group + HF-filtered group"
    assert not [p for p in members if "huggingface" in p], "ADR-0008 member displayed"
    assert not [p for p in members if "blocked" in p], "ADR-0007 blocked cluster displayed"
    assert {
        (root / "Other" / "hard_link.bin").as_posix(),
        (root / "Archive" / "hard.bin").as_posix(),
    } <= members, "teeth: hardlink cluster present"
    if force_needs_review:
        assert {row["needs_review"] for row in rows} == {True, False}


# (iv) ------------------------------------------------------------------------------------------


def test_a_cancelled_warm_leaves_no_clusters_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    _client, state = _cold_client(tmp_path, root)

    def _cancel_at_first_boundary(*_args: object, **kwargs: object) -> object:
        state.candidates_warm_cancel_event.set()
        kwargs["checkpoint"]()  # type: ignore[operator]  # raises DedupCancelled
        raise AssertionError("checkpoint did not cancel")

    monkeypatch.setattr(service, "find_duplicate_clusters", _cancel_at_first_boundary)
    assert service.begin_candidates_warm(state, source="user")[0] == "started"
    service.run_candidates_warm(state)

    assert state.candidates_warm_status.status == "cancelled"
    assert state.candidates_cache is None
    assert state.candidates_clusters_cache is None
    assert state.candidates_cache_key is None


def test_a_failed_warm_leaves_no_clusters_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    _client, state = _cold_client(tmp_path, root)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "find_duplicate_clusters", _boom)
    assert service.begin_candidates_warm(state, source="user")[0] == "started"
    service.run_candidates_warm(state)

    assert state.candidates_warm_status.status == "failed"
    assert state.candidates_cache is None
    assert state.candidates_clusters_cache is None


def test_category_toggle_clears_the_clusters_cache_with_the_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # update_category_setting persists to a cwd-relative config.toml; without this the test wrote
    # into the checkout (and the hermetic guard refuses that when the checkout is under the home).
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tree"
    _build_tree(root)
    client, state = _cold_client(tmp_path, root)
    assert client.post("/api/candidates/warm").status_code == 202
    assert state.candidates_clusters_cache is not None
    service.update_category_setting(state, "large_logs", enabled=False)
    assert state.candidates_cache is None
    assert state.candidates_clusters_cache is None


# (v) -------------------------------------------------------------------------------------------


def test_warm_failure_reports_the_original_error_not_the_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    _client, state = _cold_client(tmp_path, root)

    def _locked(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    real_close = ScanIndex.close

    def _failing_close(self: ScanIndex) -> None:
        real_close(self)  # release the handle, then fail like the checkpoint did in the incident
        raise sqlite3.OperationalError("database table is locked")

    monkeypatch.setattr(service, "find_duplicate_clusters", _locked)
    monkeypatch.setattr(ScanIndex, "close", _failing_close)
    assert service.begin_candidates_warm(state, source="user")[0] == "started"
    logs: list[dict[str, object]] = []

    class _Recorder:
        """The repo's loggers are cached lazily, so structlog's capture_logs cannot see them."""

        def __init__(self, name: str) -> None:
            self.name = name

        def __getattr__(self, level: str):  # type: ignore[no-untyped-def]
            return lambda event, **kw: logs.append({"event": event, **kw})

    monkeypatch.setattr(service, "logger", _Recorder("service"))
    monkeypatch.setattr(index_module, "logger", _Recorder("index"))
    service.run_candidates_warm(state)

    assert state.candidates_warm_status.status == "failed"
    assert state.candidates_warm_status.error == "database is locked"
    failed = [e for e in logs if e["event"] == "api.candidates_warm_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "database is locked"
    assert failed[0]["exc_info"] is True, "the traceback must be logged"
    secondary = [e for e in logs if e["event"] == "index.close_failed_while_handling_exception"]
    assert [e["error"] for e in secondary] == ["database table is locked"]


# Other readers that used to run a private dedup pass (verifier finding D1) ----------------------


def _blocked_warm(
    client: object, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[int], threading.Event, threading.Thread]:
    """Starts a warm-up on its own thread, blocked inside its (counted) dedup pass."""
    calls = [0]
    started = threading.Event()
    release = threading.Event()
    real = service.find_duplicate_clusters

    def _blocking(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        started.set()
        assert release.wait(timeout=30), "warm-up never released"
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "find_duplicate_clusters", _blocking)
    thread = threading.Thread(target=lambda: client.post("/api/candidates/warm"))  # type: ignore[attr-defined]
    thread.start()
    assert started.wait(timeout=10), "warm-up never reached the dedup pass"
    return calls, release, thread


def test_category_explanation_during_an_in_flight_warm_is_409_without_a_second_dedup_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, _state = _cold_client(tmp_path, root)
    calls, release, warm_thread = _blocked_warm(client, monkeypatch)

    response = client.get("/api/ai/category-explanation/duplicates")
    release.set()
    warm_thread.join(timeout=60)

    assert response.status_code == 409
    assert response.json()["code"] == "candidates_not_warm"
    assert calls[0] == 1, "only the warm-up's own dedup pass may run"


def test_category_explanation_after_warm_does_not_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, _state = _cold_client(tmp_path, root)
    calls = _count_calls(monkeypatch, "find_duplicate_clusters")
    assert client.post("/api/candidates/warm").status_code == 202
    response = client.get("/api/ai/category-explanation/duplicates")
    assert response.status_code == 200
    assert calls[0] == 1


def test_mcp_selector_during_an_in_flight_warm_reuses_it_instead_of_a_second_dedup_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, state = _cold_client(tmp_path, root)
    calls, release, warm_thread = _blocked_warm(client, monkeypatch)

    selected: list[object] = []
    selector = threading.Thread(
        target=lambda: selected.extend(
            service.select_candidates_for_selector(
                state, tier="both", rule_id_or_category="duplicates"
            )
        )
    )
    selector.start()
    release.set()
    warm_thread.join(timeout=60)
    selector.join(timeout=60)
    assert not selector.is_alive()

    assert calls[0] == 1, "the selector started its own dedup pass"
    assert len(selected) == 1, "selector output differs from the warm candidates"


# Verifier finding D3 -----------------------------------------------------------------------------


def test_candidates_cached_without_clusters_count_as_cold_and_the_next_warm_recomputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client, state = _cold_client(tmp_path, root)
    calls = _count_calls(monkeypatch, "find_duplicate_clusters")
    assert client.post("/api/candidates/warm").status_code == 202
    assert service.is_candidates_cache_warm(state)
    assert calls[0] == 1

    state.candidates_clusters_cache = None  # candidates cached, clusters missing
    assert not service.is_candidates_cache_warm(state)
    assert service.candidates_cache_stale_reason(state) == "cold"
    with pytest.raises(service.CandidatesNotWarmError):
        service.require_warm_candidates(state)

    assert client.post("/api/candidates/warm").status_code == 202
    assert calls[0] == 2, "the warm POST must recompute the missing clusters"
    assert state.candidates_clusters_cache is not None
    assert client.get("/api/duplicate-clusters/review").status_code == 200
