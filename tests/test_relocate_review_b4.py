from __future__ import annotations

# ruff: noqa: F811, PTH101, PTH102, PTH104, PTH106, PTH118, PTH123
# Hostile-review probes for reclaim.relocate (verifier B4, head 463f6da). Each test asserts a
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
from test_relocate_review_b2 import _arm  # type: ignore[import-not-found]

import reclaim.relocate as relocate
from reclaim.relocate import (
    MOVED_SUFFIX,
    PROBE_SUFFIX,
    RelocateError,
    Stage,
    delete_old_copy,
    recover,
    rollback,
)


def _moved(e: Env) -> Path:
    return Path(str(e.source) + MOVED_SUFFIX)


def _rmtree(path: Path) -> None:
    for dirpath, _dirs, files in os.walk(_x(path)):
        for name in files:
            os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(_x(path))


def _all_bytes(root: Path) -> list[bytes]:
    """Every file's content anywhere under `root` (links not followed)."""
    out = []
    for dirpath, dirs, files in os.walk(_x(root)):
        dirs[:] = [d for d in dirs if not _is_link(os.path.join(dirpath, d))]
        for name in files:
            with open(os.path.join(dirpath, name), "rb") as fh:
                out.append(fh.read())
    return out


def _stage(e: Env) -> str:
    return relocate.Journal.load(e.journal_path()).stage


def _patch_stat_failing(monkeypatch: pytest.MonkeyPatch, key: str, exc: type[OSError]) -> None:
    """os.stat(follow_symlinks=False) on exactly `key` raises `exc` (a transient error such as
    a sharing violation, an ACL deny, or a drive still spinning up); everything else is real."""
    real = os.stat

    def fake(path, *a, **k):  # type: ignore[no-untyped-def]
        if isinstance(path, str) and relocate._key(path) == key:
            raise exc(13, "simulated transient error", path)
        return real(path, *a, **k)

    monkeypatch.setattr(relocate.os, "stat", fake)


# ------------------------------------------------------------------ copy-back over the old copy


def test_rollback_never_overwrites_a_write_made_into_the_old_copy(env: Env) -> None:
    """The design treats writes into the old copy as real (delete_old_copy refuses them: B2 H4,
    B3 H3). Rollback's _sync_back copies every target file whose size/mtime differs from the
    old copy's OVER the old copy, so a newer edit made in the old copy after the move is
    replaced by the target's stale version, and _discard_copy then deletes the target too."""
    _make_tree(env.source)
    env.move()
    edit = b"edited in the old copy after the move -- only copy of this text"
    _write(_moved(env) / "sub" / "b.txt", edit)
    # delete_old_copy agrees this is data: it refuses.
    with pytest.raises(RelocateError):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    # Addendum 5: rollback never writes into the old copy; it is kept as it is.
    survivors = _all_bytes(env.source) + _all_bytes(_moved(env))
    if _exists(env.target):
        survivors += _all_bytes(env.target)
    assert edit in survivors, (
        f"the edit made in the old copy is gone; source b.txt is now "
        f"{(env.source / 'sub' / 'b.txt').read_bytes()!r}; target exists: {_exists(env.target)}"
    )


def test_rollback_never_removes_a_folder_stream_added_to_the_old_copy(env: Env) -> None:
    """_sync_back's metadata copy-back calls _copy_folder_meta(target, old), which removes every
    named stream of the old copy's folder that the target lacks: a stream written into the old
    copy after the move (data delete_old_copy refuses to delete) is destroyed by rollback."""
    _make_tree(env.source)
    env.move()
    sub = _x(_moved(env) / "sub")
    with open(sub + ":note", "wb") as fh:
        fh.write(b"stream written into the old copy")
    with pytest.raises(RelocateError):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    rollback(env.journal_path())
    streams = relocate._named_streams(_x(env.source / "sub"))
    streams += relocate._named_streams(_x(_moved(env) / "sub"))
    left = [p for p in (env.target,) if _exists(p)]
    assert ":note:$DATA" in streams or left, (
        f"the folder stream written into the old copy is gone: {streams}"
    )


# ------------------------------------------------------------------ missing-target branches


