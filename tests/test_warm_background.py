"""ADR-0040: the dedup warm-up runs in the background after a full scan, at low priority, and is
cancellable and resumable.

No sleeps, no wall-clock: hashing is counted through a fake `_compute_partial_hash`, the thread an
auto warm-up would get is captured by an injected `warm_spawner` and run synchronously by the test,
and the OS priority call is a recording fake. The tree has 12 size buckets x 2 identical files (all
<= 128 KB, so one partial hash per file and no separate full read): a cold pass == 24 hash calls.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reclaim import dedup
from reclaim.api import security, service
from reclaim.api.app import create_app
from reclaim.api.state import AppState, CandidatesWarmStatus
from reclaim.config import CategoriesConfig, Config, DedupConfig, DuplicatesConfig
from reclaim.index import ScanIndex

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")

_HOST = "127.0.0.1"
_PORT = 8420
_SIZES = 12
_FILES = _SIZES * 2
_WINDOW = 4  # files per dedup window -> 6 windows; checkpoints before and mid each window


def _make_tree(root: Path) -> Path:
    tree = root / "tree"
    for size_index in range(_SIZES):
        for copy in range(2):
            directory = tree / f"d{copy}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"f{size_index}.bin").write_bytes(
                bytes([65 + size_index]) * (1000 + size_index)
            )
    return tree


def _config(*, warm_after_scan: bool = True) -> Config:
    return Config(
        categories=CategoriesConfig(duplicates=DuplicatesConfig(min_reclaim_bytes=0)),
        dedup=DedupConfig(warm_after_scan=warm_after_scan),
    )


def _client(tmp_path: Path, *, warm_after_scan: bool = True) -> TestClient:
    app = create_app(
        db_path=tmp_path / "index.sqlite3",
        config=_config(warm_after_scan=warm_after_scan),
        config_path=tmp_path / "config.toml",
        vault_dir=tmp_path / "vault",
        manifest_path=tmp_path / "manifest.jsonl",
        mode_log_path=tmp_path / "mode_log.jsonl",
        first_run_state_path=tmp_path / "first_run_state.json",
        log_path=tmp_path / "reclaim.log",
        host=_HOST,
        port=_PORT,
    )
    token: str = app.state.reclaim.csrf_token
    return TestClient(
        app, base_url=f"http://{_HOST}:{_PORT}", headers={security.CSRF_HEADER_NAME: token}
    )


class _Env:
    """One app + the seams: spawned warm-up bodies, priority calls, counted hash calls."""

    def __init__(self, client: TestClient, tree: Path) -> None:
        self.client = client
        self.tree = tree
        self.state: AppState = client.app.state.reclaim
        self.spawned: list[Callable[[], None]] = []
        self.priority: list[tuple[bool, int]] = []  # (enable, thread id)
        self.hash_calls: list[Path] = []
        self.state.warm_spawner = self.spawned.append
        self.state.background_mode_setter = self._set_priority

    def _set_priority(self, enable: bool) -> bool:
        self.priority.append((enable, threading.get_ident()))
        return True

    def scan(self) -> None:
        """POST /api/scan of the tree. Whether that is a FULL scan depends on `Path.home()`."""
        assert self.client.post("/api/scan", json={"path": str(self.tree)}).status_code == 202
        assert self.client.get("/api/scan/status").json()["status"] == "completed"

    def hashed_rows(self) -> int:
        with ScanIndex(self.state.db_path) as index:
            row = index._conn.execute("SELECT COUNT(*) FROM files WHERE hash_size IS NOT NULL")
            return int(row.fetchone()[0])

    def warm_status(self) -> dict[str, object]:
        return dict(self.client.get("/api/candidates/warm-status").json())


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    monkeypatch.setattr(dedup, "_WINDOW_FILES", _WINDOW)
    tree = _make_tree(tmp_path)
    # The tree stands in for the user's profile: scanning it is a FULL scan.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tree))
    environment = _Env(_client(tmp_path), tree)
    real = dedup._compute_partial_hash
    lock = threading.Lock()

    def counting(path: Path, size: int) -> str:
        with lock:
            environment.hash_calls.append(path)
        return real(path, size)

    monkeypatch.setattr(dedup, "_compute_partial_hash", counting)
    return environment


_REAL_PARTIAL_HASH = dedup._compute_partial_hash


def _counting_only(env: _Env) -> Callable[[Path, int], str]:
    """A plain counting hasher: replaces any cancel/promote hook installed earlier."""

    def counting(path: Path, size: int) -> str:
        env.hash_calls.append(path)
        return _REAL_PARTIAL_HASH(path, size)

    return counting


def _run_spawned(env: _Env) -> None:
    assert len(env.spawned) == 1
    env.spawned.pop()()


def _cancel_after_hash_calls(env: _Env, monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Request a cancel from inside the n-th hash call: a cancel arriving mid-pass."""
    counting = dedup._compute_partial_hash
    fired = threading.Event()

    def hook(path: Path, size: int) -> str:
        digest = counting(path, size)
        if len(env.hash_calls) >= n and not fired.is_set():
            fired.set()
            service.cancel_candidates_warm(env.state)
        return digest

    monkeypatch.setattr(dedup, "_compute_partial_hash", hook)


