from __future__ import annotations

# ruff: noqa: F811, PTH104
# Tests for the three mutants that survived the mutation round on the B7/B8 fixes (mutate_rl7:
# Y6, Y9, Y17). Every root is under `tmp_path` (the `env` fixture); nothing here touches a real
# profile.
import os
from pathlib import Path

import pytest
from test_relocate import (  # type: ignore[import-not-found]
    Env,
    SimulatedCrash,
    _is_link,
    _make_tree,
    _manifest,
    _write,
    _x,
    env,  # noqa: F401 -- the fixture
)

import reclaim.relocate as relocate
from reclaim.relocate import Journal, RelocateError, Stage, delete_old_copy, recover, rollback


def _moved(e: Env) -> Path:
    return Path(str(e.source) + relocate.MOVED_SUFFIX)


def _restore(e: Env) -> Path:
    return Path(str(e.source) + relocate.RESTORE_SUFFIX)


def test_a_kill_during_the_final_sync_leaves_the_restore_unverified(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y6: the final sync changes the restore folder. A kill in the middle of it must not leave
    the journal calling that half-synced folder verified, or a later rollback with the target
    drive gone would put it back as a whole copy of the moved folder."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    full = _manifest(env.target)

    def half_sync(_moved_dir: str, restore: str, _started: int) -> None:
        victim = next(p for p in sorted(Path(_x(Path(restore))).rglob("*")) if p.is_file())
        victim.unlink()
        raise SimulatedCrash("in the middle of the final sync")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_delta_sync", half_sync)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert not Journal.load(env.journal_path()).restore_verified
    os.rename(_x(env.target), _x(env.d / "unplugged"))
    try:
        out = rollback(env.journal_path())
    except RelocateError:
        return  # refusing is a correct answer: the restore copy is not a proved copy
    assert _manifest(env.source) == full, out.message


def test_recover_rolls_back_when_the_old_copy_is_gone(env: Env) -> None:
    """Y9: with the old copy gone there is nothing to prove that the listings differ only because
    programs wrote through the junction (an empty comparison is no proof), so the conservative
    path, a rollback, is taken. What the program wrote is kept."""
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    assert Journal.load(env.journal_path()).stage == Stage.SWAPPING
    _write(env.source / "written-after-restart.log", b"kept")
    os.rename(_x(_moved(env)), _x(env.d / "old-copy-moved-away"))
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.ROLLED_BACK, out.message
    assert not _is_link(env.source)
    assert (env.source / "written-after-restart.log").read_bytes() == b"kept"


def test_a_raw_os_error_in_rollback_itself_is_a_relocate_error_and_a_rerun_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y17: `rollback` called directly (the CLI and the API do), not only through `recover`,
    turns an OSError no step handled into a message naming the move and the rerun."""
    _make_tree(env.source)
    env.move()
    real = relocate._remove_junction
    state = {"fail": True}

    def held(link: str) -> None:
        if state["fail"]:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", link)
        real(link)

    monkeypatch.setattr(relocate, "_remove_junction", held)
    journal_id = Journal.load(env.journal_path()).id
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path())
    text = str(info.value)
    assert journal_id in text and "relocate rollback" in text and "again" in text
    assert _is_link(env.source)  # the path still points to the data
    state["fail"] = False
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source)