def test_a_transient_stat_error_on_the_target_does_not_drop_post_move_writes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_exists() is `_lstat() is not None`, and _lstat swallows EVERY OSError. A transient
    error on the target (sharing violation, ACL deny, drive spinning up) fires the
    missing-target branch while the target is there: the junction is removed and the stale old
    copy restored as ROLLED_BACK, and a write made through the junction after the move is not
    brought back. Expected: refuse (the target is not proven missing) or copy it back."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"edited through the junction")
    with monkeypatch.context() as m:
        _patch_stat_failing(m, relocate._key(env.target), PermissionError)
        try:
            out = rollback(env.journal_path())
        except (RelocateError, OSError):
            return  # refused: fine
    assert (env.source / "sub" / "b.txt").read_bytes() == b"edited through the junction", (
        f"rollback finished ({out.journal.stage}: {out.message!r}) with the target reachable "
        "all along, and the post-move edit is not in the restored folder"
    )


def test_junction_gone_and_target_unreachable_is_not_reported_as_a_plain_restore(
    env: Env,
) -> None:
    """The junction was removed by something else and the target's drive is unplugged. The
    elif branch (junction_attempted, src absent) skips the copy-back because the target is
    missing, restores the old copy and says only 'restored the original folder': no word that
    every write made since the move is on the unreachable target (the M4 branch says so, this
    one does not)."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"edited through the junction")
    os.rmdir(_x(env.source))  # something removed the junction
    unplugged = env.d / "unplugged"
    os.rename(_x(env.target), _x(unplugged))  # the drive is gone
    try:
        out = rollback(env.journal_path())
    except RelocateError:
        return
    assert str(env.target) in out.message or "reach" in out.message, (
        f"rollback reported {out.message!r}; the post-move writes are only on {env.target}"
    )


# ------------------------------------------------------------------ freeze identity race


def test_a_folder_replaced_after_the_id_check_leaves_a_rollbackable_move_frozen_policy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same race as above, via the frozen Policy dataclass."""
    _make_tree(env.source)
    real = env.policy.exclusion_match
    state = {"armed": False, "done": False}
    aside = env.c / "data-renamed-by-program"

    def hook(name: str) -> None:
        if name == "after_verify":
            state["armed"] = True

    def excl(path: Path) -> str | None:
        if (
            state["armed"]
            and not state["done"]
            and relocate._key(path) == relocate._key(env.source)
        ):
            state["done"] = True
            os.rename(_x(env.source), _x(aside))
            _write(env.source / "fresh.txt", b"the program's new folder")
        return real(path)

    object.__setattr__(env.policy, "exclusion_match", excl)
    try:
        env.move(checkpoint=hook)
    except RelocateError:
        return
    assert state["done"]
    assert _stage(env) == Stage.DONE
    try:
        rollback(env.journal_path())
    except RelocateError as exc:
        pytest.fail(f"the move finished DONE but can never be rolled back: {exc}")


# ------------------------------------------------------------------ conflict set-aside


def test_conflict_set_aside_never_merges_into_a_target_folder_of_the_same_name(
    env: Env,
) -> None:
    """_free_name only checks the OLD copy for '<name>.reclaim-conflict'. If the target has a
    folder of that name, the old folder is renamed onto that name and the target's files are
    then copied over the set-aside content: the old copy's file of the same name is lost."""
    _make_tree(env.source)
    _write(env.source / "d" / "x.txt", b"ORIGINAL content kept in the old copy")
    env.move()
    _rmtree(env.source / "d")
    _write(env.source / "d", b"a program replaced the folder with a file")
    _write(env.source / "d.reclaim-conflict" / "x.txt", b"other")
    rollback(env.journal_path())
    survivors = _all_bytes(env.source) + _all_bytes(_moved(env))
    if _exists(env.target):
        survivors += _all_bytes(env.target)
    assert b"ORIGINAL content kept in the old copy" in survivors, (
        "the set-aside folder's file was overwritten by the target's file of the same name"
    )


# ------------------------------------------------------------------ _finish_delete false success


