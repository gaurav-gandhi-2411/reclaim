from __future__ import annotations

# ruff: noqa: F811, PTH101, PTH104, PTH106, PTH107, PTH123
# Hostile-review probes for reclaim.relocate (verifier B6, head 4eb7b06). Each test asserts a
# safety claim; a failure on the current code demonstrates the finding.
import os
import shutil
import stat
from pathlib import Path

import pytest
from test_relocate import (  # type: ignore[import-not-found]
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
    _all_bytes,
    _arm_deletes,
    _moved,
    _stage,
    _vol,
)

import reclaim.relocate as relocate
from reclaim.relocate import (
    RESTORE_SUFFIX,
    RelocateError,
    Stage,
    delete_old_copy,
    rollback,
)

OTHER_VOLUME = Path(r"D:\reclaim-build\tmp\verifierB6")


def _restore(e: Env) -> Path:
    return Path(str(e.source) + RESTORE_SUFFIX)


# ------------------------------------------------------------------ space


def test_the_rollback_space_check_measures_the_source_volume(env: Env, tmp_path: Path) -> None:
    """rollback calls free_bytes(src) while src is still the junction. _free_bytes(junction)
    -> shutil.disk_usage follows the junction, so it reports the TARGET volume's free space
    (the roomy one the data was moved to), not the source volume the copy-back fills."""
    if tmp_path.drive.casefold() == OTHER_VOLUME.drive.casefold():
        pytest.skip("needs tmp_path and the export on two volumes")
    _make_tree(env.source)
    env.move()
    calls: list[tuple[str, bool]] = []

    def spy(path: str) -> int:
        calls.append((path, _is_link(path)))
        return 10**15

    rollback(env.journal_path(), free_bytes=spy)
    # The fix: the check is given the source volume's real folder, never the junction.
    assert calls and not calls[0][1], "the space check was handed the junction"
    # What the default free_bytes returns for a junction to another volume:
    link = tmp_path / "jprobe"
    relocate._create_junction(str(link), str(OTHER_VOLUME))
    try:
        got = relocate._free_bytes(str(link))
        here = shutil.disk_usage(_x(tmp_path)).free
        there = shutil.disk_usage(str(OTHER_VOLUME)).free
    finally:
        relocate._remove_junction(str(link))
    assert abs(got - here) < abs(got - there), (
        f"_free_bytes(junction) = {got}, source volume free = {here}, target volume free = "
        f"{there}: the rollback space check reads the target volume"
    )


def test_delete_old_copy_reports_the_space_the_source_volume_gained(
    env: Env, tmp_path: Path
) -> None:
    """After DONE, _finish_delete measures free_bytes(journal.source): the junction, so the
    target volume. The 'volume gained X GiB' figure subtracts the source volume's free space
    before the move from the target volume's free space now."""
    _make_tree(env.source)
    env.move()
    calls: list[bool] = []

    def spy(path: str) -> int:
        calls.append(_is_link(path))
        return 0

    delete_old_copy(env.journal_path(), confirmed=True, free_bytes=spy)
    assert calls and not calls[-1], "the gain was measured through the junction (target volume)"


