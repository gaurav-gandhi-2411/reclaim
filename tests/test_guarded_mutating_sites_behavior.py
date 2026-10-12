from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from reclaim import (
    anthropic_key_store,
    autoclean_schedule,
    autoclean_state,
    config,
    executor,
    first_run,
    logging_config,
    mode,
    notifications,
    regenerable,
    safety_env,
)
from reclaim.ai.category_explainer import CategoryExplanation, _write_cache
from reclaim.ai.feedback_store import FeedbackStore
from reclaim.ai.image_embeddings import ImageEmbeddingCache
from reclaim.ai.labeling import LabelStore
from reclaim.index import ScanIndex
from reclaim.safety_env import RealProfileAccessError

# Behavior of the sites wired by the guard-all-mutating-sites change (ADR-0034 addendum). Every
# directory is under tmp_path: the "real profile" is a STAND-IN root and "sandbox" is a sibling.
# A broken guard therefore mutates only a tmp dir, which the tests assert did not happen.


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(stand-in real root, sandbox dir). Swaps the guard's captured roots for this test only."""
    real = tmp_path / "standin-real-profile"
    sandbox = tmp_path / "sandbox"
    real.mkdir()
    sandbox.mkdir()
    monkeypatch.setattr(safety_env, "_real_roots", (safety_env._norm(real),))
    monkeypatch.setattr(safety_env, "_sandbox_roots", [safety_env._norm(sandbox)])
    monkeypatch.setattr(safety_env, "_sandbox_inherited", True)
    monkeypatch.delenv(safety_env.OPT_IN_ENV_VAR, raising=False)
    return real, sandbox


def _entries(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir())


def _refuses(call: Callable[[Path], object], real: Path, name: str = "target.dat") -> None:
    before = _entries(real)
    with pytest.raises(RealProfileAccessError):
        call(real / "sub" / name)
    assert _entries(real) == before, "a refused call must not create anything"


