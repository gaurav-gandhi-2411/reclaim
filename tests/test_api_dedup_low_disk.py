"""ADR-0040 addendum: the dedup disk guard applies only when a window has files to hash.

Regression (verifier D1): the guard ran unconditionally at the start of every
`find_duplicate_clusters` call, and `GET /api/candidates` / `GET /api/duplicate-clusters/review`
call that on every read with a HOT hash cache. With under 2 GB free they answered HTTP 500 --
in exactly the near-full-disk situation this app exists for. Through the real API (TestClient).
"""

from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path

import pytest
from test_api import (
    _build_tree,
    _config,
    _make_app,
    _scan_and_wait,
    _warm_candidates_and_wait,
)

from reclaim import dedup

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_Usage = namedtuple("_Usage", "total used free")


def _count_hash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real_partial, real_full = dedup._compute_partial_hash, dedup._compute_full_hash

    def partial(path: Path, size: int) -> str:
        calls.append(path)
        return real_partial(path, size)

    def full(path: Path) -> str:
        calls.append(path)
        return real_full(path)

    monkeypatch.setattr(dedup, "_compute_partial_hash", partial)
    monkeypatch.setattr(dedup, "_compute_full_hash", full)
    return calls


def _low_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dedup.shutil, "disk_usage", lambda _p: _Usage(10**12, 10**12 - 10**6, 10**6)
    )


def test_warm_cache_reads_succeed_on_a_nearly_full_disk_with_zero_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root, duplicates_enabled=True))
    _scan_and_wait(client, root)
    _warm_candidates_and_wait(client)  # real hashing happens here, disk still fine

    calls = _count_hash_calls(monkeypatch)
    _low_disk(monkeypatch)

    candidates = client.get("/api/candidates")
    review = client.get("/api/duplicate-clusters/review")

    assert candidates.status_code == 200, candidates.text
    assert review.status_code == 200, review.text
    assert len(review.json()["clusters"]) == 1
    assert calls == []  # fully cached: nothing was hashed, so the guard never had a reason to fire


def test_cold_cache_on_a_nearly_full_disk_fails_the_warmup_readably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root, duplicates_enabled=True))
    _scan_and_wait(client, root)
    calls = _count_hash_calls(monkeypatch)
    _low_disk(monkeypatch)

    assert client.post("/api/candidates/warm").status_code == 202
    status = client.get("/api/candidates/warm-status").json()

    assert status["status"] == "failed", status
    assert "not enough free disk space" in status["error"]
    assert "Free up space" in status["error"]
    assert calls == []  # it refused before reading a single file


def test_review_endpoint_returns_a_typed_non_500_when_the_guard_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    _build_tree(root)
    client = _make_app(tmp_path, config=_config(root, duplicates_enabled=True))
    _scan_and_wait(client, root)  # hash cache is cold: the review call must hash, and cannot
    _low_disk(monkeypatch)

    response = client.get("/api/duplicate-clusters/review")

    # With the warm-cache reads (review-clusters PR) a cold cache answers the typed 409 and starts
    # the courtesy warm-up, which is what meets the guard; the 503 handler in the route is the
    # backstop for a guard that fires on the read path itself. Never a 500 either way.
    assert response.status_code in (409, 503), response.text
    body = response.json()
    if response.status_code == 409:
        assert body["code"] == "candidates_not_warm"
        status = client.get("/api/candidates/warm-status").json()
        assert status["status"] == "failed", status
        assert "not enough free disk space" in status["error"]
    else:
        assert body["code"] == "dedup_insufficient_disk"
        assert "not enough free disk space" in body["detail"]


def test_message_does_not_round_to_a_self_contradiction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free is 1 byte under the threshold: GB rounding used to print '2.0 GB free, need 2.0 GB'."""
    from reclaim.index import ScanIndex

    monkeypatch.setattr(dedup, "_compute_partial_hash", lambda _p, _s: "0" * 64)
    db = tmp_path / "i.sqlite3"
    with ScanIndex(db) as index:
        from test_dedup_bounded_wal import _record

        index.upsert_records(
            [_record("C:/a/x.bin", 5000), _record("C:/a/y.bin", 5000)], scanned_at=1.0
        )
        with pytest.raises(dedup.DedupAborted) as err:
            dedup.find_duplicate_clusters(
                index,
                min_reclaim_bytes=0,
                disk_usage=lambda _p: _Usage(10**12, 0, dedup._MIN_FREE_DISK_BYTES - 1),
            )
    message = str(err.value)
    assert f"{dedup._MIN_FREE_DISK_BYTES - 1:,} bytes free" in message
    assert "need at least" in message and "MB" not in message  # exact bytes, no rounded tie