def test_a_partial_restore_does_not_skip_the_space_check(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill inside _rebuild_restore leaves a partial restore with restore_id set. The next
    rollback skips the space check (`_is_original(journal, restore)`), then rmtree's the partial
    copy and copies the whole folder again, on a volume it never measured."""
    _make_tree(env.source)
    env.move()
    real = relocate._copy_file
    n = {"i": 0}

    def die(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))):
            n["i"] += 1
            if n["i"] == 3:
                raise SimulatedCrash("copy-back")
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_copy_file", die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert _exists(_restore(env))
    with pytest.raises(RelocateError, match="GiB free"):
        rollback(env.journal_path(), free_bytes=lambda _p: 0)


# ------------------------------------------------------------------ partial old copy


@pytest.mark.parametrize("how", ["use_old_copy", "target_unreachable"])
def test_a_half_deleted_old_copy_is_never_put_back_as_the_copy_from_before_the_move(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """delete_old_copy killed half-way (stage DELETING_OLD). rollback accepts the half-deleted
    folder as 'the copy from before the move' (its root still has the source ID), renames it to
    the original path and reports ROLLED_BACK with 'put back the copy from before the move'."""
    _make_tree(env.source)
    env.move()
    full = _manifest(_moved(env))
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    if how == "target_unreachable":
        os.rename(_x(env.target), _x(env.d / "unplugged"))  # stands in for D: being offline
    try:
        out = rollback(env.journal_path(), use_old_copy=how == "use_old_copy")
    except RelocateError:
        return  # refusing is a correct answer
    got = _manifest(env.source)
    assert got == full, (
        f"reported {out.journal.stage}: {out.message!r}, but {len(full) - len(got)} entries of "
        "the folder are missing"
    )


# ------------------------------------------------------------------ keep period


def test_rolling_back_an_unfinished_swap_keeps_the_old_copy_for_the_keep_period(
    env: Env,
) -> None:
    """swapped_at is set only at DONE. A move killed after the junction and rolled back keeps
    swapped_at = 0, so delete_old_copy computes an age of ~20,000 days and deletes the old copy
    with no confirmation."""
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    rollback(env.journal_path())
    assert _exists(_moved(env))
    with pytest.raises(RelocateError, match="kept for"):
        delete_old_copy(env.journal_path())  # not confirmed


# ------------------------------------------------------------------ after the junction removal


@pytest.mark.parametrize("where", ["rename_into_place", "repair"])
def test_a_failure_after_the_junction_removal_leaves_the_folder_reachable(
    env: Env, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Only _delta_sync is wrapped by the 'put the junction back' handler. An OSError from
    _repair_from_old_copy (old copy file locked) or from the rename of the restore into place
    (an antivirus scanner holding a freshly written file) escapes raw, with the junction gone
    and nothing at the original path."""
    _make_tree(env.source)
    env.move()
    rkey, skey = relocate._key(_restore(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == rkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    def repair(_j: relocate.Journal, _r: str) -> list[str]:
        raise PermissionError(32, "The process cannot access the file (simulated)", "old")

    if where == "rename_into_place":
        monkeypatch.setattr(relocate, "_rename", rename)
    else:
        monkeypatch.setattr(relocate, "_repair_from_old_copy", repair)
    with pytest.raises(Exception) as info:
        rollback(env.journal_path())
    assert _exists(env.source), f"nothing at {env.source} after {info.value!r}"
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}"


# ------------------------------------------------------------------ messages


def test_use_old_copy_with_the_old_copy_deleted_does_not_say_the_target_is_unreachable(
    env: Env,
) -> None:
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path(), use_old_copy=True)
    assert "not reachable" not in str(info.value), str(info.value)


def test_use_old_copy_after_a_stopped_copy_back_does_not_leave_the_restore_unmentioned(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()

    def die(_link: str) -> None:
        raise SimulatedCrash("before the junction removal")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert _exists(_restore(env))
    out = rollback(env.journal_path(), use_old_copy=True)
    assert not _exists(_restore(env)) or str(_restore(env)) in out.message, (
        f"{_restore(env)} (a full copy of the folder) is left behind unmentioned: {out.message!r}"
    )


# ------------------------------------------------------------------ delete_old_copy reach


@pytest.mark.parametrize("rolled_back", [False, True])
@pytest.mark.parametrize("change", ["folder_removed", "stream_removed"])
def test_delete_old_copy_never_deletes_a_folder_stream_the_live_folder_lacks(
    env: Env, rolled_back: bool, change: str
) -> None:
    """delete_old_copy proves every recorded FILE is in the live folder, but never compares
    the recorded FOLDERS (attributes, named streams) with it. A folder's stream that a program
    removed (or whose folder it removed) in the live folder exists only in the old copy, and
    is deleted with no accept_missing; a removed file in the same position refuses."""
    _make_tree(env.source)
    with open(_x(env.source / "empty-dir") + ":meta", "wb") as fh:
        fh.write(b"folder metadata only the old copy will have")
    env.move()
    if change == "folder_removed":
        os.rmdir(_x(env.source / "empty-dir"))
    else:
        os.remove(_x(env.source / "empty-dir") + ":meta")
    if rolled_back:
        rollback(env.journal_path())
    try:
        delete_old_copy(env.journal_path(), confirmed=True)
    except RelocateError:
        return
    pytest.fail("deleted a folder stream that no other copy holds, without accept_missing")


# ------------------------------------------------------------------ kill sweep (new rollback)

_KILL = (
    "_copy_file",
    "_copy_stream",
    "_rename",
    "_remove_junction",
    "_create_junction",
    "rmtree_reparse_point_safe",
    "_set_attributes",
    "_copy_folder_meta",
)


def _arm6(m: pytest.MonkeyPatch, at: int, restore_key: str) -> None:
    count = {"n": 0}

    def tick(name: str) -> None:
        count["n"] += 1
        if count["n"] == at:
            raise SimulatedCrash(name)

    for name in _KILL:
        real = getattr(relocate, name)

        def wrapped(*a: object, _real=real, _name=name, **k: object) -> object:
            tick(_name)
            return _real(*a, **k)

        m.setattr(relocate, name, wrapped)
    for name in ("unlink", "rmdir", "remove", "mkdir", "makedirs"):
        real_os = getattr(os, name)

        def os_wrapped(path, *a, _real=real_os, _name=name, **k):  # type: ignore[no-untyped-def]
            if isinstance(path, str) and relocate._key(path).startswith(restore_key):
                tick("os." + _name)
            return _real(path, *a, **k)

        m.setattr(os, name, os_wrapped)
    real_save = relocate.Journal.save

    def save(self: relocate.Journal) -> None:
        tick("save")
        real_save(self)

    m.setattr(relocate.Journal, "save", save)


def _rich_tree(root: Path) -> None:
    _make_tree(root)
    _write(root / ("L" * 120) / ("M" * 120) / "deep.txt", b"past MAX_PATH")
    with open(_x(root / "a.bin") + ":zone", "wb") as fh:
        fh.write(b"[ZoneTransfer]\nZoneId=3")


@pytest.mark.parametrize("then", ["rollback", "use_old_copy"])
@pytest.mark.parametrize("delete_first", [False, True])
def test_a_kill_anywhere_in_the_new_rollback_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_first: bool, then: str
) -> None:
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"r{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _rich_tree(e.source)
        e.move()
        if delete_first:
            delete_old_copy(e.journal_path(), confirmed=True)
        else:
            _write(_moved(e) / "sub" / "deeper" / "c [1].txt", b"OLD-SIDE edit, only here")
        _write(e.source / "sub" / "b.txt", b"TARGET-SIDE edit through the junction")
        _write(e.source / "new.txt", b"TARGET-SIDE addition")
        os.rename(_x(e.source / "hidden.txt"), _x(e.source / "Hidden.txt"))
        late = {"done": False}
        real_rebuild = relocate._rebuild_restore

        def rebuild(
            j: relocate.Journal, _e: Env = e, _late: dict = late, _real=real_rebuild
        ) -> None:
            _real(j)
            if not _late["done"]:
                _late["done"] = True
                _write(_e.target / "late.txt", b"TARGET-SIDE late write during the copy-back")
                os.remove(_x(_e.target / "sub" / "zero.bin"))

        old_before = _manifest(_moved(e)) if not delete_first else None
        rkey = relocate._key(_restore(e))
        with monkeypatch.context() as m:
            m.setattr(relocate, "_rebuild_restore", rebuild)
            _arm6(m, at, rkey)
            try:
                rollback(e.journal_path())
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        expected = _manifest(e.target) if _exists(e.target) else None
        must_survive = [
            b"TARGET-SIDE edit through the junction",
            b"TARGET-SIDE addition",
        ]
        if late["done"]:
            must_survive.append(b"TARGET-SIDE late write during the copy-back")
        if not delete_first:
            must_survive.append(b"OLD-SIDE edit, only here")
        try:
            out = None
            for _ in range(2):
                out = rollback(e.journal_path(), use_old_copy=then == "use_old_copy")
            assert out is not None
            survivors = []
            for root in (e.source, e.target, _moved(e), _restore(e)):
                if _exists(root) and not _is_link(root):
                    survivors += _all_bytes(root)
            lost = [b for b in must_survive if b not in survivors]
            problems = []
            if out.journal.stage != Stage.ROLLED_BACK or _is_link(e.source):
                problems.append(f"not rolled back: {out.message}")
            if lost:
                problems.append(f"lost {lost}")
            if old_before is not None and _exists(_moved(e)) and _manifest(_moved(e)) != old_before:
                problems.append("the old copy was written")
            said_old = "copy from before the move" in out.message and "put back" in out.message
            if then == "rollback" and expected is not None and _manifest(e.source) != expected:
                problems.append("restored folder is not the moved folder")
            if said_old and old_before is not None and _manifest(e.source) != old_before:
                problems.append(f"said old copy put back but it differs: {out.message}")
            if _exists(_restore(e)) and str(_restore(e)) not in out.message:
                problems.append("restore folder left behind, unmentioned")
            if problems:
                failures.append(f"kill at {at} ({where}): {'; '.join(problems)}")
        except (RelocateError, OSError) as exc:
            refused_ok = (
                then == "use_old_copy"
                and delete_first
                and (
                    "nothing to put back" in str(exc)
                    or "no copy from before the move left" in str(exc)
                )
                and _is_link(e.source)
            )
            if not refused_ok:
                failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:200]}")
        at += 1
    assert at > 10
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