def test_delete_old_copy_never_reports_deleted_when_the_old_copy_is_still_there(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_finish_delete runs rmtree only `if _exists(moved)`; a transient stat error makes it skip
    the delete and journal DELETED_OLD 'deleted the old copy' with the folder still on disk."""
    _make_tree(env.source)
    env.move()
    j = relocate.Journal.load(env.journal_path())
    j.log(Stage.DELETING_OLD, "verified (simulated)")
    with monkeypatch.context() as m:
        _patch_stat_failing(m, relocate._key(_moved(env)), PermissionError)
        try:
            out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
        except (RelocateError, OSError):
            return
    assert not (out.journal.stage == Stage.DELETED_OLD and _exists(_moved(env))), (
        f"journal says {out.journal.stage} ({out.message!r}) but {_moved(env)} still exists"
    )


# ------------------------------------------------------------------ kill sweeps


def _vol(monkeypatch: pytest.MonkeyPatch, e: Env) -> None:
    d_key = relocate._key(e.d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p, _d=d_key: 2 if relocate._key(p).startswith(_d) else 1
    )


def _arm_deletes(monkeypatch: pytest.MonkeyPatch, at: int, inside: str) -> None:
    """Kill on the `at`-th os.unlink/os.rmdir/os.remove whose path is inside `inside` (so a kill
    lands in the middle of shutil.rmtree, leaving a half-deleted old copy)."""
    count = {"n": 0}
    for name in ("unlink", "rmdir", "remove"):
        real = getattr(os, name)

        def wrapped(path, *a, _real=real, _name=name, **k):  # type: ignore[no-untyped-def]
            if isinstance(path, str) and relocate._key(path).startswith(inside):
                count["n"] += 1
                if count["n"] == at:
                    raise SimulatedCrash(f"os.{_name}")
            return _real(path, *a, **k)

        monkeypatch.setattr(os, name, wrapped)


@pytest.mark.parametrize("after", ["recover", "rollback"])
def test_a_kill_anywhere_in_delete_old_copy_is_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: str
) -> None:
    at = 1
    failures: list[str] = []
    phase = "ops"
    while True:
        base = tmp_path / f"d{phase}{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        _write(e.source / "sub" / "b.txt", b"edited through the junction")
        expected = _manifest(e.target)
        with monkeypatch.context() as m:
            if phase == "ops":
                _arm(m, at)
            else:
                _arm_deletes(m, at, relocate._key(_moved(e)))
            try:
                delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            if phase == "ops":
                phase, at = "rmtree", 1
                continue
            break
        try:
            if after == "recover":
                out = recover(e.journal_path(), policy=e.policy)
                if out.journal.stage == Stage.DONE:
                    out = delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
                ok = (
                    out.journal.stage == Stage.DELETED_OLD
                    and _is_link(e.source)
                    and _manifest(e.target) == expected
                    and not _exists(_moved(e))
                )
            else:
                for _ in range(2):
                    out = rollback(e.journal_path())
                ok = (
                    out.journal.stage == Stage.ROLLED_BACK
                    and not _is_link(e.source)
                    and _manifest(e.source) == expected
                )
            if not ok:
                failures.append(f"{phase} kill at {at} ({where}): wrong state {out.message}")
        except (RelocateError, OSError) as exc:
            failures.append(
                f"{phase} kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:200]}"
            )
        at += 1
    assert not failures, " || ".join(failures)


@pytest.mark.parametrize("race", ["empty", "busy"])
def test_a_kill_anywhere_in_the_probe_failure_path_then_recover_is_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """The move's probe fails because a program recreates the folder between the probe's two
    renames ('empty': an empty folder, which rollback settles; 'busy': the first rename fails,
    InUseError). A kill anywhere in that path, then recover (and, for a non-empty race, the
    user's documented step), must end with the original whole at its path."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"p{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        expected = _manifest(e.source)
        with monkeypatch.context() as m:
            real = relocate._rename
            fired = {"done": False}

            def racing(a: str, b: str, _e=e, _real=real, _f=fired) -> None:
                if not _f["done"] and a.endswith(PROBE_SUFFIX):
                    _f["done"] = True
                    os.mkdir(_x(_e.source))
                if race == "busy" and b.endswith(PROBE_SUFFIX):
                    raise PermissionError(32, "in use", a)
                _real(a, b)

            m.setattr(relocate, "_rename", racing)
            _arm(m, at)
            try:
                e.move()
                crashed = False
                where = ""
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
            except RelocateError:
                crashed = False
        paths = sorted(e.journals.glob("relocate-*.json"))
        try:
            out = recover(paths[0], policy=e.policy) if paths else None
            ok = (
                (out is None or out.journal.stage == Stage.ROLLED_BACK)
                and not _is_link(e.source)
                and _manifest(e.source) == expected
                and not _exists(str(e.source) + PROBE_SUFFIX)
                and not _exists(e.target)
            )
            if not ok:
                failures.append(
                    f"kill at {at} ({where}): wrong state "
                    f"{out.journal.stage if out else None} {out.message if out else ''}"
                )
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:200]}")
        if not crashed:
            break
        at += 1
    assert at > 2
    assert not failures, " || ".join(failures)


