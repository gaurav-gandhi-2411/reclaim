from __future__ import annotations

# ruff: noqa: F811, PTH101, PTH104, PTH107, PTH118, S103
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
from test_relocate_review_b2 import _arm  # type: ignore[import-not-found]
from test_relocate_review_b4 import (  # type: ignore[import-not-found]
    _all_bytes,
    _arm_deletes,
    _moved,
    _stage,
    _vol,
)
from test_relocate_review_b6 import _arm6, _restore  # type: ignore[import-not-found]

import reclaim.relocate as relocate
from reclaim.cli import main
from reclaim.relocate import (
    Journal,
    Policy,
    RelocateError,
    Stage,
    classify,
    delete_old_copy,
    execute_move,
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
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
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


def test_b7_recover_finishes_a_swap_that_programs_already_use(env: Env) -> None:
    """Killed after the junction was made (stage SWAPPING). After the restart a program writes
    into its folder (through the junction) before the user runs recover. _post_swap_check
    compares the old copy's listing with the live folder, sees one more file, and recover
    rolls the finished move back: a full copy back onto the drive the user was freeing (or, if
    it lacks room, a refusal telling them to free space or use the old copy)."""
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    assert _stage(env) == Stage.SWAPPING
    _write(env.source / "written-after-restart.log", b"the program ran before recover")
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.DONE, f"{out.journal.stage}: {out.message}"


def test_b7_recover_keeps_a_use_old_copy_rollback_on_the_old_copy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user chose use_old_copy (the moved folder holds something they do not want). A kill
    lands between the junction removal and the rename. recover() always calls rollback without
    use_old_copy, so it copies the moved folder back instead, the opposite of the choice (and
    needs a folder's worth of free space the user chose use_old_copy to avoid)."""
    _make_tree(env.source)
    env.move()
    old_before = _manifest(_moved(env))
    _write(env.source / "sub" / "b.txt", b"damaged by a program after the move")
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise SimulatedCrash("between the junction removal and the rename")
        real(a, b)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_rename", rename)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path(), use_old_copy=True)
    out = recover(env.journal_path(), policy=env.policy)
    assert _manifest(env.source) == old_before, f"recover did a copy-back: {out.message!r}"


def test_b7_rollback_after_an_interrupted_delete_never_calls_the_rest_unchanged(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    out = rollback(env.journal_path())
    assert "kept unchanged" not in out.message, out.message


def test_b7_delete_old_after_that_rollback_does_not_blame_a_writer(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the rollback above, delete_old_copy (stage ROLLED_BACK, allow_missing=False)
    refuses for ever and says 'something wrote into the old copy, so Reclaim will not delete
    it': nothing wrote; Reclaim itself half-deleted it."""
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    rollback(env.journal_path())
    try:
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    except RelocateError as exc:
        assert "something wrote into the old copy" not in str(exc), str(exc)


def test_b7_delete_old_never_deletes_an_excluded_projects_old_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """delete_old_copy takes no policy, and the CLI computes one but does not pass it: a
    project the user excluded after the move has its original files (the old copy) deleted."""
    d = tmp_path / "D"
    d.mkdir()
    d_key = relocate._key(d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )
    src = tmp_path / "C" / "models"
    _write(src / "a.bin", b"a" * 10)
    _write(src / "zzexclproj" / "main.py", b"the excluded project's source")
    config = tmp_path / "config.toml"
    journals = tmp_path / "journals"

    def cli(*args: str) -> int:
        return main(["relocate", "--config", str(config), "--journal-dir", str(journals), *args])

    assert cli("move", str(src), "--target", str(d / "models"), "--yes") == 0
    (journal,) = relocate.journals(journals)
    config.write_text('[exclusions]\nproject_names = ["zzexclproj"]\n', encoding="utf-8")
    assert cli("plan", str(d / "models"), "--target", str(tmp_path / "C" / "x")) == 1
    assert "excluded" in capsys.readouterr().out  # the exclusion is live for the plan
    cli("delete-old", journal.id, "--confirm")
    moved = Path(str(src) + relocate.MOVED_SUFFIX)
    assert _exists(moved / "zzexclproj" / "main.py"), "the excluded project's old copy was deleted"


def test_b7_a_second_move_after_a_rollback_says_how_to_clear_the_kept_old_copy(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    plan = plan_move(
        env.source,
        env.d / "elsewhere",
        env.policy,
        free_bytes=lambda _p: BIG,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert not plan.ok
    text = " ".join(plan.reasons)
    jid = Journal.load(env.journal_path()).id
    assert jid in text or "delete-old" in text or "delete_old" in text, text


def test_b7_a_rolled_back_move_whose_old_copy_was_deleted_does_not_block_the_folder(
    env: Env,
) -> None:
    """Move, rollback, delete-old: the journal ends DELETED_OLD although the folder is back
    and nothing of the move is left. _overlap_reasons skips only ROLLED_BACK, so every later
    move of the folder is refused with 'finish or roll that one back first' (a rollback of a
    move that was rolled back and finished)."""
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    plan = plan_move(
        env.source,
        env.d / "elsewhere",
        env.policy,
        free_bytes=lambda _p: BIG,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert plan.ok, plan.reasons


def test_b7_holds_moving_the_parent_of_a_rolled_back_move(env: Env) -> None:
    """J1 rolled back with its old copy kept (ROLLED_BACK journals are skipped by the overlap
    check). J2 moves the parent, carrying both folders. J1's delete-old must then refuse (its
    old copy is reached through J2's junction), and J2's delete-old must not lose J1's copy."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"edited after the move")
    rollback(env.journal_path())
    j1 = env.journal_path()
    old_before = _manifest(_moved(env))
    live_before = _manifest(env.source)
    parent_target = env.d / "parent-moved"
    execute_move(env.c, parent_target, env.policy, env.journals, free_bytes=lambda _p: BIG)
    with pytest.raises(RelocateError):
        delete_old_copy(j1, policy=env.policy, confirmed=True)
    (j2,) = [p for p in sorted(env.journals.glob("relocate-*.json")) if p != j1]
    delete_old_copy(j2, policy=env.policy, confirmed=True)
    assert _manifest(parent_target / "data.reclaim-moved") == old_before
    assert _manifest(parent_target / "data") == live_before


def test_b7_holds_a_stale_old_copy_journal_never_deletes_a_later_moves_old_copy(env: Env) -> None:
    """J1: move, rollback (old copy kept), the user removes the old copy by hand. J2 moves the
    same folder again: its old copy carries J1's restore_id. J1's delete-old must refuse."""
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    j1 = env.journal_path()
    import shutil

    for dirpath, _dirs, files in os.walk(_x(_moved(env))):
        for name in files:
            os.chmod(os.path.join(dirpath, name), 0o666)
    shutil.rmtree(_x(_moved(env)))
    env.move()
    before = _manifest(_moved(env))
    with pytest.raises(RelocateError):
        delete_old_copy(j1, policy=env.policy, confirmed=True)
    assert _manifest(_moved(env)) == before


def _journal_paths(e: Env) -> list[Path]:
    return sorted(e.journals.glob("relocate-*.json"))


@pytest.mark.parametrize("then", ["recover", "use_old_copy"])
def test_b7_sweep_a_kill_anywhere_in_a_use_old_copy_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    """B6 swept kills inside a plain rollback only. This kills rollback(use_old_copy=True)
    from DONE at every point, then follows with recover or another use_old_copy rollback."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"u{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        _write(_moved(e) / "sub" / "deeper" / "c [1].txt", b"OLD-SIDE edit, only here")
        _write(e.source / "sub" / "b.txt", b"TARGET-SIDE edit through the junction")
        with monkeypatch.context() as m:
            _arm6(m, at, relocate._key(_restore(e)))
            try:
                rollback(e.journal_path(), use_old_copy=True)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        empty_after_kill = not _exists(e.source)
        if _stage(e) == Stage.DONE:
            at += 1  # killed before the first journal write: nothing started
            continue
        try:
            for _ in range(2):
                out = (
                    recover(e.journal_path(), policy=e.policy)
                    if then == "recover"
                    else rollback(e.journal_path(), use_old_copy=True)
                )
            survivors = []
            for root in (e.source, e.target, _moved(e), _restore(e)):
                if _exists(root) and not _is_link(root):
                    survivors += _all_bytes(root)
            lost = [
                b
                for b in (b"OLD-SIDE edit, only here", b"TARGET-SIDE edit through the junction")
                if b not in survivors
            ]
            problems = []
            if out.journal.stage != Stage.ROLLED_BACK or _is_link(e.source):
                problems.append(f"not rolled back: {out.message}")
            if lost:
                problems.append(f"lost {lost}")
            if not _exists(e.source):
                problems.append("original path empty")
            if problems:
                failures.append(
                    f"kill at {at} ({where}, empty after kill={empty_after_kill}): "
                    + "; ".join(problems)
                )
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:160]}")
        at += 1
    assert at > 2
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


def test_b7_sweep_a_kill_anywhere_in_delete_old_after_a_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_old_copy at stage ROLLED_BACK (live = the restored folder) killed at every
    point, then recover. The restored folder must be untouched, and the old copy either whole
    (refused) or gone with DELETED_OLD."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"d{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        _write(e.source / "new.txt", b"written after the move")
        rollback(e.journal_path())
        live = _manifest(e.source)
        old = _manifest(_moved(e))
        with monkeypatch.context() as m:
            _arm_deletes(m, at, relocate._key(_moved(e)))
            try:
                delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        try:
            out = recover(e.journal_path(), policy=e.policy)
            problems = []
            if _manifest(e.source) != live:
                problems.append("restored folder changed")
            if out.journal.stage == Stage.DELETED_OLD:
                if _exists(_moved(e)):
                    problems.append("reported deleted but the old copy is there")
            elif _exists(_moved(e)) and _manifest(_moved(e)) != old:
                problems.append(f"half-deleted, not finished: {out.journal.stage}")
            if problems:
                failures.append(f"kill at {at} ({where}): {'; '.join(problems)}")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:160]}")
        at += 1
    assert at > 5
    assert not failures, " || ".join(failures)


def test_b7_sweep_a_kill_anywhere_in_a_second_move_after_a_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle: move, rollback, delete-old, then a second move of the same folder (its
    folder ID is the first journal's restore_id) killed at every point, then recover. The
    first journal stays in the folder the whole time."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"s{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        _write(e.source / "new.txt", b"written after the first move")
        rollback(e.journal_path())
        delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
        (j1,) = _journal_paths(e)
        expected = _manifest(e.source)
        with monkeypatch.context() as m:
            _arm(m, at)
            try:
                e.move()
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        rest = [p for p in _journal_paths(e) if p != j1]
        try:
            if rest:
                out = recover(rest[0], policy=e.policy)
                if out.journal.stage == Stage.DONE:
                    ok = _is_link(e.source) and _manifest(e.target) == expected
                    out = delete_old_copy(rest[0], policy=e.policy, confirmed=True)
                    ok = ok and out.journal.stage == Stage.DELETED_OLD
                else:
                    ok = (
                        out.journal.stage == Stage.ROLLED_BACK
                        and _manifest(e.source) == expected
                        and not _exists(e.target)
                    )
            else:
                ok = _manifest(e.source) == expected
            # The first journal must stay inert whatever the second did.
            assert recover(j1, policy=e.policy).journal.stage == Stage.DELETED_OLD
            if not ok:
                failures.append(f"kill at {at} ({where}): wrong state")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:160]}")
        at += 1
    assert at > 10
    assert not failures, " || ".join(failures)


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
            delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
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
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
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
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
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


def test_b7_l1_the_use_old_copy_choice_is_in_the_journal_until_a_plain_rollback_replaces_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise SimulatedCrash("between the junction removal and the rename")
        real(a, b)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_rename", rename)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path(), use_old_copy=True)
    assert Journal.load(env.journal_path()).use_old_copy is True

    # An explicit plain rollback is a new choice and replaces the saved one.
    def crash(*_a: object, **_k: object) -> str:
        raise SimulatedCrash("during the copy-back's sync")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_delta_sync", crash)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert Journal.load(env.journal_path()).use_old_copy is False


def test_b7_l2_the_message_after_a_rollback_from_a_partial_delete_says_so(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    out = rollback(env.journal_path())
    assert "partly deleted" in out.message and "kept unchanged" not in out.message
    assert Journal.load(env.journal_path()).old_copy_partial


def test_b7_l2_delete_old_after_that_rollback_finishes_and_a_real_write_is_still_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    rollback(env.journal_path())
    assert [p for p in _moved(env).rglob("*") if p.is_file()]  # the delete stopped half-way
    _write(_moved(env) / "written-by-a-program.txt", b"new")
    with pytest.raises(RelocateError, match="beyond what Reclaim's own interrupted delete"):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    os.remove(_x(_moved(env) / "written-by-a-program.txt"))
    out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD
    assert not _exists(_moved(env))


def test_b7_l3_a_deleted_old_after_a_finished_move_keeps_blocking(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    # A finished move: the path is a live junction, so the journal keeps blocking.
    assert relocate._overlap_reasons(str(env.source), str(env.d / "elsewhere"), env.journals)


def test_b7_l3_only_a_deleted_old_that_followed_a_rollback_stops_blocking(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    other = str(env.d / "elsewhere")
    rollback(env.journal_path())
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    journal = Journal.load(env.journal_path())
    assert journal.stage == Stage.DELETED_OLD and journal.rolled_back
    assert not relocate._overlap_reasons(str(env.source), other, env.journals)
    # A journal from before the flag existed stays blocking (fail closed).
    journal.rolled_back = False
    journal.save()
    assert relocate._overlap_reasons(str(env.source), other, env.journals)


def test_b7_l4_the_refusal_for_a_kept_old_copy_names_the_command_and_the_id(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    jid = Journal.load(env.journal_path()).id
    plan = plan_move(
        env.source,
        env.d / "elsewhere",
        env.policy,
        free_bytes=lambda _p: BIG,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert any(f"`reclaim relocate delete-old {jid}`" in r for r in plan.reasons), plan.reasons


def test_b7_l5_the_original_must_be_on_the_recorded_volume_too(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    journal = Journal.load(env.journal_path())
    moved = str(_moved(env))
    assert journal.source_dev == relocate._folder_dev(moved) != 0
    assert relocate._is_original(journal, moved)
    journal.source_dev += 1  # the same file ID, another volume
    assert not relocate._is_original(journal, moved)
    journal.source_dev = 0  # a journal from before the field: the file ID alone, as before
    assert relocate._is_original(journal, moved)


def test_b7_l5_a_corrupt_journal_is_a_relocate_error_in_the_library_and_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()
    (journals / "relocate-badbadbad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(RelocateError, match=r"relocate-badbadbad\.json"):
        relocate.journals(journals)
    code = main(
        [
            "relocate",
            "--config",
            str(tmp_path / "config.toml"),
            "--journal-dir",
            str(journals),
            "status",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1 and "relocate-badbadbad.json" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_b7_l5_an_efs_encrypted_file_stops_the_plan(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    real_stat = os.stat

    class Flagged:
        def __init__(self, st: os.stat_result) -> None:
            self._st = st

        def __getattr__(self, name: str) -> object:
            return getattr(self._st, name)

        @property
        def st_file_attributes(self) -> int:
            return self._st.st_file_attributes | 0x4000

    def fake(path, *a, **k):  # type: ignore[no-untyped-def]
        st = real_stat(path, *a, **k)
        return Flagged(st) if isinstance(path, str) and path.endswith("b.txt") else st

    with monkeypatch.context() as m:
        m.setattr(os, "stat", fake)
        found = classify(env.source, env.policy)
    assert found.verdict is relocate.Verdict.MUST_STAY
    assert any("EFS-encrypted" in r for r in found.reasons), found.reasons
    assert classify(env.source, env.policy).verdict is relocate.Verdict.MOVABLE


def test_b7_l5_a_target_that_is_not_a_local_fixed_drive_is_refused_before_any_copy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    target_key = relocate._key(env.d)
    monkeypatch.setattr(
        relocate, "_drive_type", lambda p: 4 if relocate._key(p).startswith(target_key) else 3
    )
    with pytest.raises(RelocateError, match="network drive"):
        env.move()
    assert not _exists(env.target) and not _exists(_moved(env))
    assert not env.journals.exists() or not list(env.journals.glob("relocate-*.json"))
    monkeypatch.setattr(relocate, "_drive_type", lambda _p: 3)
    assert env.move().journal.stage == Stage.DONE


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
    out = recover(env.journal_path(), policy=env.policy)  # the cause is gone
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
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD


def _excluding(*, marker: str) -> Policy:
    """A policy that excludes any path whose text ends with `marker` (casefolded)."""
    return Policy(
        exclusion_match=lambda p: "excluded" if relocate._key(p).endswith(marker) else None,
        protected=lambda _p: False,
    )


def test_b7_m3_delete_old_copy_and_recover_cannot_be_called_without_a_policy(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    with pytest.raises(TypeError):
        delete_old_copy(env.journal_path(), confirmed=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        recover(env.journal_path())  # type: ignore[call-arg]


@pytest.mark.parametrize("marker", [relocate.MOVED_SUFFIX, "data"])
def test_b7_m3_an_excluded_moved_folder_or_old_copy_is_never_deleted(env: Env, marker: str) -> None:
    """The old copy's own path (`...reclaim-moved`), or the folder's path, is excluded now."""
    _make_tree(env.source)
    env.move()
    before = _manifest(_moved(env))
    with pytest.raises(RelocateError, match="excluded"):
        delete_old_copy(env.journal_path(), policy=_excluding(marker=marker), confirmed=True)
    assert _manifest(_moved(env)) == before
    assert _stage(env) == Stage.DONE
    out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)  # control
    assert out.journal.stage == Stage.DELETED_OLD


def test_b7_m3_an_excluded_project_inside_the_old_copy_is_never_deleted(env: Env) -> None:
    """The exclusion is by project name beneath the folder, as the plan applies it. The name is
    only in the old copy's tree at the move, and the manifest still matches."""
    _make_tree(env.source)
    _write(env.source / "zzexclproj" / "main.py", b"the excluded project's source")
    # The move itself refuses it (the plan's test), so move with a policy that does not see it.
    execute_move(
        env.source,
        env.target,
        Policy(exclusion_match=lambda _p: None, protected=lambda _p: False),
        env.journals,
        free_bytes=lambda _p: BIG,
    )
    with pytest.raises(RelocateError, match="excluded"):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _exists(_moved(env) / "zzexclproj" / "main.py")


def test_b7_m3_recover_does_not_finish_a_delete_for_an_excluded_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    left = _manifest(_moved(env))
    with pytest.raises(RelocateError, match="excluded"):
        recover(env.journal_path(), policy=_excluding(marker=relocate.MOVED_SUFFIX))
    assert _manifest(_moved(env)) == left


def test_b7_m4_recover_still_rolls_back_when_the_old_copy_itself_changed(env: Env) -> None:
    """The conservative path stays: if the old copy differs from its manifest, the listings
    differing is no proof that only programs wrote through the junction."""
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    assert _stage(env) == Stage.SWAPPING
    _write(_moved(env) / "written-into-the-old-copy.log", b"something wrote here")
    _write(env.source / "also-new.log", b"and through the junction")
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.ROLLED_BACK, out.message
    assert not _is_link(env.source)


def test_b7_m4_a_finished_swap_keeps_what_the_program_wrote(env: Env) -> None:
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    _write(env.source / "written-after-restart.log", b"kept")
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.DONE
    assert _is_link(env.source)
    assert (env.target / "written-after-restart.log").read_bytes() == b"kept"