# ------------------------------------------------------------------ concurrent writers, messages


def test_a_file_deleted_in_the_target_during_the_copy_back_gives_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program deletes a file through the junction while _rebuild_restore copies. The
    FileNotFoundError is not a RelocateError, so it escapes raw (no 'nothing changed' and no
    use_old_copy hint) instead of the wrapped message the design gives for a copy-back failure."""
    _make_tree(env.source)
    env.move()
    real = relocate._copy_file
    victim = env.target / "sub" / "b.txt"

    def copy(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))) and _exists(victim):
            os.chmod(_x(victim), stat.S_IWRITE | stat.S_IREAD)
            os.remove(_x(victim))
        real(src, dst)

    monkeypatch.setattr(relocate, "_copy_file", copy)
    try:
        rollback(env.journal_path())
    except RelocateError:
        return
    except OSError as exc:
        pytest.fail(f"raw {type(exc).__name__}: {exc}")


def test_a_folder_recreated_after_the_junction_removal_names_where_the_newest_data_is(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killed after the junction removal; a program recreates the folder. The refusal says
    'the original data is safe in <name>.reclaim-moved' -- the copy from BEFORE the move -- and
    never names the moved folder (or the restore copy) that holds everything written since."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"written through the junction after the move")

    def die(*_a: object, **_k: object) -> str:
        raise SimulatedCrash("after the junction removal")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_delta_sync", die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert not _exists(env.source)
    _write(env.source / "settings.ini", b"recreated by the program")
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path())
    msg = str(info.value)
    assert str(env.target) in msg or str(_restore(env)) in msg, msg
