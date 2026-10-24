from __future__ import annotations

# ruff: noqa: F811, PTH104, PTH116, PTH123, PTH208
# Hostile-review probes for reclaim.relocate (verifier B5, head dfa7eb2). Each test asserts a
# safety claim; a failure on the current code demonstrates the finding.
import os
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
from test_relocate_review_b2 import _arm  # type: ignore[import-not-found]
from test_relocate_review_b4 import _all_bytes, _moved, _rmtree, _stage, _vol  # type: ignore

import reclaim.relocate as relocate
from reclaim.relocate import (
    RelocateError,
    Stage,
    delete_old_copy,
    recover,
    rollback,
)


def _survivors(e: Env) -> list[bytes]:
    out = _all_bytes(e.source) if _exists(e.source) and not _is_link(e.source) else []
    if _exists(e.target):
        out += _all_bytes(e.target)
    if _exists(_moved(e)):
        out += _all_bytes(_moved(e))
    return out


# ------------------------------------------------------------------ case-insensitive copy-back


def test_a_case_rename_through_the_junction_never_overwrites_an_edit_in_the_old_copy(
    env: Env,
) -> None:
    """_sync_back compares relative names case-sensitively. A program renames sub\\b.txt to
    sub\\B.txt through the junction (content unchanged); the user has edited sub\\b.txt in the
    old copy. 'B.txt' is not in old_files, so it is 'changed' without the manifest set-aside
    check, and _copy_file opens the old copy's b.txt (NTFS is case-insensitive) and overwrites
    the edit. _discard_copy then finds the target equal and removes it."""
    _make_tree(env.source)
    env.move()
    edit = b"edited in the old copy after the move -- only copy of this text"
    _write(_moved(env) / "sub" / "b.txt", edit)
    os.rename(_x(env.source / "sub" / "b.txt"), _x(env.source / "sub" / "B.txt"))
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert edit in _survivors(env), (
        f"the old-copy edit is gone; restored sub: {os.listdir(_x(env.source / 'sub'))}, "
        f"target kept: {_exists(env.target)}, message: {out.message!r}"
    )


def test_a_folder_replaced_by_a_file_of_other_case_does_not_block_rollback(env: Env) -> None:
    """The B3 M3 type-conflict set-aside, with the program's file spelled in another case
    (sub\\deeper -> file sub\\DEEPER). The case-sensitive conflict check misses it and
    _copy_file tries to open the old copy's folder for writing: a raw OSError, every time."""
    _make_tree(env.source)
    env.move()
    _rmtree(env.source / "sub" / "deeper")
    _write(env.source / "sub" / "DEEPER", b"a program replaced the folder with a file")
    try:
        out = rollback(env.journal_path())
    except OSError as exc:
        pytest.fail(f"rollback fails with a raw {type(exc).__name__}: {exc}")
    except RelocateError as exc:
        pytest.fail(f"rollback refuses: {exc}")
    assert out.journal.stage == Stage.ROLLED_BACK


# ------------------------------------------------------------------ a link inside the target


def test_a_junction_a_program_made_in_the_target_does_not_block_rollback_forever(
    env: Env,
) -> None:
    """After the move a program creates a junction inside its folder (in the target). A copy
    cannot keep a link, so copying the folder back is refused with nothing changed, and the
    message names the way out that works: use_old_copy (B5 M2)."""
    _make_tree(env.source)
    before = _manifest(env.source)
    env.move()
    elsewhere = env.c / "elsewhere"
    elsewhere.mkdir()
    relocate._create_junction(_x(env.source / "sub" / "lnk"), str(elsewhere))
    with pytest.raises(RelocateError, match="use_old_copy") as info:
        rollback(env.journal_path())
    assert "Nothing changed" in str(info.value)
    assert _is_link(env.source)
    out = rollback(env.journal_path(), use_old_copy=True)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == before
    assert _is_link(env.target / "sub" / "lnk"), "the moved folder is left as it is"


# ------------------------------------------------------------------ freeze-race kills


def _race_policy(e: Env, aside: Path) -> dict[str, bool]:
    """The B4 M3 race: after the pre-freeze ID check a program renames the folder away and
    makes a new one, so the freeze renames the program's new folder."""
    real = e.policy.exclusion_match
    state = {"armed": False, "done": False}

    def excl(path: Path) -> str | None:
        if state["armed"] and not state["done"] and relocate._key(path) == relocate._key(e.source):
            state["done"] = True
            os.rename(_x(e.source), _x(aside))
            _write(e.source / "fresh.txt", b"the program's new folder")
        return real(path)

    object.__setattr__(e.policy, "exclusion_match", excl)
    return state


