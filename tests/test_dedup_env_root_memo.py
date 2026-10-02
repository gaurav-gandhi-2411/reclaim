"""The per-pass `_environment_root` memo must change speed, never an answer."""

from __future__ import annotations

from pathlib import Path

import pytest

import reclaim.dedup as dedup_module
from reclaim.models import FileRecord


def _record(path: Path) -> FileRecord:
    return FileRecord(
        path=path,
        is_dir=False,
        size_bytes=10,
        attributes=0,
        ext=path.suffix.lower(),
        git_repo_root=None,
        git_repo_clean=False,
        mtime=1.0,
        ctime=1.0,
        dev=1,
        ino=0,
    )


def _make_venv(root: Path) -> Path:
    venv = root / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x")
    return venv


def test_environment_root_cache_gives_identical_answers_with_one_probe_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-clustering eligibility used to re-probe every ancestor for every member (91% of the
    candidate-generation time on a real index); a per-pass cache must not change a single answer."""
    venv = _make_venv(tmp_path / "proj")
    files = [venv / "Lib" / "site-packages" / "pkg" / f"m{i}.py" for i in range(5)]
    files.append(tmp_path / "proj" / "src" / "plain.py")
    probes: list[Path] = []
    real = dedup_module._has_site_packages
    monkeypatch.setattr(
        dedup_module, "_has_site_packages", lambda d: (probes.append(d), real(d))[1]
    )
    uncached = [dedup_module._environment_root(f) for f in files]
    uncached_probes = len(probes)
    probes.clear()
    cache: dict[Path, bool] = {}
    cached = [dedup_module._environment_root(f, cache) for f in files]
    assert cached == uncached
    assert uncached[0] == venv
    assert len(probes) < uncached_probes
    assert len(probes) == len(set(probes))  # each directory probed at most once


def test_cross_environment_verdicts_are_identical_with_a_shared_cache(tmp_path: Path) -> None:
    """One cache shared across members of different environments (and a non-environment keep)
    must give the same `_is_cross_environment_duplicate` answer as no cache at all."""
    venv_a = _make_venv(tmp_path / "a")
    venv_b = _make_venv(tmp_path / "b")
    in_a = _record(venv_a / "Lib" / "site-packages" / "x.py")
    in_a2 = _record(venv_a / "Lib" / "site-packages" / "y.py")
    in_b = _record(venv_b / "Lib" / "site-packages" / "x.py")
    outside = _record(tmp_path / "elsewhere" / "x.py")
    pairs = [(in_a, in_a2), (in_a, in_b), (in_a, outside), (outside, in_a), (in_b, in_a)]
    shared: dict[Path, bool] = {}
    for duplicate, keep in pairs:
        assert dedup_module._is_cross_environment_duplicate(
            duplicate, keep, shared
        ) == dedup_module._is_cross_environment_duplicate(duplicate, keep)


def test_memo_is_per_call_scope_a_venv_created_later_is_seen_by_a_fresh_cache(
    tmp_path: Path,
) -> None:
    """The memo is scoped to one pass (a dict the caller owns), never module-level, so an
    environment created after one pass is still found by the next."""
    target = tmp_path / "proj" / ".venv" / "Lib" / "site-packages" / "m.py"
    assert dedup_module._environment_root(target, {}) is None
    venv = _make_venv(tmp_path / "proj")
    assert dedup_module._environment_root(target, {}) == venv