# --- (d) auto-start ---------------------------------------------------------------------------


def test_full_scan_auto_starts_a_background_warmup(env: _Env) -> None:
    env.scan()
    assert len(env.spawned) == 1  # a warm-up was handed to its own thread
    status = env.warm_status()
    assert (status["status"], status["source"]) == ("computing", "auto")
    assert env.hash_calls == []  # started, not run inline in the scan

    _run_spawned(env)
    assert env.warm_status()["status"] == "ready"
    assert service.is_candidates_cache_warm(env.state)
    assert len(env.hash_calls) == _FILES


def test_scoped_scan_does_not_auto_start(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.tree.parent))
    assert not service.scan_roots_cover_home([env.tree])
    env.scan()
    assert env.spawned == []
    assert env.warm_status()["status"] == "idle"


def test_flag_off_does_not_auto_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = _make_tree(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tree))
    environment = _Env(_client(tmp_path, warm_after_scan=False), tree)
    environment.scan()
    assert environment.spawned == []
    assert environment.warm_status()["status"] == "idle"


def test_scan_roots_cover_home_means_profile_or_an_ancestor() -> None:
    home = Path("C:/Users/Alice")
    assert service.scan_roots_cover_home([Path("C:/Users/Alice")], home=home)
    assert service.scan_roots_cover_home([Path("c:/")], home=home)  # a whole drive, any case
    assert service.scan_roots_cover_home([Path("D:/x"), Path("C:/Users")], home=home)
    assert not service.scan_roots_cover_home([Path("C:/Users/Alice/Downloads")], home=home)
    assert not service.scan_roots_cover_home([Path("C:/Users/Bob")], home=home)
    assert not service.scan_roots_cover_home([Path("D:/")], home=home)


@pytest.mark.parametrize("relative", [".", "", "sub", "sub/dir"])
def test_a_relative_root_never_counts_as_covering_home(relative: str) -> None:
    home = Path("C:/Users/Alice")
    assert not service.scan_roots_cover_home([Path(relative)], home=home)


def test_startup_tick_warms_a_cold_cache_from_the_persisted_index(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.tree.parent))
    env.scan()  # populate the index; scoped, so nothing auto-started
    assert env.spawned == []
    service._startup_warm_tick(env.state)
    assert len(env.spawned) == 1
    assert env.warm_status()["source"] == "auto"


def test_startup_tick_with_an_empty_index_starts_nothing(env: _Env) -> None:
    service._startup_warm_tick(env.state)
    assert env.spawned == []


def test_lifespan_schedules_the_startup_timer_and_shutdown_cancels_it(
    env: _Env,
) -> None:
    env.state.startup_warm_delay_seconds = 3600.0  # never fires inside the test
    with env.client:  # entering the context runs the app lifespan
        timer = env.state.startup_warm_timer
        assert timer is not None
        assert not timer.finished.is_set()
    assert timer.finished.is_set()  # cancelled on shutdown


