from __future__ import annotations

# ruff: noqa: F811, PTH104
# Hostile-review probes for reclaim.relocate (verifier B7, head 9474aa8). Each test asserts a
# safety claim; a failure on the current code demonstrates the finding. The "_holds" tests
# are interaction checks that are expected to pass (evidence for what held up).
import os
from pathlib import Path

import pytest
from test_relocate import (  # type: ignore[import-not-found]
    BIG,
    Env,
    SimulatedCrash,
    _exists,
    _is_link,
    _make_tree,
    _manifest,
    _write,
    _x,
    env,  # noqa: F401 -- the fixture
)
from test_relocate_review_b4 import (  # type: ignore[import-not-found]
    _moved,
    _stage,
    _vol,
)
from test_relocate_review_b6 import _arm6, _restore  # type: ignore[import-not-found]

import reclaim.relocate as relocate
from reclaim.cli import main
from reclaim.relocate import (
    Journal,
    RelocateError,
    Stage,
    delete_old_copy,
    plan_move,
    recover,
    rollback,
)


def test_b7_a_partial_restore_is_never_put_back_as_the_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old copy deleted (DELETED_OLD). The copy-back is cut by the target drive going away
    (a raw FileNotFoundError: _rebuild_restore's errors are not wrapped), leaving a PARTIAL
    restore whose restore_id is already recorded. The user retries with the drive still gone:
    _put_back_old_copy takes the restore as "a complete copy" (only its ID is checked), renames
    it into place and reports ROLLED_BACK, 'put back the copy from before the move'."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    full = _manifest(env.target)
    unplugged = env.d / "unplugged"
    real = relocate._copy_file
    n = {"i": 0}

    def copy(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))):
            n["i"] += 1
            if n["i"] == 3:
                os.rename(_x(env.target), _x(unplugged))  # the drive goes away mid-copy
                raise FileNotFoundError(3, "The system cannot find the path specified", src)
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_copy_file", copy)
        with pytest.raises(RelocateError):
            rollback(env.journal_path())
    assert _exists(_restore(env))
    try:
        out = rollback(env.journal_path())
    except RelocateError:
        return  # refusing is a correct answer
    got = _manifest(env.source)
    assert got == full, (
        f"reported {out.journal.stage}: {out.message!r}, but {len(full) - len(got)} of "
        f"{len(full)} entries are missing from {env.source}"
    )


def test_b7_a_held_old_copy_at_put_back_never_leaves_the_path_empty(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rollback(use_old_copy=True): _put_back_old_copy removes the junction, then renames the
    old copy into place. A scanner handle inside the old copy fails the rename: the original
    path is left EMPTY and the PermissionError escapes raw (the CLI catches only RelocateError).
    B6 M3 fixed exactly this in the copy-back branch, not here."""
    _make_tree(env.source)
    env.move()
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", rename)
    with pytest.raises(Exception) as info:
        rollback(env.journal_path(), use_old_copy=True)
    assert _exists(env.source), f"nothing at {env.source} after {info.value!r}"
    assert _is_link(env.source), "the junction was not restored"
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}"


def test_b7_a_failed_move_whose_rollback_rename_is_blocked_gives_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move fails after the freeze; its own rollback renames the old copy back, and a
    scanner handle in it blocks that rename. execute_move then raises a raw PermissionError,
    the folder sits at <name>.reclaim-moved, and nothing says to run recover (the CLI prints a
    traceback with no move id)."""
    _make_tree(env.source)

    def boom(*_a: object, **_k: object) -> str:
        raise RelocateError("simulated: 1 files differ after copying them twice")

    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    monkeypatch.setattr(relocate, "_delta_sync", boom)
    monkeypatch.setattr(relocate, "_rename", rename)
    with pytest.raises(Exception) as info:
        env.move()
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}: {info.value}"


