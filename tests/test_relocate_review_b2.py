from __future__ import annotations

# ruff: noqa: F811, PTH101, PTH102, PTH104, PTH106, PTH116, PTH118, PTH123, PTH208
# Hostile-review probes for reclaim.relocate (verifier B2). Each test asserts a safety claim;
# a failure on the current code demonstrates the finding.
import ctypes
import json
import os
import shutil
import stat
from ctypes import wintypes
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

import reclaim.relocate as relocate
from reclaim.relocate import (
    MOVED_SUFFIX,
    RESTORE_SUFFIX,
    RelocateError,
    Stage,
    delete_old_copy,
    plan_move,
    recover,
    rollback,
)

OLD_NS = 1_600_000_000 * 10**9  # 2020: outside the delta window


def _age(root: Path) -> None:
    """Back-date every file so the delta pass relies on size/mtime only."""
    for dirpath, _dirs, files in os.walk(_x(root)):
        for name in files:
            p = os.path.join(dirpath, name)
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            os.utime(p, ns=(OLD_NS, OLD_NS))
    # keep the read-only file read-only as _make_tree made it
    ro = root / "readonly.txt"
    if _exists(ro):
        os.chmod(_x(ro), stat.S_IREAD)


def _moved(env: Env) -> Path:
    return Path(str(env.source) + MOVED_SUFFIX)


# ------------------------------------------------------------------ false success


def test_kill_after_freeze_then_recreated_folder_is_not_reported_restored(
    env: Env,
) -> None:
    """Killed after the freeze (before the junction); the program recreates its folder before
    recover runs. junction_attempted is False, so _settle_plain_source is skipped and rollback
    takes the program's folder as the restored original."""
    _make_tree(env.source)
    expected = _manifest(env.source)

    def die(name: str) -> None:
        if name == "after_freeze":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=die)
    _write(env.source / "fresh.log", b"recreated by the program")

    try:
        outcome = recover(env.journal_path())
    except RelocateError:
        return
    assert _manifest(env.source) == expected, (
        f"stage={outcome.journal.stage} message={outcome.message!r} but the source holds "
        f"{sorted(_manifest(env.source))}; original still at {_moved(env)}: {_exists(_moved(env))}"
    )


def test_kill_during_delta_then_empty_recreated_folder_is_not_reported_restored(
    env: Env,
) -> None:
    """Same as above with the commonest program reaction: an empty folder."""
    _make_tree(env.source)
    expected = _manifest(env.source)

    def die(name: str) -> None:
        if name == "after_delta":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=die)
    os.mkdir(_x(env.source))

    try:
        outcome = recover(env.journal_path())
    except RelocateError:
        return
    assert _manifest(env.source) == expected, (
        f"stage={outcome.journal.stage} message={outcome.message!r}, source is empty"
    )


def test_folder_recreated_inside_rollbacks_own_window_is_not_reported_restored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between _remove_junction and the rename of the old copy back, the original path does not
    exist; a program recreating its folder there is taken as 'restored'."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    real = relocate._sync_back
    calls = {"n": 0}

    def sync_back(target: str, moved: str, record: object = None) -> int:
        n = real(target, moved, record)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 2:  # after the junction is gone
            _write(env.source / "fresh.log", b"recreated by the program")
        return n

    monkeypatch.setattr(relocate, "_sync_back", sync_back)
    try:
        outcome = rollback(env.journal_path())
    except RelocateError:
        return
    assert _manifest(env.source) == expected, (
        f"stage={outcome.journal.stage} message={outcome.message!r}; source holds "
        f"{sorted(_manifest(env.source))}; old copy still at {_moved(env)}: "
        f"{_exists(_moved(env))}"
    )


# ------------------------------------------------------------------ removal of the only copy


def test_rebuild_restore_never_wipes_a_complete_restore_when_the_target_is_gone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old copy deleted; a rollback builds <name>.reclaim-restore (a full copy) and is killed
    before the junction is removed. The target then disappears (deleted by the user/another
    cleaner). The next rollback wipes the restore folder -- the only remaining copy -- before
    it ever checks the target exists."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)

    def die(target: str, moved: str, record: object = None) -> int:
        raise SimulatedCrash("killed in the first copy-back")

    real = relocate._sync_back
    monkeypatch.setattr(relocate, "_sync_back", die)
    with pytest.raises(SimulatedCrash):
        rollback(env.journal_path())
    monkeypatch.setattr(relocate, "_sync_back", real)
    restore = Path(str(env.source) + RESTORE_SUFFIX)
    assert _manifest(restore) == expected  # the restore is complete at this point

    os.chmod(_x(env.target / "readonly.txt"), stat.S_IWRITE)
    shutil.rmtree(_x(env.target))  # external removal of the target folder

    with pytest.raises((RelocateError, OSError)):
        rollback(env.journal_path())
    assert _exists(restore) and _manifest(restore) == expected, (
        f"the complete restore copy was wiped; left: "
        f"{_manifest(restore) if _exists(restore) else 'nothing'}"
    )


