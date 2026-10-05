from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scratch_index.py"


def _load() -> ModuleType:
    """Standalone scripts/ file -- load by path (same convention as test_check_dist_dll_closure)."""
    spec = importlib.util.spec_from_file_location("scratch_index", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scratch_index"] = module
    spec.loader.exec_module(module)
    return module


si = _load()


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    conn.commit()
    conn.close()
    return path


def test_copy_is_deleted_with_its_sidecars_and_its_directory_on_success(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    dest = tmp_path / "scratch"
    with si.scratch_index_copy(src, dest_dir=dest) as db:
        assert db.parent == dest and db.is_file()
        # What a crashed/ANALYZEd copy leaves behind: sidecar files next to the db.
        for suffix in ("-wal", "-shm", "-journal"):
            db.with_name(db.name + suffix).write_bytes(b"x" * 1024)
    assert not dest.exists()


def test_copy_is_deleted_when_the_body_raises(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    dest = tmp_path / "scratch"
    with pytest.raises(RuntimeError, match="boom"), si.scratch_index_copy(src, dest_dir=dest) as db:
        assert db.is_file()
        raise RuntimeError("boom")
    assert not dest.exists()


def test_the_source_is_never_modified_or_removed(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    before = src.read_bytes()
    with (
        si.scratch_index_copy(src, dest_dir=tmp_path / "s") as db,
        contextlib.closing(
            sqlite3.connect(db)
        ) as conn,  # closed: Windows can't unlink an open file
    ):
        conn.execute("DELETE FROM t")
        conn.commit()
    assert src.read_bytes() == before


def test_refuses_when_free_space_is_insufficient_and_leaves_nothing(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    dest = tmp_path / "scratch"
    with (
        pytest.raises(si.InsufficientSpaceError),
        si.scratch_index_copy(src, dest_dir=dest, min_free_multiple=1e12),
    ):
        raise AssertionError("must not be entered")
    assert not dest.exists()


def test_a_preexisting_destination_directory_is_kept_but_emptied(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    dest = tmp_path / "mine"
    dest.mkdir()
    (dest / "keep.txt").write_text("x")
    with si.scratch_index_copy(src, dest_dir=dest) as db:
        assert db.is_file()
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]


def test_missing_source_is_a_clear_error(tmp_path: Path) -> None:
    with (
        pytest.raises(FileNotFoundError, match="index not found"),
        si.scratch_index_copy(tmp_path / "nope.sqlite3"),
    ):
        pass


def test_cli_substitutes_db_runs_command_returns_its_exit_code_and_cleans_up(
    tmp_path: Path,
) -> None:
    src = _make_db(tmp_path / "real.sqlite3")
    dest = tmp_path / "scratch"
    code = (
        "import sqlite3,sys;"
        "n=sqlite3.connect(sys.argv[1]).execute('select count(*) from t').fetchone()[0];"
        "sys.exit(0 if n==1000 else 3)"
    )
    rc = si.main([str(src), "--dest-dir", str(dest), "--", sys.executable, "-c", code, "{DB}"])
    assert rc == 0
    assert not dest.exists()
    rc = si.main(
        [str(src), "--dest-dir", str(dest), "--", sys.executable, "-c", "raise SystemExit(7)"]
    )
    assert rc == 7
    assert not dest.exists()