def test_a_kill_anywhere_in_the_freeze_race_path_then_recover_is_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill sweep over the B4 M3 path (freeze renames a folder that is not the original, the
    move renames it back). After recover (plus up to two rollbacks) the program's new folder
    must be at its own path and the journal closed, never stranded at .reclaim-moved."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"r{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        aside = e.c / "data-renamed-by-program"
        state = _race_policy(e, aside)

        def hook(name: str, _s=state) -> None:
            if name == "after_verify":
                _s["armed"] = True

        with monkeypatch.context() as m:
            _arm(m, at)
            try:
                e.move(checkpoint=hook)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
            except RelocateError:
                crashed = False
        if not crashed:
            break
        if not state["done"]:
            at += 1
            continue
        paths = sorted(e.journals.glob("relocate-*.json"))
        try:
            out = recover(paths[0], policy=e.policy)
            for _ in range(2):
                if out.journal.stage == Stage.ROLLED_BACK:
                    break
                out = rollback(paths[0])
            ok = (
                out.journal.stage == Stage.ROLLED_BACK
                and _exists(e.source / "fresh.txt")
                and not _exists(_moved(e))
            )
            if not ok:
                failures.append(f"kill at {at} ({where}): wrong state {out.message}")
        except (RelocateError, OSError) as exc:
            failures.append(
                f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:220]} | "
                f"fresh at source: {_exists(e.source / 'fresh.txt')}, "
                f"moved exists: {_exists(_moved(e))}"
            )
        at += 1
    assert at > 10
    assert not failures, " || ".join(failures)


# ------------------------------------------------------------------ set-aside kill sweep


def _conflict_setup(e: Env) -> tuple[bytes, bytes]:
    _make_tree(e.source)
    e.move()
    old_edit = b"edited in the OLD COPY after the move"
    new_edit = b"edited THROUGH THE JUNCTION after the move, longer"
    _write(_moved(e) / "sub" / "b.txt", old_edit)
    _write(e.source / "sub" / "b.txt", new_edit)
    _rmtree(e.source / "sub" / "deeper")
    _write(e.source / "sub" / "deeper", b"a program replaced the folder with a file")
    return old_edit, new_edit


_LEGIT_ASIDE = {"sub\\b.txt.reclaim-conflict", "sub\\deeper.reclaim-conflict"}


def _asides(root: Path) -> set[str]:
    return {rel for rel in _manifest(root) if ".reclaim-conflict" in rel.split("\\")[-1]}


@pytest.mark.parametrize("torn", [False, True])
def test_a_kill_anywhere_in_a_rollback_that_sets_conflicts_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn: bool
) -> None:
    """Rollback with a manifest set-aside (b.txt edited on both sides) and a type conflict
    (sub\\deeper became a file). Kill at every killable call; with `torn`, the kill lands in
    the middle of a stream write (half the bytes written), as a real kill would. After two more
    rollbacks: both edits survive, the original path is a folder holding every target file,
    and no conflict entry exists beyond the two real ones."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"s{int(torn)}{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        old_edit, new_edit = _conflict_setup(e)
        expected_target = _manifest(e.target)
        with monkeypatch.context() as m:
            if torn:
                count = {"n": 0}
                real_copy = relocate._copy_stream

                def torn_copy(fi, dst, _c=count, _r=real_copy, _at=at):  # type: ignore[no-untyped-def]
                    _c["n"] += 1
                    if _c["n"] == _at:
                        data = fi.read()
                        with open(dst, "wb") as fo:
                            fo.write(data[: len(data) // 2])
                        raise SimulatedCrash("torn _copy_stream")
                    return _r(fi, dst)

                m.setattr(relocate, "_copy_stream", torn_copy)
            else:
                _arm(m, at)
            try:
                rollback(e.journal_path())
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        try:
            out = rollback(e.journal_path())
            out = rollback(e.journal_path())
            survivors = _survivors(e)
            got = _manifest(e.source) if not _is_link(e.source) else {}
            missing = [r for r, h in expected_target.items() if got.get(r) != h]
            extra = sorted(_asides(e.source) - _LEGIT_ASIDE)
            problems = []
            if out.journal.stage != Stage.ROLLED_BACK or _is_link(e.source):
                problems.append(f"stage {out.journal.stage}")
            if old_edit not in survivors:
                problems.append("old-copy edit lost")
            if new_edit not in survivors:
                problems.append("junction edit lost")
            if missing:
                problems.append(f"target files not restored: {missing[:3]}")
            if extra:
                problems.append(f"junk conflict entries: {extra[:3]}")
            if problems:
                failures.append(f"kill at {at} ({where}): {'; '.join(problems)}")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:200]}")
        at += 1
    assert at > 2
    assert not failures, " || ".join(failures)


def test_a_rollback_with_edits_on_both_sides_keeps_both_and_says_where(env: Env) -> None:
    """Addendum 5: the folder comes back as the programs left it, and the old copy, with the
    user's own edit, is kept unchanged; the outcome says where it is."""
    old_edit, _ = _conflict_setup(env)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert (_moved(env) / "sub" / "b.txt").read_bytes() == old_edit
    assert str(_moved(env)) in out.message and "kept unchanged" in out.message