def test_a_kill_anywhere_in_the_move_with_a_post_move_old_copy_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The b2 move sweep, extended: after a kill and recover that finishes the move, the
    manifest must let delete_old_copy succeed (a stale or missing manifest after recover would
    block the delete forever), and the old copy it deletes must equal the target."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"m{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
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
        paths = sorted(e.journals.glob("relocate-*.json"))
        try:
            if paths:
                out = recover(paths[0], policy=e.policy)
                if out.journal.stage == Stage.DONE:
                    out = delete_old_copy(paths[0], policy=e.policy, confirmed=True)
                    ok = (
                        out.journal.stage == Stage.DELETED_OLD
                        and _manifest(e.target) == expected
                        and _is_link(e.source)
                    )
                else:
                    ok = (
                        out.journal.stage == Stage.ROLLED_BACK
                        and _manifest(e.source) == expected
                        and not _exists(e.target)
                    )
            else:
                ok = _manifest(e.source) == expected
            if not ok:
                failures.append(f"kill at {at} ({where}): wrong state")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:200]}")
        at += 1
    assert at > 10
    assert not failures, " || ".join(failures)


# ------------------------------------------------------------------ messages


def test_an_unreadable_manifest_says_what_to_do(env: Env) -> None:
    """A missing or corrupt manifest refuses the delete (right), but the message gives no next
    step and nothing can ever rebuild the record: the old copy can then never be deleted by the
    tool. Expected: an instruction (roll back, or how to re-verify)."""
    _make_tree(env.source)
    env.move()
    manifest = relocate.Journal.load(env.journal_path()).manifest
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with pytest.raises(RelocateError) as info:
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    msg = str(info.value)
    assert any(w in msg.lower() for w in ("roll back", "rollback", "yourself", "retry")), (
        f"no next step in: {msg!r}"
    )


def test_a_file_where_the_folder_was_gives_a_relocate_error_not_a_raw_oserror(
    env: Env,
) -> None:
    """Killed after the freeze; a program then writes a FILE at the original path. _settle_source
    calls _is_empty() on it, which scandirs a file: a raw NotADirectoryError with no next step
    (and _where_original is never reached to say where the data is)."""
    _make_tree(env.source)

    def die(name: str) -> None:
        if name == "after_freeze":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=die)
    _write(env.source, b"a program wrote a file where its folder was")
    try:
        rollback(env.journal_path())
    except RelocateError as exc:
        assert str(_moved(env)) in str(exc)
    except OSError as exc:
        pytest.fail(f"raw {type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ pins from mutation (rl4)


def test_a_folder_stream_edited_in_the_old_copy_is_not_replaced_by_the_targets(env: Env) -> None:
    """Both sides changed the same folder stream after the move. The folder comes back as
    the programs left it (v2) and the old copy keeps its own version (v3): neither is lost."""
    _make_tree(env.source)
    with open(_x(env.source / "sub") + ":meta", "wb") as fh:
        fh.write(b"v1 at the move")
    env.move()
    with open(_x(env.target / "sub") + ":meta", "wb") as fh:
        fh.write(b"v2 through the junction")
    with open(_x(_moved(env) / "sub") + ":meta", "wb") as fh:
        fh.write(b"v3 in the old copy")
    rollback(env.journal_path())
    with open(_x(env.source / "sub") + ":meta", "rb") as fh:
        assert fh.read() == b"v2 through the junction"
    with open(_x(_moved(env) / "sub") + ":meta", "rb") as fh:
        assert fh.read() == b"v3 in the old copy"


def test_a_tree_removal_that_leaves_the_old_copy_is_never_reported_as_deleted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation V6: the removal returned but the folder is still there."""
    _make_tree(env.source)
    env.move()
    monkeypatch.setattr(relocate, "rmtree_reparse_point_safe", lambda _p: None)
    with pytest.raises(RelocateError, match="still there"):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert relocate.Journal.load(env.journal_path()).stage != Stage.DELETED_OLD