def test_delete_old_copy_does_not_delete_writes_made_to_the_old_copy(env: Env) -> None:
    """delete_old_copy treats any mtime difference as 'edited through the junction' without
    asking which side changed: newer bytes in the old copy itself are deleted."""
    _make_tree(env.source)
    env.move()
    _write(_moved(env) / "sub" / "b.txt", b"written into the old copy after the move")

    try:
        delete_old_copy(env.journal_path(), confirmed=True)
    except RelocateError:
        return
    with open(_x(env.target / "sub" / "b.txt"), "rb") as fh:
        assert fh.read() == b"written into the old copy after the move", "the write is lost"


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def test_a_writer_holding_a_share_delete_handle_does_not_lose_its_writes(env: Env) -> None:
    """Rust's std, .NET FileShare.Delete and many loggers open with FILE_SHARE_DELETE. If the
    rename probe and the freeze succeed while such a handle is open, writes after the delta pass
    land in the old copy, never in the target, and delete_old_copy then deletes them."""
    _make_tree(env.source)
    path = _x(env.source / "sub" / "b.txt")
    handle = _k32.CreateFileW(path, 0x40000000, 0x7, None, 3, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        try:
            env.move()
        except RelocateError:
            return  # refused while in use: fine
        data = b"appended by the writer after the move"
        written = wintypes.DWORD(0)
        assert _k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    finally:
        _k32.CloseHandle(handle)
    try:
        delete_old_copy(env.journal_path(), confirmed=True)
    except RelocateError:
        return
    with open(_x(env.target / "sub" / "b.txt"), "rb") as fh:
        assert fh.read().startswith(b"appended"), "the writer's bytes are gone with the old copy"


# ------------------------------------------------------------------ silent alteration


def test_an_attribute_change_before_the_freeze_reaches_the_target(env: Env) -> None:
    _make_tree(env.source)
    _age(env.source)

    def during(name: str) -> None:
        if name == "after_verify":
            relocate._set_attributes(_x(env.source / "sub" / "b.txt"), stat.FILE_ATTRIBUTE_HIDDEN)

    env.move(checkpoint=during)
    got = os.stat(_x(env.target / "sub" / "b.txt"), follow_symlinks=False).st_file_attributes
    assert got & stat.FILE_ATTRIBUTE_HIDDEN, "the hidden attribute set before the freeze is lost"


def test_a_folder_stream_added_after_the_plan_survives_delete_old_copy(env: Env) -> None:
    _make_tree(env.source)

    def during(name: str) -> None:
        if name == "after_verify":
            with open(_x(env.source / "sub") + ":meta", "wb") as fh:
                fh.write(b"folder metadata")

    env.move(checkpoint=during)
    delete_old_copy(env.journal_path(), confirmed=True)
    assert relocate._named_streams(_x(env.target / "sub")), "the folder's stream is lost"


def test_a_folder_case_rename_during_the_copy_does_not_fail_the_move(env: Env) -> None:
    _make_tree(env.source)

    def during(name: str) -> None:
        if name == "after_verify":
            os.rename(_x(env.source / "sub"), _x(env.source / "SUB"))

    outcome = env.move(checkpoint=during)
    assert outcome.journal.stage == Stage.DONE


# ------------------------------------------------------------------ exclusion


def test_an_excluded_project_appearing_during_the_copy_is_not_moved(env: Env) -> None:
    _make_tree(env.source)

    def during(name: str) -> None:
        if name == "after_copy":
            _write(env.source / "zzexclproj" / "main.py", b"print('excluded')")

    try:
        env.move(checkpoint=during)
    except RelocateError:
        return
    assert not _exists(env.target / "zzexclproj"), "the excluded project was copied and moved"


def test_the_plan_never_renames_onto_an_excluded_path(env: Env) -> None:
    excluded = env.c / "zzexclproj"
    leftover = Path(str(excluded) + relocate.PROBE_SUFFIX)
    _write(leftover / "f.txt", b"x")
    plan = plan_move(excluded, env.d / "x", env.policy, free_bytes=lambda _p: BIG)
    assert not plan.ok
    assert _exists(leftover) and not _exists(excluded), "the plan renamed onto an excluded path"


# ------------------------------------------------------------------ robustness


def test_read_only_folders_move_and_roll_back(env: Env) -> None:
    _make_tree(env.source)
    rodir = env.source / "rodir"
    _write(rodir / "f.txt", b"f")
    relocate._set_attributes(_x(rodir), stat.FILE_ATTRIBUTE_READONLY)
    expected = _manifest(env.source)
    env.move()
    outcome = rollback(env.journal_path())
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected
    assert not _exists(env.target)


def test_a_read_only_folder_deleted_before_the_freeze_does_not_fail_the_move(env: Env) -> None:
    _make_tree(env.source)
    rodir = env.source / "rodir"
    _write(rodir / "f.txt", b"f")
    relocate._set_attributes(_x(rodir), stat.FILE_ATTRIBUTE_READONLY)

    def during(name: str) -> None:
        if name == "after_verify":
            relocate._set_attributes(_x(rodir), 0)
            shutil.rmtree(_x(rodir))

    outcome = env.move(checkpoint=during)
    assert outcome.journal.stage == Stage.DONE


def test_a_journal_with_a_null_field_fails_the_plan_cleanly(env: Env) -> None:
    _make_tree(env.source)
    env.journals.mkdir()
    bad = {f: None for f in relocate.Journal.__dataclass_fields__ if f != "path"}
    bad["stage"] = "done"
    (env.journals / "relocate-bad.json").write_text(json.dumps(bad), encoding="utf-8")
    plan = plan_move(
        env.source,
        env.target,
        env.policy,
        free_bytes=lambda _p: BIG,
        run_probe=False,
        journal_dir=env.journals,
    )
    assert not plan.ok


def test_moved_root_hidden_attribute_is_kept(env: Env) -> None:
    _make_tree(env.source)
    relocate._set_attributes(_x(env.source), stat.FILE_ATTRIBUTE_HIDDEN)
    env.move()
    st = os.stat(_x(env.source), follow_symlinks=False).st_file_attributes
    assert st & stat.FILE_ATTRIBUTE_HIDDEN, "the folder is no longer hidden"
    assert _is_link(env.source)


# ------------------------------------------------------------------ kill sweep over rollback

_KILLABLE = (
    "_copy_file",
    "_rename",
    "_remove_junction",
    "rmtree_reparse_point_safe",
    "_create_junction",
    "_copy_stream",
)


def _arm(monkeypatch: pytest.MonkeyPatch, at: int) -> dict[str, int]:
    count = {"n": 0}
    for name in _KILLABLE:
        real = getattr(relocate, name)

        def wrapped(*a: object, _real=real, _name=name, **k: object) -> object:
            count["n"] += 1
            if count["n"] == at:
                raise SimulatedCrash(_name)
            return _real(*a, **k)

        monkeypatch.setattr(relocate, name, wrapped)
    real_save = relocate.Journal.save

    def save(self: relocate.Journal) -> None:
        count["n"] += 1
        if count["n"] == at:
            raise SimulatedCrash("save")
        real_save(self)

    monkeypatch.setattr(relocate.Journal, "save", save)
    return count


@pytest.mark.parametrize("delete_first", [False, True])
def test_a_kill_anywhere_in_rollback_then_recover_restores_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_first: bool
) -> None:
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"k{at}"
        base.mkdir()
        e = Env(base)
        d_key = relocate._key(e.d)
        monkeypatch.setattr(
            relocate,
            "_volume_serial",
            lambda p, _d=d_key: 2 if relocate._key(p).startswith(_d) else 1,
        )
        _make_tree(e.source)
        e.move()
        if delete_first:
            delete_old_copy(e.journal_path(), confirmed=True)
        _write(e.source / "sub" / "b.txt", b"edited through the junction")
        _write(e.source / "new.txt", b"added through the junction")
        expected = _manifest(e.target)
        with monkeypatch.context() as m:
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
            for _ in range(2):
                out = rollback(e.journal_path())
            ok = (
                out.journal.stage == Stage.ROLLED_BACK
                and not _is_link(e.source)
                and _manifest(e.source) == expected
            )
            if not ok:
                failures.append(f"kill at {at} ({where}): wrong state {out.message}")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:160]}")
        at += 1
    assert at > 3
    assert not failures, " || ".join(failures)