def test_b7_a_full_journal_drive_after_the_freeze_gives_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal and manifest live on the drive being freed (data_root on C:), which is
    often the full one; the plan never checks room for them. Once a save fails after the
    freeze, the move's rollback cannot save either: a raw OSError escapes, the folder is left
    at <name>.reclaim-moved with nothing at its path, and no message says what to do."""
    _make_tree(env.source)
    real_save = relocate.Journal.save

    def save(self: Journal) -> None:
        if any(h[1] == Stage.FROZEN.value for h in self.history) and self.stage != "frozen":
            raise OSError(28, "There is not enough space on the disk")
        real_save(self)

    monkeypatch.setattr(relocate.Journal, "save", save)
    with pytest.raises(Exception) as info:
        env.move()
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}: {info.value}"


def _journal_paths(e: Env) -> list[Path]:
    return sorted(e.journals.glob("relocate-*.json"))


@pytest.mark.parametrize("delete_first", [False, True])
def test_b7_sweep_a_kill_in_the_copy_back_then_the_target_drive_goes_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_first: bool
) -> None:
    """A plain rollback killed at every point, then the target becomes unreachable, then
    rollback again. Whatever is reported as rolled back must be a whole folder: the copy from
    before the move when the message says so, never a partial copy-back."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"t{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        pre_move = _manifest(e.source)
        e.move()
        if delete_first:
            delete_old_copy(e.journal_path(), confirmed=True)
        _write(e.source / "new.txt", b"TARGET-SIDE addition")
        full_target = _manifest(e.target)
        with monkeypatch.context() as m:
            _arm6(m, at, relocate._key(_restore(e)))
            try:
                rollback(e.journal_path())
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        if _exists(e.target):
            os.rename(_x(e.target), _x(e.d / "unplugged"))
        try:
            out = rollback(e.journal_path())
            if out.journal.stage == Stage.ROLLED_BACK and not _is_link(e.source):
                got = _manifest(e.source)
                if "copy from before the move" in out.message and "put back" in out.message:
                    if got != pre_move and got != full_target:
                        failures.append(
                            f"kill at {at} ({where}): PARTIAL folder reported put back "
                            f"({len(got)} of {len(full_target)} entries)"
                        )
                    elif got != pre_move:
                        failures.append(
                            f"kill at {at} ({where}): message says 'copy from before the move' "
                            "but the restore copy of the moved folder was put back"
                        )
        except (RelocateError, OSError):
            pass  # refusing is a correct answer here
        at += 1
    assert at > 10
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


def test_b7_h1_a_verified_restore_is_put_back_and_named_for_what_it_is(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy-back finished and was verified, then the process died before the junction went.
    With the target drive gone, that restore is a whole copy of the moved folder: put back, but
    the message must not call it the copy from before the move (which was deleted)."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    full = _manifest(env.target)

    def die(_link: str) -> None:
        raise SimulatedCrash("after the restore was verified")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert Journal.load(env.journal_path()).restore_verified
    os.rename(_x(env.target), _x(env.d / "unplugged"))
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == full
    assert "verified copy of the moved folder" in out.message
    assert "put back the copy from before the move" not in out.message


def test_b7_h1_a_partial_restore_is_refused_with_the_way_forward_and_keeps_everything(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    full = _manifest(env.target)
    unplugged = env.d / "unplugged"
    real = relocate._copy_file
    n = {"i": 0}

    def copy(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))):
            n["i"] += 1
            if n["i"] == 3:
                os.rename(_x(env.target), _x(unplugged))
                raise FileNotFoundError(3, "The system cannot find the path specified", src)
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_copy_file", copy)
        with pytest.raises(RelocateError, match="reconnect"):
            rollback(env.journal_path())
    assert not Journal.load(env.journal_path()).restore_verified
    partial = _manifest(_restore(env))
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path())
    assert str(env.target) in str(info.value) and "reconnect" in str(info.value)
    assert _is_link(env.source)  # still the junction: the path is never left empty
    assert _manifest(_restore(env)) == partial  # nothing was touched
    os.rename(_x(unplugged), _x(env.target))  # the drive comes back: the next rollback finishes
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source)
    assert _manifest(env.source) == full