# ------------------------------------------------------------------ metadata


def test_an_attribute_set_in_the_old_copy_is_not_silently_reset_by_rollback(env: Env) -> None:
    """Rollback never writes into the old copy (addendum 5): attributes set there stay."""
    _make_tree(env.source)
    env.move()
    relocate._set_attributes(_x(_moved(env) / "sub"), stat.FILE_ATTRIBUTE_HIDDEN)
    relocate._set_attributes(_x(_moved(env) / "a.bin"), stat.FILE_ATTRIBUTE_HIDDEN)
    rollback(env.journal_path())
    folder = os.stat(_x(_moved(env) / "sub"), follow_symlinks=False).st_file_attributes
    file_ = os.stat(_x(_moved(env) / "a.bin"), follow_symlinks=False).st_file_attributes
    assert folder & stat.FILE_ATTRIBUTE_HIDDEN and file_ & stat.FILE_ATTRIBUTE_HIDDEN


# ------------------------------------------------------------------ messages after _gone


def test_a_check_failure_after_the_restore_does_not_say_nothing_changed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_gone raises '... nothing changed, retry'. In _discard_copy it runs after the junction
    was removed and the original renamed back, so 'nothing changed' is false."""
    _make_tree(env.source)
    env.move()
    flag = {"restored": False}
    real_rename = relocate._rename
    tkey = relocate._key(env.target)

    def rename(src: str, dst: str) -> None:
        real_rename(src, dst)
        if relocate._key(dst) == relocate._key(env.source):
            flag["restored"] = True

    real_stat = os.stat

    def fake(path, *a, **k):  # type: ignore[no-untyped-def]
        if flag["restored"] and isinstance(path, str) and relocate._key(path) == tkey:
            raise PermissionError(13, "simulated transient error", path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(relocate, "_rename", rename)
    monkeypatch.setattr(relocate.os, "stat", fake)
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path())
    assert not _is_link(env.source), "precondition: the original was restored"
    assert "nothing changed" not in str(info.value), (
        f"says nothing changed after restoring the original: {info.value}"
    )


def test_a_touched_old_copy_refusal_names_a_step_that_works(env: Env) -> None:
    """Once something was written into the old copy, Reclaim never deletes it (it cannot tell
    that data from the move's own), and the message says so instead of suggesting a step that
    would not change the answer (B5 L6)."""
    _make_tree(env.source)
    env.move()
    _write(_moved(env) / "notes.txt", b"pasted into the old copy")
    with pytest.raises(RelocateError) as first:
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    message = str(first.value)
    assert "will not delete it" in message and "remove it if you no longer need it" in message
    assert (_moved(env) / "notes.txt").read_bytes() == b"pasted into the old copy"


# ------------------------------------------------------------------ recover deletes unchecked


def test_recover_at_deleting_old_rechecks_the_old_copy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between DELETING_OLD and the tree removal; later something writes into the old
    copy; recover finishes the delete without comparing the old copy with the manifest again."""
    _make_tree(env.source)
    env.move()
    with monkeypatch.context() as m:
        m.setattr(
            relocate,
            "rmtree_reparse_point_safe",
            lambda _p: (_ for _ in ()).throw(SimulatedCrash("rmtree")),
        )
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _stage(env) == Stage.DELETING_OLD
    written = b"written into the old copy after the interrupted delete"
    _write(_moved(env) / "late.txt", written)
    try:
        recover(env.journal_path(), policy=env.policy)
    except RelocateError:
        return
    assert written in _survivors(env), "recover deleted a write the delete check never saw"


# ------------------------------------------------------------------ exclusion


def test_rollback_does_not_move_or_delete_an_excluded_project_without_approval(
    env: Env,
) -> None:
    """Decision (review pack DECISIONS 13): a rollback the user asked for brings the folder back
    as the programs left it, including a folder named like an excluded project that was created
    through the junction after the move. Refusing would strand that data on the other drive;
    the requirement here is that it is never lost and lands at its original path."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "zzexclproj" / "main.py", b"print('excluded project')")
    rollback(env.journal_path())
    assert (env.source / "zzexclproj" / "main.py").read_bytes() == b"print('excluded project')"


@pytest.mark.parametrize("kill", ["before_freeze_rename", "before_rename_back"])
def test_following_the_freeze_race_instruction_closes_the_move(
    env: Env, monkeypatch: pytest.MonkeyPatch, kill: str
) -> None:
    """The B4 M3 race plus a kill on the freeze path. 'before_freeze_rename': the journal says
    freeze_attempted, the program's new folder is at the source; recover says to empty and
    remove that folder (the program's live data) and roll back again. Doing exactly that still
    never closes the move. 'before_rename_back': the program's folder is stranded at
    .reclaim-moved and every recover refuses to touch it."""
    _make_tree(env.source)
    aside = env.c / "data-renamed-by-program"
    state = _race_policy(env, aside)
    real_rename = relocate._rename
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)

    def rename(src: str, dst: str) -> None:
        if state["done"]:
            if kill == "before_freeze_rename" and relocate._key(dst) == mkey:
                raise SimulatedCrash(kill)
            if kill == "before_rename_back" and relocate._key(dst) == skey:
                raise SimulatedCrash(kill)
        real_rename(src, dst)

    def hook(name: str) -> None:
        if name == "after_verify":
            state["armed"] = True

    with monkeypatch.context() as m:
        m.setattr(relocate, "_rename", rename)
        with pytest.raises(SimulatedCrash):
            env.move(checkpoint=hook)
    steps = []
    for _ in range(3):
        try:
            out = recover(env.journal_path(), policy=env.policy)
        except RelocateError as exc:
            steps.append(str(exc))
            if "Move that folder's contents elsewhere" in str(exc):
                keep = env.c / "program-data-moved-out"
                os.rename(_x(env.source), _x(keep))  # contents elsewhere, folder removed
            continue
        if out.journal.stage == Stage.ROLLED_BACK:
            break
    else:
        pytest.fail(f"never closed; messages: {steps}")
    assert _exists(aside / "a.bin"), "the original renamed away by the program is gone"


# ------------------------------------------------------------------ copy-back rollback (add. 5)


def test_a_rollback_without_room_to_copy_back_changes_nothing_and_offers_the_old_copy(
    env: Env,
) -> None:
    _make_tree(env.source)
    env.move()
    expected = _manifest(env.target)
    with pytest.raises(RelocateError, match="use_old_copy") as info:
        rollback(env.journal_path(), free_bytes=lambda _p: 0)
    assert "Nothing changed" in str(info.value) or "nothing changed" in str(info.value)
    assert _is_link(env.source) and _manifest(env.target) == expected
    assert not _exists(str(env.source) + relocate.RESTORE_SUFFIX)


def test_a_write_between_the_copy_back_and_the_junction_removal_reaches_the_restored_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    real = relocate._rebuild_restore

    def rebuild_then_write(journal: relocate.Journal) -> None:
        real(journal)
        _write(env.target / "sub" / "late.txt", b"written while the copy-back ran")

    monkeypatch.setattr(relocate, "_rebuild_restore", rebuild_then_write)
    rollback(env.journal_path())
    assert (env.source / "sub" / "late.txt").read_bytes() == b"written while the copy-back ran"
    assert not _exists(env.target)


def test_a_rollback_stopped_after_the_restore_finishes_cleanly_next_time(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"through the junction")
    expected = _manifest(env.source)
    real = relocate._discard_copy

    def die(_journal: relocate.Journal) -> None:
        raise SimulatedCrash("after the restore was renamed into place")

    monkeypatch.setattr(relocate, "_discard_copy", die)
    with pytest.raises(SimulatedCrash):
        rollback(env.journal_path())
    monkeypatch.setattr(relocate, "_discard_copy", real)
    outcome = rollback(env.journal_path())
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected
    assert not _exists(str(env.source) + relocate.RESTORE_SUFFIX)
    assert not _exists(env.target)