def Journal_hist(e: Env) -> object:
    j = relocate.Journal.load(e.journal_path())
    return (
        j.stage,
        j.history[-2:],
        sorted(os.listdir(_x(e.c))),
        _exists(e.target),
        _is_link(e.source),
    )


def test_a_kill_anywhere_in_the_move_then_recover_is_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"m{at}"
        base.mkdir()
        e = Env(base)
        d_key = relocate._key(e.d)
        monkeypatch.setattr(
            relocate,
            "_volume_serial",
            lambda p, _d=d_key: 2 if relocate._key(p).startswith(_d) else 1,
        )
        _make_tree(e.source)
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
        try:
            paths = sorted(e.journals.glob("relocate-*.json"))
            if paths:
                out = recover(paths[0])
                if out.journal.stage == Stage.DONE:
                    ok = _is_link(e.source) and _manifest(e.target) == expected
                else:
                    ok = (
                        out.journal.stage == Stage.ROLLED_BACK
                        and not _is_link(e.source)
                        and _manifest(e.source) == expected
                        and not _exists(e.target)
                    )
            else:
                ok = _manifest(e.source) == expected
            if not ok:
                failures.append(
                    f"kill at {at} ({where}): wrong state "
                    f"{out.journal.stage if paths else None} target_exists={_exists(e.target)} "
                    f"src_ok={_manifest(e.source) == expected}"
                )
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:160]}")
        at += 1
    assert at > 10
    assert not failures, " || ".join(failures)