def test_shutdown_cancels_a_running_warmup(env: _Env) -> None:
    env.scan()
    assert env.warm_status()["status"] == "computing"
    service.shutdown_candidates_warm(env.state)
    _run_spawned(env)  # the worker observes the cancel at its first boundary and stops
    assert env.warm_status()["status"] == "cancelled"
    assert env.hash_calls == []


# --- (a)(b)(c) cancel + resume ----------------------------------------------------------------


def test_cancel_mid_pass_leaves_cache_cold_and_keeps_committed_hashes(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.scan()
    _cancel_after_hash_calls(env, monkeypatch, 3 * _WINDOW)  # fires in the last read of window 3
    _run_spawned(env)

    status = env.warm_status()
    assert status["status"] == "cancelled"
    assert status["finished_at"] is not None
    assert env.state.candidates_cache is None
    assert env.state.candidates_cache_key is None
    assert not service.is_candidates_cache_warm(env.state)
    assert len(env.hash_calls) == 3 * _WINDOW
    assert env.hashed_rows() == 3 * _WINDOW  # committed, not lost with the cancelled pass


def test_second_pass_after_cancel_hashes_exactly_the_remainder(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.scan()
    _cancel_after_hash_calls(env, monkeypatch, 3 * _WINDOW)
    _run_spawned(env)
    already_hashed = env.hashed_rows()
    env.hash_calls.clear()
    monkeypatch.setattr(dedup, "_compute_partial_hash", _counting_only(env))

    assert env.client.post("/api/candidates/warm").status_code == 202  # runs to completion
    assert env.warm_status()["status"] == "ready"
    assert len(env.hash_calls) == _FILES - already_hashed
    assert len(set(env.hash_calls)) == len(env.hash_calls)


def test_restart_resumes_from_the_same_db_file(
    env: _Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env.scan()
    _cancel_after_hash_calls(env, monkeypatch, 3 * _WINDOW)
    _run_spawned(env)
    committed = env.hashed_rows()
    assert committed == 3 * _WINDOW
    env.hash_calls.clear()
    monkeypatch.setattr(dedup, "_compute_partial_hash", _counting_only(env))

    restarted = _Env(_client(tmp_path), env.tree)  # brand-new app + AppState, same db file
    assert restarted.state is not env.state
    assert restarted.state.candidates_cache is None  # in-memory state did not survive
    assert restarted.client.post("/api/candidates/warm").status_code == 202
    assert restarted.warm_status()["status"] == "ready"
    assert len(env.hash_calls) == _FILES - committed


# --- (e) a new scan cancels a running warm-up -------------------------------------------------


def test_a_new_scan_cancels_a_running_warmup(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.scan()  # leaves an auto warm-up computing (spawned, not yet run)
    assert env.warm_status()["status"] == "computing"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.tree.parent))

    env.scan()  # a second scan starts

    assert env.state.candidates_warm_cancel_event.is_set()
    assert env.warm_status()["cancel_requested"] is True
    _run_spawned(env)  # the stale warm-up's worker observes it at its first boundary
    assert env.warm_status()["status"] == "cancelled"
    assert env.hash_calls == []  # no wasted hashing against the superseded scan


# --- (f) single flight ------------------------------------------------------------------------


def test_auto_then_user_never_runs_two_warmups(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(service, "run_candidates_warm", lambda _s: started.append(1))
    assert service.start_auto_warm(env.state) is True
    assert len(env.spawned) == 1

    response = env.client.post("/api/candidates/warm")  # user asks while auto is computing
    assert response.status_code == 202
    assert response.json()["promoted"] is True
    assert response.json()["source"] == "auto"  # the SAME run, not a second one
    assert service.start_auto_warm(env.state) is False  # nor a second auto
    assert len(env.spawned) == 1
    assert started == []  # the route did not schedule a body of its own


def test_user_then_auto_never_runs_two_warmups(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(service, "run_candidates_warm", lambda _s: started.append(1))
    assert env.client.post("/api/candidates/warm").status_code == 202
    assert started == [1]
    assert service.start_auto_warm(env.state) is False
    assert env.spawned == []
    assert env.client.post("/api/candidates/warm").status_code == 409  # unchanged behavior


def test_auto_warm_is_refused_while_a_scan_is_running(env: _Env) -> None:
    with env.state.lock:
        env.state.scan_status.status = "running"
    assert service.start_auto_warm(env.state) is False
    assert env.spawned == []


def test_auto_warm_skips_when_the_cache_is_already_warm(env: _Env) -> None:
    env.scan()
    _run_spawned(env)
    assert service.is_candidates_cache_warm(env.state)
    assert service.start_auto_warm(env.state) is False


# --- (g) priority seam ------------------------------------------------------------------------


def test_auto_warmup_runs_in_background_mode_on_worker_and_pool_threads(env: _Env) -> None:
    env.scan()
    worker = threading.get_ident()  # the test runs the spawned body on this thread
    _run_spawned(env)

    on_worker = [enable for enable, tid in env.priority if tid == worker]
    on_pool = [enable for enable, tid in env.priority if tid != worker]
    assert on_worker == [True, False]  # entered once, left once, in order
    assert on_pool, "the hashing pool threads must enter background mode too"
    assert all(enable for enable in on_pool)


def test_user_warmup_is_not_background(env: _Env) -> None:
    env.scan()
    env.spawned.clear()
    with env.state.lock:  # turn the auto run into a finished one, then run a user warm-up
        env.state.candidates_warm_status = CandidatesWarmStatus(status="idle")
    assert env.client.post("/api/candidates/warm").status_code == 202
    assert env.priority == []


def test_priority_left_even_when_the_warmup_raises(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.scan()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(service, "generate_candidates", boom)
    _run_spawned(env)
    assert env.warm_status()["status"] == "failed"
    assert [enable for enable, _ in env.priority] == [True, False]


def test_priority_left_even_when_cancelled(env: _Env) -> None:
    env.scan()
    service.cancel_candidates_warm(env.state)
    _run_spawned(env)
    assert env.warm_status()["status"] == "cancelled"
    assert [enable for enable, _ in env.priority] == [True, False]


def test_a_failing_priority_setter_never_breaks_the_warmup(env: _Env) -> None:
    def broken(_enable: bool) -> bool:
        raise OSError("no such API")

    env.state.background_mode_setter = broken
    env.scan()
    _run_spawned(env)
    assert env.warm_status()["status"] == "ready"


def test_promotion_ends_background_mode_at_the_next_batch_boundary(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.scan()
    worker = threading.get_ident()
    promoted_after = 5
    hook_real = dedup._compute_partial_hash
    fired = threading.Event()

    def hook(path: Path, size: int) -> str:
        digest = hook_real(path, size)
        if len(env.hash_calls) >= promoted_after and not fired.is_set():
            fired.set()
            outcome, _ = service.begin_candidates_warm(env.state, source="user")  # user asks
            assert outcome == "promoted"
        return digest

    monkeypatch.setattr(dedup, "_compute_partial_hash", hook)
    _run_spawned(env)

    assert env.warm_status()["status"] == "ready"
    assert env.warm_status()["promoted"] is True
    on_worker = [enable for enable, tid in env.priority if tid == worker]
    assert on_worker == [True, False]  # left once (at the boundary), not twice (the finally)
    # Pool threads started for windows AFTER the promotion never entered background mode. The
    # promotion lands during window 2 (hash call 5), so only windows 1-2 (<= 4 submits each, one
    # thread per submit) can have entered: at most 2 * _WINDOW entries, far below an un-promoted
    # run's up-to-_FILES.
    pool_entries = [tid for enable, tid in env.priority if tid != worker and enable]
    assert 0 < len(pool_entries) <= 2 * _WINDOW


# --- (h) cancel endpoint ----------------------------------------------------------------------


def test_cancel_endpoint_is_a_noop_when_idle_and_idempotent(env: _Env) -> None:
    for _ in range(2):
        response = env.client.post("/api/candidates/warm/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "idle"
        assert body["cancel_requested"] is False
    assert not env.state.candidates_warm_cancel_event.is_set()


def test_cancel_endpoint_cancels_a_running_warmup_and_repeats_safely(env: _Env) -> None:
    env.scan()
    first = env.client.post("/api/candidates/warm/cancel")
    second = env.client.post("/api/candidates/warm/cancel")
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "computing"
    assert first.json()["cancel_requested"] is True
    _run_spawned(env)
    assert env.warm_status()["status"] == "cancelled"
    after = env.client.post("/api/candidates/warm/cancel")
    assert after.status_code == 200
    assert after.json()["status"] == "cancelled"  # nothing running: unchanged


def test_a_new_warmup_can_start_after_a_cancel(env: _Env) -> None:
    env.scan()
    service.cancel_candidates_warm(env.state)
    _run_spawned(env)
    assert env.client.post("/api/candidates/warm").status_code == 202
    assert env.warm_status()["status"] == "ready"
    assert env.state.candidates_warm_cancel_event.is_set() is False


# --- (i) the ADR-0037 typed 409 flow while an auto warm-up computes ---------------------------


def test_typed_409_flow_while_an_auto_warmup_is_computing(env: _Env) -> None:
    env.scan()
    assert env.warm_status()["status"] == "computing"
    # The auto run holds the cache lock while it computes; model that without running it.
    assert env.state.candidates_cache_lock.acquire(blocking=False)
    try:
        response = env.client.get("/api/summary")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "candidates_not_warm"
        assert "not warm" in body["detail"]
        status = env.warm_status()
        assert status["status"] == "computing"  # progress visible, no error
        assert status["error"] is None
        assert status["promoted"] is True  # the courtesy kick promoted the auto run ...
        assert len(env.spawned) == 1  # ... and did not start a second one
    finally:
        env.state.candidates_cache_lock.release()

    _run_spawned(env)
    assert env.client.get("/api/summary").status_code == 200  # retry converges


def test_a_base_exception_in_the_worker_never_leaves_status_computing(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Abort(BaseException):  # stands in for SystemExit / KeyboardInterrupt
        pass

    def abort(*_a: object, **_k: object) -> None:
        raise _Abort

    env.scan()
    monkeypatch.setattr(service, "generate_candidates", abort)
    with pytest.raises(_Abort):
        _run_spawned(env)  # the BaseException still propagates

    assert env.warm_status()["status"] == "failed"
    assert [enable for enable, _ in env.priority] == [True, False]
    monkeypatch.undo()  # a following warm-up runs normally; the mocks above are gone
    assert env.client.post("/api/candidates/warm").status_code == 202


# --- summary precompute ----------------------------------------------------------------------


def _count_physical_size_queries(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    real = ScanIndex.physical_size_bytes_total

    def counting(self: ScanIndex, under: Path | None = None) -> int:
        calls.append(under)
        return real(self, under)

    monkeypatch.setattr(ScanIndex, "physical_size_bytes_total", counting)
    return calls


def test_first_summary_after_a_warmup_is_a_cache_hit(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the first /api/summary after "ready" ran the whole-index physical-size
    aggregate itself (24.1 s on a 7.7 GB index) and outlasted the page's 30 s wait."""
    calls = _count_physical_size_queries(monkeypatch)
    env.scan()
    _run_spawned(env)
    assert env.warm_status()["status"] == "ready"
    assert calls, "the warm-up must pay the aggregate itself"
    during_warm = len(calls)

    assert env.client.get("/api/summary").status_code == 200

    assert len(calls) == during_warm  # the summary found it cached


def test_a_failing_summary_precompute_never_fails_the_warmup(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("simulated aggregate failure")

    env.scan()
    monkeypatch.setattr(service, "cached_physical_size_bytes", boom)
    _run_spawned(env)

    assert env.warm_status()["status"] == "ready"
    assert service.is_candidates_cache_warm(env.state)