def test_config_writers_refuse_real_and_write_in_sandbox(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    for setter in (
        lambda p: config.set_category_enabled(p, "crash_dumps", enabled=True),
        lambda p: config.set_notifications_enabled(p, enabled=True),
        lambda p: config.set_autoclean_enabled(p, enabled=True),
    ):
        _refuses(setter, real, "config.toml")
    config.set_autoclean_enabled(sandbox / "c" / "config.toml", enabled=True)
    assert (sandbox / "c" / "config.toml").is_file()


def test_first_run_acknowledge(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    _refuses(lambda p: first_run.acknowledge(p), real)
    first_run.acknowledge(sandbox / "x" / "fr.json", now=1.0)
    assert (sandbox / "x" / "fr.json").is_file()


def test_notifications_save_state(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    state = notifications.NotificationState()
    _refuses(lambda p: notifications.save_state(state, p), real)
    notifications.save_state(state, sandbox / "n" / "s.json")
    assert (sandbox / "n" / "s.json").is_file()


def test_autoclean_state_write(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    state = autoclean_state.AutoCleanState()
    _refuses(lambda p: autoclean_state.write_state(state, p), real)
    autoclean_state.write_state(state, sandbox / "a" / "st.json")
    assert (sandbox / "a" / "st.json").is_file()


def test_store_key(world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    real, sandbox = world
    monkeypatch.setattr(anthropic_key_store, "protect", lambda b: b)  # no real DPAPI call
    _refuses(lambda p: anthropic_key_store.store_key("k", p), real)
    anthropic_key_store.store_key("k", sandbox / "k" / "key.bin")
    assert (sandbox / "k" / "key.bin").read_bytes() == b"k"


def test_manifest_writers(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    _refuses(lambda p: executor.append_manifest_entries(p, []), real, "manifest.jsonl")
    _refuses(lambda p: executor._open_manifest_for_sync(p), real, "manifest.jsonl")
    executor.append_manifest_entries(sandbox / "m" / "manifest.jsonl", [])
    assert (sandbox / "m" / "manifest.jsonl").is_file()
    fh = executor._open_manifest_for_sync(sandbox / "m2" / "manifest.jsonl")
    fh.close()


def test_rmtree_clear_readonly_refuses_before_chmod(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    real, _ = world
    chmods: list[object] = []
    monkeypatch.setattr(os, "chmod", lambda *a, **k: chmods.append(a))
    victim = real / "ro.txt"
    with pytest.raises(RealProfileAccessError):
        executor.rmtree_clear_readonly(lambda p: None, str(victim), PermissionError())
    assert chmods == []


def test_diag_log_and_task_xml_tempdir(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    real, sandbox = world
    outcome = autoclean_schedule.SchtasksOutcome(0, "")
    _refuses(lambda p: autoclean_schedule._append_diag("a", "c", outcome, p), real)
    autoclean_schedule._append_diag("a", "c", outcome, sandbox / "d" / "diag.log")
    assert (sandbox / "d" / "diag.log").is_file()

    fake_exe = sandbox / "reclaim.exe"
    fake_exe.write_bytes(b"")
    runs: list[object] = []
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(real))
    with pytest.raises(RealProfileAccessError):
        autoclean_schedule.register_task(
            exe_path=fake_exe,
            username="u",
            runner=lambda argv: runs.append(argv) or outcome,
            diag_log_path=sandbox / "d" / "diag2.log",
        )
    assert runs == [] and _entries(real) == []


def test_logging_mode_audit_labels_cache_refuse(world: tuple[Path, Path]) -> None:
    real, _ = world
    _refuses(lambda p: logging_config.configure_logging(p), real, "reclaim.log")
    _refuses(lambda p: mode._append_mode_change(p, object()), real)  # type: ignore[arg-type]
    _refuses(lambda p: regenerable._write_audit(object(), p), real)  # type: ignore[arg-type]
    _refuses(lambda p: FeedbackStore(p).append(object()), real)  # type: ignore[arg-type]
    _refuses(lambda p: LabelStore(p).append(object()), real)  # type: ignore[arg-type]
    _refuses(lambda p: _write_cache(p, CategoryExplanation("g", "e", False)), real)


def test_image_embedding_cache_open(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    _refuses(lambda p: ImageEmbeddingCache(p), real, "emb.sqlite3")
    ImageEmbeddingCache(sandbox / "e" / "emb.sqlite3")
    assert (sandbox / "e" / "emb.sqlite3").is_file()


def test_scan_index_open_refuses_real_and_works_in_sandbox(world: tuple[Path, Path]) -> None:
    real, sandbox = world
    _refuses(lambda p: ScanIndex(p), real, "idx.sqlite3")
    with ScanIndex(sandbox / "idx.sqlite3") as index:
        assert index.delete_paths([]) == 0


def test_scan_index_destructive_methods_refuse_on_real_db(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened legitimately in the sandbox, then the guard's view flips so the DB location counts
    as real: prune/delete/VACUUM/replace must each refuse before any SQL runs."""
    _, sandbox = world
    with ScanIndex(sandbox / "idx.sqlite3") as index:
        index.begin_scan_tracking()
        monkeypatch.setattr(safety_env, "_real_roots", (safety_env._norm(sandbox),))
        monkeypatch.setattr(safety_env, "_sandbox_roots", [])
        executes: list[object] = []
        real_conn = index._conn

        class _Spy:
            def execute(self, sql: str, *a: object) -> object:
                executes.append(sql)
                return real_conn.execute(sql, *a)

            def executemany(self, sql: str, *a: object) -> object:
                executes.append(sql)
                return real_conn.executemany(sql, *a)

            def __getattr__(self, name: str) -> object:
                return getattr(real_conn, name)

        index._conn = _Spy()  # type: ignore[assignment]
        with pytest.raises(RealProfileAccessError):
            index.prune_missing(["a", "b"], ["a"])
        with pytest.raises(RealProfileAccessError):
            index.prune_unseen_under_root(Path("/x"))
        with pytest.raises(RealProfileAccessError):
            index.delete_paths(["a"])
        with pytest.raises(RealProfileAccessError):
            index.vacuum()
        with pytest.raises(RealProfileAccessError):
            index.replace_inaccessible_under_root(Path("/x"), [], scanned_at=0.0)
        assert executes == [], "no SQL may run once the guard refused"
        index._conn = real_conn