# ------------------------------------------------------------------ pins from mutation (rl2)


def test_a_file_written_into_the_old_copy_is_never_accepted_as_removed(env: Env) -> None:
    """Mutation S5: a file that appeared in the old copy after the move is not "removed
    through the junction", so even accept_missing must not delete it."""
    _make_tree(env.source)
    env.move()
    _write(_moved(env) / "written-later.txt", b"only copy")
    with pytest.raises(RelocateError, match="changed since the move"):
        delete_old_copy(env.journal_path(), confirmed=True, accept_missing=True)
    assert (_moved(env) / "written-later.txt").read_bytes() == b"only copy"


def test_a_folder_stream_only_in_the_old_copy_refuses_the_delete(env: Env) -> None:
    """Mutation S10."""
    _make_tree(env.source)
    env.move()
    with open(_x(_moved(env) / "sub") + ":meta", "wb") as fh:
        fh.write(b"folder metadata that only the old copy has")
    with pytest.raises(RelocateError, match="changed since the move"):
        delete_old_copy(env.journal_path(), confirmed=True)
    assert _exists(_moved(env))


def test_a_volume_that_is_not_ntfs_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation S11: folder IDs, streams and junctions are NTFS behaviour."""
    _make_tree(env.source)
    monkeypatch.setattr(relocate, "_filesystem", lambda p: "exFAT" if "\\D\\" in p else "NTFS")
    plan = plan_move(env.source, env.target, env.policy, free_bytes=lambda _p: BIG)
    assert any("NTFS is required" in r for r in plan.reasons), plan.reasons


def test_a_folder_that_replaced_our_target_is_kept(env: Env) -> None:
    """Mutation S13: killed before the junction; something replaced our target folder with its
    own. Rollback restores the original and must not remove the other folder."""
    _make_tree(env.source)
    expected = _manifest(env.source)

    def die(name: str) -> None:
        if name == "after_verify":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=die)
    os.chmod(_x(env.target / "readonly.txt"), stat.S_IWRITE)
    shutil.rmtree(_x(env.target))
    _write(env.target / "someone-elses.txt", b"not ours")
    outcome = recover(env.journal_path())
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected
    assert (env.target / "someone-elses.txt").read_bytes() == b"not ours"
    assert outcome.kept and "not the folder this move made" in outcome.kept[1]


def test_a_foreign_folder_at_the_old_copys_name_is_never_used(env: Env) -> None:
    """Mutation S14: the old copy was swapped for another folder of the same name."""
    _make_tree(env.source)
    env.move()
    os.rename(_x(_moved(env)), _x(env.c / "kept-aside"))
    _write(_moved(env) / "impostor.txt", b"x")
    with pytest.raises(RelocateError, match="not the folder this move kept"):
        delete_old_copy(env.journal_path(), confirmed=True)
    with pytest.raises(RelocateError, match="not the folder this move renamed"):
        rollback(env.journal_path())
    assert _is_link(env.source)
    assert _exists(_moved(env) / "impostor.txt")


def test_nothing_restorable_is_never_reported_as_rolled_back(env: Env) -> None:
    """Mutation S17: old copy deleted, then the junction and the target both removed by
    something else. There is nothing to restore, and the rollback must say so."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    os.rmdir(_x(env.source))
    os.chmod(_x(env.target / "readonly.txt"), stat.S_IWRITE)
    shutil.rmtree(_x(env.target))
    with pytest.raises(RelocateError, match="could not restore"):
        rollback(env.journal_path())
    assert relocate.Journal.load(env.journal_path()).stage != Stage.ROLLED_BACK