def _boom_sync(*_a: object, **_k: object) -> str:
    raise RelocateError("simulated: 1 files differ after copying them twice")


def test_b7_m2_a_blocked_rollback_names_the_move_and_recover_finishes_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_delta_sync", _boom_sync)
        m.setattr(relocate, "_rename", rename)
        with pytest.raises(RelocateError) as info:
            env.move()
    (journal,) = relocate.journals(env.journals)
    assert journal.id in str(info.value)
    assert f"reclaim relocate recover {journal.id}" in str(info.value)
    assert _exists(_moved(env)) and not _exists(env.source)  # parked, and the message says so
    out = recover(env.journal_path())  # the cause is gone
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_b7_m2_the_plan_refuses_a_journal_drive_without_room(env: Env) -> None:
    _make_tree(env.source)
    journal_key = relocate._key(env.journals)
    plan = plan_move(
        env.source,
        env.target,
        env.policy,
        free_bytes=lambda p: 1000 if relocate._key(p) == journal_key else BIG,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert not plan.ok
    assert any("journal folder" in r for r in plan.reasons), plan.reasons
    roomy = plan_move(
        env.source,
        env.target,
        env.policy,
        free_bytes=lambda _p: relocate.JOURNAL_RESERVE_BYTES,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert not any("journal folder" in r for r in roomy.reasons), roomy.reasons


def test_b7_m2_the_cli_prints_the_recover_instruction_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "D"
    d.mkdir()
    d_key = relocate._key(d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )
    src = tmp_path / "C" / "models"
    _write(src / "a.bin", b"a" * 10)
    moved = Path(str(src) + relocate.MOVED_SUFFIX)
    mkey, skey = relocate._key(moved), relocate._key(src)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    def boom(*_a: object, **_k: object) -> str:
        raise RelocateError("simulated: 1 files differ after copying them twice")

    monkeypatch.setattr(relocate, "_delta_sync", boom)
    monkeypatch.setattr(relocate, "_rename", rename)
    code = main(
        [
            "relocate",
            "--config",
            str(tmp_path / "config.toml"),
            "--journal-dir",
            str(tmp_path / "journals"),
            "move",
            str(src),
            "--target",
            str(d / "models"),
            "--yes",
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "reclaim relocate recover " in err and "Traceback" not in err


def test_b7_m2_a_second_rename_of_the_target_probe_that_fails_is_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    tkey = relocate._key(env.target)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(b) == tkey and relocate._key(a).endswith(relocate.PROBE_SUFFIX):
            raise PermissionError(5, "Access is denied (simulated)", a)
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", rename)
    with pytest.raises(RelocateError, match="rename it back"):
        relocate._probe_target(str(env.target))


def test_b7_m2_a_failed_removal_of_our_copy_after_the_restore_is_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)

    def hook(name: str) -> None:
        if name == "after_freeze":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)

    def refuse(_path: str) -> None:
        raise PermissionError(5, "Access is denied (simulated)")

    with monkeypatch.context() as m:
        m.setattr(relocate, "rmtree_reparse_point_safe", refuse)
        with pytest.raises(RelocateError, match="could not be removed"):
            rollback(env.journal_path())
    assert _manifest(env.source) == expected
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _exists(env.target)


def test_b7_m2_a_failed_removal_of_the_old_copy_is_a_relocate_error_naming_delete_old(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    jid = Journal.load(env.journal_path()).id

    def refuse(_path: str) -> None:
        raise PermissionError(5, "Access is denied (simulated)")

    with monkeypatch.context() as m:
        m.setattr(relocate, "rmtree_reparse_point_safe", refuse)
        with pytest.raises(RelocateError, match=f"delete-old {jid}"):
            delete_old_copy(env.journal_path(), confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    out = delete_old_copy(env.journal_path(), confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD
