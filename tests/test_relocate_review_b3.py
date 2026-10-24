from __future__ import annotations

# ruff: noqa: F811, PTH101, PTH102, PTH106, PTH116, PTH118, PTH123
# Hostile-review probes for reclaim.relocate (verifier B3). Each test asserts a safety claim;
# a failure on the current code demonstrates the finding.
import os
import shutil
import stat
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
from reclaim.config import Config, ExclusionsConfig, SafetyConfig
from reclaim.relocate import (
    MOVED_SUFFIX,
    PROBE_SUFFIX,
    Policy,
    RelocateError,
    Stage,
    delete_old_copy,
    execute_move,
    recover,
    rollback,
)
from reclaim.safety import SafetyValidator

OLD_NS = 1_600_000_000 * 10**9


def _moved(e: Env) -> Path:
    return Path(str(e.source) + MOVED_SUFFIX)


def _rmtree(path: Path) -> None:
    for dirpath, _dirs, files in os.walk(_x(path)):
        for name in files:
            os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(_x(path))


def _stage(e: Env) -> str:
    return relocate.Journal.load(e.journal_path()).stage


# ------------------------------------------------------------------ identity


def test_source_recreated_before_the_freeze_leaves_a_rollbackable_move(env: Env) -> None:
    """A program wipes and recreates its folder during the copy (no handle open at the freeze).
    The freeze renames the NEW folder (new file ID) to .reclaim-moved without checking it is
    the folder whose ID the journal recorded. The move reports done, and from then on rollback
    refuses ('not the folder this move renamed') and delete_old_copy refuses: no way back."""
    _make_tree(env.source)

    def during(name: str) -> None:
        if name == "after_verify":
            _rmtree(env.source)
            _write(env.source / "fresh.bin", b"the program's new content")

    try:
        env.move(checkpoint=during)
    except RelocateError:
        assert not _is_link(env.source) and set(_manifest(env.source)) == {"fresh.bin"}
        return
    expected = _manifest(env.target)
    outcome = rollback(env.journal_path())  # raises on the current code
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_a_racing_writer_during_the_moves_probe_is_not_journalled_rolled_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move's own probe: the source is at .reclaim-probe when a program recreates the
    original path, so the rename back fails. The journal is then saved as ROLLED_BACK 'nothing
    changed' while the original sits at .reclaim-probe: recover says nothing to recover, and the
    overlap check ignores the journal."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    real = relocate._rename
    fired = {"done": False}

    def racing(a: str, b: str) -> None:
        if not fired["done"] and a.endswith(PROBE_SUFFIX):
            fired["done"] = True
            _write(env.source / "app.log", b"recreated by the program")
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", racing)
    with pytest.raises(RelocateError):
        env.move()
    monkeypatch.setattr(relocate, "_rename", real)
    stage = _stage(env)
    if stage == Stage.ROLLED_BACK:
        out = recover(env.journal_path())
        assert _manifest(env.source) == expected, (
            f"journal says {stage} ({out.message!r}) but the original is at "
            f"{env.source}{PROBE_SUFFIX}: {_exists(str(env.source) + PROBE_SUFFIX)}"
        )


def test_kill_inside_the_moves_probe_then_recreated_folder_names_the_real_location(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killed between the probe's two renames; the program recreates its folder (non-empty).
    Rollback refuses, saying the original data is safe in the TARGET, which does not exist:
    the original is at <name>.reclaim-probe."""
    _make_tree(env.source)
    real = relocate._rename

    def die(a: str, b: str) -> None:
        if a.endswith(PROBE_SUFFIX):
            raise SimulatedCrash("probe")
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", die)
    with pytest.raises(SimulatedCrash):
        env.move()
    monkeypatch.setattr(relocate, "_rename", real)
    _write(env.source / "app.log", b"recreated by the program")
    with pytest.raises(RelocateError) as info:
        rollback(env.journal_path())
    msg = str(info.value)
    assert not (str(env.target) in msg and not _exists(env.target)), (
        f"message points at a folder that does not exist: {msg!r}"
    )


def test_kill_inside_the_moves_probe_then_empty_folder_restores_in_one_rollback(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same kill, the program recreates an EMPTY folder. _settle_source removes it, but the
    restore loop only looks at the old copy / restore, never the probe path, so the first
    rollback fails with 'the original data is in <target>' (a folder that does not exist)."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    real = relocate._rename

    def die(a: str, b: str) -> None:
        if a.endswith(PROBE_SUFFIX):
            raise SimulatedCrash("probe")
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", die)
    with pytest.raises(SimulatedCrash):
        env.move()
    monkeypatch.setattr(relocate, "_rename", real)
    os.mkdir(_x(env.source))
    try:
        rollback(env.journal_path())
    except RelocateError as exc:
        pytest.fail(f"first rollback failed: {exc} (target exists: {_exists(env.target)})")
    assert _manifest(env.source) == expected


# ------------------------------------------------------------------ delete_old_copy


def test_older_bytes_written_into_the_old_copy_refuse_the_delete(env: Env) -> None:
    """A file of the old copy whose mtime is OLDER than the target's passes without a hash:
    nothing checks that the target side is the one that changed. Bytes pasted into the old
    copy with an older mtime (Explorer keeps mtimes) are deleted without any prompt."""
    _make_tree(env.source)
    env.move()
    path = _moved(env) / "sub" / "b.txt"
    _write(path, b"only copy of these bytes")
    os.utime(_x(path), ns=(OLD_NS, OLD_NS))
    try:
        delete_old_copy(env.journal_path(), confirmed=True)
    except RelocateError:
        return
    pytest.fail("the old copy's only version of sub\\b.txt was deleted")


def test_a_pasted_file_with_an_old_mtime_is_never_accepted_as_removed(env: Env) -> None:
    """H4/S5 says a file written into the old copy after the move refuses the delete even with
    accept_missing. 'Written after the move' is judged by mtime only, so a pasted file (mtime
    kept) is called 'no longer in the moved folder' and deleted under accept_missing."""
    _make_tree(env.source)
    env.move()
    path = _moved(env) / "pasted.txt"
    _write(path, b"only copy")
    os.utime(_x(path), ns=(OLD_NS, OLD_NS))
    try:
        delete_old_copy(env.journal_path(), confirmed=True, accept_missing=True)
    except RelocateError:
        return
    pytest.fail("a file added to the old copy after the move was deleted as 'removed'")


def test_a_folder_attribute_set_in_the_old_copy_is_compared(env: Env) -> None:
    """The docstring says folder streams AND attributes are compared; only streams are."""
    _make_tree(env.source)
    env.move()
    relocate._set_attributes(_x(_moved(env) / "sub"), stat.FILE_ATTRIBUTE_HIDDEN)
    try:
        delete_old_copy(env.journal_path(), confirmed=True)
    except RelocateError:
        return
    hidden = os.stat(_x(env.target / "sub"), follow_symlinks=False).st_file_attributes
    assert hidden & stat.FILE_ATTRIBUTE_HIDDEN, "the old copy's folder attribute was not compared"


# ------------------------------------------------------------------ rollback


def test_rollback_from_a_half_deleted_old_copy_keeps_folder_streams(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_old_copy killed mid-rmtree, then the user rolls back. _sync_back copies the
    missing files and makedirs the missing folders, but not their streams or attributes; the
    target is then discarded because its FILES all match."""
    _make_tree(env.source)
    with open(_x(env.source / "sub") + ":meta", "wb") as fh:
        fh.write(b"folder metadata")
    relocate._set_attributes(_x(env.source / "sub"), stat.FILE_ATTRIBUTE_HIDDEN)
    expected = _manifest(env.source)
    env.move()
    real = relocate.rmtree_reparse_point_safe

    def partial(path: str) -> None:
        real(_x(_moved(env) / "sub"))
        raise SimulatedCrash("rmtree")

    monkeypatch.setattr(relocate, "rmtree_reparse_point_safe", partial)
    with pytest.raises(SimulatedCrash):
        delete_old_copy(env.journal_path(), confirmed=True)
    monkeypatch.setattr(relocate, "rmtree_reparse_point_safe", real)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected
    streams = relocate._named_streams(_x(env.source / "sub"))
    attrs = os.stat(_x(env.source / "sub"), follow_symlinks=False).st_file_attributes
    target_kept = _exists(env.target)
    assert streams or target_kept, "sub's folder stream is gone and the target was discarded"
    assert attrs & stat.FILE_ATTRIBUTE_HIDDEN or target_kept, "sub's hidden bit is gone"


def test_rollback_keeps_a_folder_stream_written_through_the_junction(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    os.mkdir(_x(env.source / "newdir"))
    with open(_x(env.source / "newdir") + ":meta", "wb") as fh:
        fh.write(b"written through the junction")
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    kept = _exists(env.target)
    assert relocate._named_streams(_x(env.source / "newdir")) or kept, (
        "the folder stream written after the move is gone and the target was discarded"
    )


def test_rollback_keeps_an_attribute_change_made_through_the_junction(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    relocate._set_attributes(_x(env.source / "a.bin"), stat.FILE_ATTRIBUTE_HIDDEN)
    rollback(env.journal_path())
    attrs = os.stat(_x(env.source / "a.bin"), follow_symlinks=False).st_file_attributes
    assert attrs & stat.FILE_ATTRIBUTE_HIDDEN or _exists(env.target), (
        "the hidden bit set through the junction is lost and the target was discarded"
    )


def test_rollback_after_a_folder_became_a_file_through_the_junction(env: Env) -> None:
    """A program replaced sub\\deeper (a folder) with a file of that name. _sync_back opens the
    old copy's FOLDER for writing and fails, before the junction is removed, on every retry:
    rollback can never finish and the error is a raw OSError with no next step."""
    _make_tree(env.source)
    env.move()
    _rmtree(env.source / "sub" / "deeper")
    _write(env.source / "sub" / "deeper", b"now a file")
    errors = []
    for _ in range(2):
        try:
            out = rollback(env.journal_path())
            break
        except (RelocateError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    else:
        pytest.fail(" || ".join(errors))
    assert out.journal.stage == Stage.ROLLED_BACK


def test_rollback_with_the_target_gone_restores_the_intact_old_copy(env: Env) -> None:
    """The target was deleted or its drive unplugged; the old copy is intact. Rollback
    refuses ('the moved data cannot be copied back') on every attempt, leaving the original
    path a dangling junction, and the message gives no way back to the old copy."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    _rmtree(env.target)
    try:
        out = rollback(env.journal_path())
    except RelocateError as exc:
        pytest.fail(f"rollback refused with the old copy intact: {exc}")
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_settle_source_never_removes_an_empty_folder_holding_a_stream(env: Env) -> None:
    """After DONE something removed the junction and made a folder at the original path that
    has no entries but carries a named stream (its own data). _settle_source checks only for
    entries, so it rmdirs the folder and the stream's data is gone."""
    _make_tree(env.source)
    env.move()
    os.rmdir(_x(env.source))  # the junction entry only
    os.mkdir(_x(env.source))
    with open(_x(env.source) + ":settings", "wb") as fh:
        fh.write(b"the program's own data")
    try:
        rollback(env.journal_path())
    except RelocateError:
        return
    assert relocate._named_streams(_x(env.source)), "the folder's stream data was removed"


# ------------------------------------------------------------------ recover


def test_recover_after_a_kill_before_keep_root_flags_keeps_the_hidden_bit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    relocate._set_attributes(_x(env.source), stat.FILE_ATTRIBUTE_HIDDEN)

    def die(_moved: str, _link: str) -> None:
        raise SimulatedCrash("keep_root_flags")

    monkeypatch.setattr(relocate, "_keep_root_flags", die)
    with pytest.raises(SimulatedCrash):
        env.move()
    monkeypatch.undo()
    out = recover(env.journal_path())
    assert out.journal.stage == Stage.DONE
    attrs = os.stat(_x(env.source), follow_symlinks=False).st_file_attributes
    assert attrs & stat.FILE_ATTRIBUTE_HIDDEN, "recover finished the move without the hidden bit"


# ------------------------------------------------------------------ exclusion


def test_a_deny_path_appearing_during_the_copy_is_not_moved(tmp_path: Path, monkeypatch) -> None:
    """The M2 re-check runs on the frozen path (<name>.reclaim-moved\\...). A [safety] deny
    pattern written against the real path no longer matches there, so a denied folder that
    appeared during the copy is moved. Only *name* patterns survive the rename."""
    e = Env(tmp_path)
    d_key = relocate._key(e.d)
    monkeypatch.setattr(
        relocate, "_volume_serial", lambda p: 2 if relocate._key(p).startswith(d_key) else 1
    )
    config = Config(
        safety=SafetyConfig(deny=["*/data/keepme*"]),
        exclusions=ExclusionsConfig(project_names=["zzexclproj"]),
    )
    validator = SafetyValidator(config)
    policy = Policy(
        exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
        protected=lambda p: False,
    )
    _make_tree(e.source)
    assert validator.exclusion_match(e.source / "keepme", is_dir=True) is not None

    def during(name: str) -> None:
        if name == "after_copy":
            _write(e.source / "keepme" / "secret.txt", b"denied")

    try:
        execute_move(
            e.source, e.target, policy, e.journals, free_bytes=lambda _p: BIG, checkpoint=during
        )
    except RelocateError:
        return
    assert not _exists(e.target / "keepme"), "a denied folder was moved behind the junction"
    assert _is_link(e.source)


# ------------------------------------------------------------------ pins from mutation (rl3)


def test_a_folder_stream_written_through_the_junction_is_copied_back_and_the_copy_removed(
    env: Env,
) -> None:
    """Mutation T8: copy-back must carry folder streams, so the target is provably redundant
    and removed (not merely kept as a fallback)."""
    _make_tree(env.source)
    env.move()
    with open(_x(env.source / "sub") + ":meta", "wb") as fh:
        fh.write(b"written through the junction")
    rollback(env.journal_path())
    assert relocate._named_streams(_x(env.source / "sub")) == [":meta:$DATA"]
    assert not _exists(env.target)


def test_a_copy_whose_folder_metadata_could_not_be_brought_back_is_kept(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation T9: if folder metadata differs after copy-back, the target is kept."""
    _make_tree(env.source)
    env.move()
    with open(_x(env.source / "sub") + ":meta", "wb") as fh:
        fh.write(b"written through the junction")
    monkeypatch.setattr(relocate, "_copy_folder_meta", lambda _s, _d: None)
    outcome = rollback(env.journal_path())
    assert _exists(env.target)
    assert outcome.kept and "differ" in outcome.kept[1]


def test_a_foreign_folder_at_the_restore_name_after_a_planned_restore_is_not_used(
    env: Env,
) -> None:
    """Mutation T12: a restore intent was saved but no ID recorded; a non-empty folder at that
    name is not ours and must not be removed."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True)
    journal = relocate.Journal.load(env.journal_path())
    journal.restore_planned = True
    journal.restore_id = 0
    journal.save()
    restore = Path(str(env.source) + relocate.RESTORE_SUFFIX)
    _write(restore / "someone-elses.txt", b"not ours")
    with pytest.raises(RelocateError, match="not made by this rollback"):
        rollback(env.journal_path())
    assert (restore / "someone-elses.txt").read_bytes() == b"not ours"


def test_an_empty_folder_recreated_inside_the_moves_probe_is_settled_at_once(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation T13: when a program recreates an empty folder between the probe's renames, the
    failed move itself puts the original back: no recover needed."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    real = relocate._rename
    fired = {"done": False}

    def racing(a: str, b: str) -> None:
        if not fired["done"] and a.endswith(relocate.PROBE_SUFFIX):
            fired["done"] = True
            os.mkdir(_x(env.source))
        real(a, b)

    monkeypatch.setattr(relocate, "_rename", racing)
    with pytest.raises(RelocateError):
        env.move()
    monkeypatch.setattr(relocate, "_rename", real)
    assert _manifest(env.source) == expected
    assert relocate.Journal.load(env.journal_path()).stage == Stage.ROLLED_BACK
