from __future__ import annotations

# ruff: noqa: F811
# Tests of the fixes for the ninth hostile review (B9; ADR-0044 addendum 9) beyond the
# reviewer's own probes (`test_relocate_review_b9.py`): what the 8.3 refusal says, and the rerun
# of an interrupted use_old_copy rollback. Every root is under `tmp_path` (the `env` fixture).
import ctypes
from pathlib import Path

import pytest
from test_relocate import (  # type: ignore[import-not-found]
    BIG,
    Env,
    SimulatedCrash,
    _exists,
    _make_tree,
    _manifest,
    _write,
    env,  # noqa: F401 -- the fixture
)

import reclaim.relocate as relocate
from reclaim.relocate import Journal, RelocateError, Stage, execute_move, rollback


def _short(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else ""


def test_a_short_name_is_refused_with_the_full_name_and_nothing_is_renamed(env: Env) -> None:
    long_src = env.c / "longfoldername-data"
    _make_tree(long_src)
    short = _short(long_src)
    if not short or Path(short).name.casefold() == long_src.name.casefold():
        pytest.skip("8.3 names are disabled on this volume")
    plan = relocate.plan_move(short, env.target, env.policy, free_bytes=lambda _p: BIG)
    assert not plan.ok
    assert any("short or alternate name" in r and str(long_src) in r for r in plan.reasons)
    with pytest.raises(RelocateError, match="full name"):
        execute_move(short, env.target, env.policy, env.journals, free_bytes=lambda _p: BIG)
    assert sorted(p.name for p in env.c.iterdir()) == ["longfoldername-data"]
    # The same folder by its own name, in another case, plans normally.
    upper = env.c / "LONGFOLDERNAME-DATA"
    assert relocate.plan_move(upper, env.target, env.policy, free_bytes=lambda _p: BIG).ok


def test_a_rerun_of_an_interrupted_use_old_copy_rollback_says_what_it_put_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B9 L2: killed after the old copy was back but before the journal said so, the rerun must
    finish as the first run would: name the copy from before the move, and leave the moved
    folder (with what was written there since the move) untouched."""
    _make_tree(env.source)
    before_move = _manifest(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    real = Journal.log

    def die_at_the_end(self: Journal, stage: str, *args: object, **kwargs: object) -> None:
        if stage == Stage.ROLLED_BACK:
            raise SimulatedCrash("before the journal recorded the rollback")
        real(self, stage, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(Journal, "log", die_at_the_end)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path(), use_old_copy=True)
    assert _manifest(env.source) == before_move  # the old copy is already back
    out = rollback(env.journal_path(), use_old_copy=True)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert "put back the copy from before the move" in out.message, out.message
    assert str(env.target) in out.message and "left as it is" in out.message
    assert _exists(env.target)
    assert (env.target / "after-move.txt").read_bytes() == b"written through the junction"
