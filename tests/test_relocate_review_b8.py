from __future__ import annotations

# ruff: noqa: B017, F811, PTH101, PTH104, PTH116, PTH123, PTH202, S103, SIM105, T201, TRY301
# Hostile-review probes for reclaim.relocate (verifier B8, head 66c2df0). Each test asserts a
# safety claim; a failure on this head demonstrates a finding. "_holds" tests are interaction
# checks expected to pass (evidence for what held up).
import os
import re
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
from reclaim.config import Config, SafetyConfig
from reclaim.relocate import (
    Journal,
    Policy,
    RelocateError,
    Stage,
    delete_old_copy,
    execute_move,
    recover,
    rollback,
)
from reclaim.safety import SafetyValidator


def _paths(e: Env) -> list[Path]:
    return sorted(e.journals.glob("relocate-*.json"))


def _blind() -> Policy:
    return Policy(exclusion_match=lambda _p: None, protected=lambda _p: False)


# ------------------------------------------------------------------ L3: a rolled-back journal


def test_b8_rollback_of_a_rolled_back_and_deleted_journal_never_undoes_a_later_move(
    env: Env,
) -> None:
    """A: move, rollback, delete-old -> DELETED_OLD with rolled_back (L3: no longer blocks).
    B: moves the same folder to the same (suggested, deterministic) target -> DONE.
    The user (or the UI's per-move button) then runs `rollback A`. A's restored folder is now
    B's old copy (its ID is A.restore_id), and the junction points to A.target's path, so every
    identity check of A passes: A's rollback copies B's target back, removes B's junction and
    reports ROLLED_BACK. B's journal still says DONE."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "new.txt", b"written after the first move")
    rollback(env.journal_path())
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    (a,) = _paths(env)
    assert Journal.load(a).stage == Stage.DELETED_OLD and Journal.load(a).rolled_back
    env.move()  # B
    (b,) = [p for p in _paths(env) if p != a]
    assert Journal.load(b).stage == Stage.DONE and _is_link(env.source)
    try:
        out = rollback(a)
        detail = f"rollback A returned {out.journal.stage}: {out.message!r}"
    except RelocateError as exc:
        detail = f"rollback A refused: {exc}"
    assert _is_link(env.source), f"B's junction was removed by A's rollback ({detail})"
    assert Journal.load(b).stage == Stage.DONE


@pytest.mark.parametrize("same_target", [True, False])
def test_b8_sweep_rollback_of_the_dead_journal_at_every_stage_of_a_second_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_target: bool
) -> None:
    """A rolled back and deleted. B (same folder) killed at every point, then recovered.
    Then `rollback A`. A's rollback must never change B's state: B's journal stage and what
    sits at the folder's path must be the same before and after."""
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"l{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        rollback(e.journal_path())
        delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
        (a,) = _paths(e)
        b_target = e.target if same_target else e.d / "other"
        with monkeypatch.context() as m:
            _arm(m, at)
            try:
                execute_move(e.source, b_target, e.policy, e.journals, free_bytes=lambda _p: BIG)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        rest = [p for p in _paths(e) if p != a]
        try:
            if rest:
                recover(rest[0], policy=e.policy)
            before = (
                Journal.load(rest[0]).stage if rest else None,
                _is_link(e.source),
                _manifest(e.source) if _exists(e.source) else None,
            )
            with pytest.raises(Exception):
                rollback(a)
                raise RelocateError("returned")  # a return is checked below as well
            after = (
                Journal.load(rest[0]).stage if rest else None,
                _is_link(e.source),
                _manifest(e.source) if _exists(e.source) else None,
            )
            if before != after:
                failures.append(f"kill at {at} ({where}): B {before[:2]} -> {after[:2]}")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:120]}")
        at += 1
    print(f"B8-SWEEP points={at - 1} failures={len(failures)}")
    assert at > 10
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


# ------------------------------------------------------------------ M3: exclusions


def _path_policy(e: Env) -> tuple[Policy, SafetyValidator]:
    """A `[safety] deny` pattern naming the folder's own path (not a project name)."""
    text = re.escape(Path(e.source).resolve().as_posix())
    config = Config(safety=SafetyConfig(deny=[f"re:{text}(/|$)"]))
    validator = SafetyValidator(config)
    return (
        Policy(
            exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
            # The production wiring (`default_policy`) since B8 M-B: the same check on the path
            # text as journaled. The reviewer's original helper had only the first callable.
            exclusion_match_text=lambda p: validator.exclusion_match(p, is_dir=True, lexical=True),
            protected=lambda _p: False,
        ),
        validator,
    )


def test_b8_a_path_pattern_exclusion_of_the_moved_folder_stops_delete_old(env: Env) -> None:
    """The user excludes the folder by its path after the move. The exclusion is live (the
    plan's test hits the folder while it is a folder), but at DONE the source is a junction and
    `exclusion_match` canonicalises (resolve()) it to the TARGET's path, which the pattern does
    not name; the old copy's path ends `.reclaim-moved`, which it does not name either. The
    excluded folder's original files are deleted."""
    _make_tree(env.source)
    policy, validator = _path_policy(env)
    assert validator.exclusion_match(env.source, is_dir=True) is not None  # live, pre-move
    execute_move(env.source, env.target, _blind(), env.journals, free_bytes=lambda _p: BIG)
    before = _manifest(_moved(env))
    try:
        out = delete_old_copy(env.journal_path(), policy=policy, confirmed=True)
        detail = f"{out.journal.stage}: {out.message}"
    except RelocateError as exc:
        detail = f"refused: {exc}"
    assert _exists(_moved(env)) and _manifest(_moved(env)) == before, (
        f"the excluded folder's old copy was deleted ({detail})"
    )


def test_b8_a_path_pattern_exclusion_stops_recover_finishing_a_delete(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    policy, _ = _path_policy(env)
    execute_move(env.source, env.target, _blind(), env.journals, free_bytes=lambda _p: BIG)
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(env)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(env.journal_path(), policy=_blind(), confirmed=True)
    left = _manifest(_moved(env))
    try:
        recover(env.journal_path(), policy=policy)
    except RelocateError:
        pass
    assert _exists(_moved(env)) and _manifest(_moved(env)) == left, "recover finished the delete"


def test_b8_holds_a_project_name_in_any_case_inside_the_old_copy_stops_delete_old(
    env: Env,
) -> None:
    _make_tree(env.source)
    _write(env.source / "ZZExclProj" / "main.py", b"the excluded project's source")
    execute_move(env.source, env.target, _blind(), env.journals, free_bytes=lambda _p: BIG)
    with pytest.raises(RelocateError, match="excluded"):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _exists(_moved(env) / "ZZExclProj" / "main.py")


# ------------------------------------------------------------------ L2: finishing a delete


def _interrupted_delete(e: Env, monkeypatch: pytest.MonkeyPatch, at: int = 3) -> None:
    with monkeypatch.context() as m:
        _arm_deletes(m, at, relocate._key(_moved(e)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
    assert _stage(e) == Stage.DELETING_OLD


def test_b8_recover_does_not_finish_a_delete_while_the_moved_folder_is_unreachable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DONE, delete-old interrupted, then the target drive goes away (or its letter changes).
    `recover` finishes the delete without checking the moved folder is still there: the rest
    of the old copy, now the only reachable copy of those files, is deleted and reported as
    'deleted the old copy'."""
    _make_tree(env.source)
    env.move()
    _interrupted_delete(env, monkeypatch)
    left = _manifest(_moved(env))
    os.rename(_x(env.target), _x(env.d / "unplugged"))
    try:
        out = recover(env.journal_path(), policy=env.policy)
        detail = f"{out.journal.stage}: {out.message}"
    except RelocateError as exc:
        detail = f"refused: {exc}"
    assert _exists(_moved(env)) and _manifest(_moved(env)) == left, (
        f"the remaining old copy was deleted with the moved folder unreachable ({detail})"
    )


@pytest.mark.parametrize(
    "tamper", ["case_rename", "same_size_replace", "stream", "new_dir", "rename_into"]
)
def test_b8_holds_the_partial_delete_subset_check_refuses_any_change(
    env: Env, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """After rollback from an interrupted delete, delete-old compares only what is left. A
    user's change to what is left must still refuse it (a file that would be deleted)."""
    _make_tree(env.source)
    _write(env.source / "keep" / "k1.txt", b"k1 original")
    _write(env.source / "keep" / "k2.txt", b"k2 original")
    env.move()
    _interrupted_delete(env, monkeypatch, at=2)
    rollback(env.journal_path())
    moved = _moved(env)
    left = [p for p in moved.rglob("*") if p.is_file()]
    assert left
    victim = left[0]
    if tamper == "case_rename":
        os.rename(_x(victim), _x(victim.with_name(victim.name.upper())))
    elif tamper == "same_size_replace":
        size = os.path.getsize(_x(victim))
        st = os.stat(_x(victim))
        os.chmod(_x(victim), 0o666)
        _write(victim, b"X" * size)
        os.utime(_x(victim), ns=(st.st_atime_ns, st.st_mtime_ns))
    elif tamper == "stream":
        os.chmod(_x(victim), 0o666)
        with open(_x(victim) + ":user", "wb") as fh:
            fh.write(b"user data in a stream")
    elif tamper == "new_dir":
        _write(moved / "brand-new" / "user.txt", b"user")
    else:
        _write(moved / "user-renamed-into.txt", b"the user's own file")
    with pytest.raises(RelocateError):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _exists(moved)


# ------------------------------------------------------------------ restore_verified


def test_b8_a_verified_restore_put_back_says_newer_writes_may_be_only_on_the_target(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy-back verified the restore, then the process died before the junction was
    removed. A program kept writing through the junction (the target is the live folder). Then
    the target drive went away and the user rolled back again: the verified-but-stale restore
    is put back and the message does not say the newer writes are only on the target."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)

    def die(_link: str) -> None:
        raise SimulatedCrash("after the restore was verified")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    _write(env.source / "late.txt", b"written through the junction after the verify")
    os.rename(_x(env.target), _x(env.d / "unplugged"))
    try:
        out = rollback(env.journal_path())
    except RelocateError:
        return  # refusing is a correct answer
    assert not _exists(env.source / "late.txt")  # the stale copy really is stale
    assert str(env.target) in out.message and "only there" in out.message, out.message


# ------------------------------------------------------------------ M2: errors


def test_b8_a_failed_cleanup_of_our_restore_in_use_old_copy_is_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain rollback stopped during its copy-back leaves our partial restore (junction still
    up). The user then rolls back with use_old_copy: the old copy goes back, then the cleanup of
    our restore copy (rmtree, not wrapped) fails on a held file: a raw OSError escapes."""
    _make_tree(env.source)
    env.move()
    real = relocate._copy_file
    n = {"i": 0}

    def copy(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))):
            n["i"] += 1
            if n["i"] == 3:
                raise SimulatedCrash("in the copy-back")
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_copy_file", copy)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert _exists(_restore(env)) and _is_link(env.source)

    def refuse(_path: str) -> None:
        raise PermissionError(5, "Access is denied (simulated scanner handle)")

    with monkeypatch.context() as m:
        m.setattr(relocate, "rmtree_reparse_point_safe", refuse)
        with pytest.raises(Exception) as info:
            rollback(env.journal_path(), use_old_copy=True)
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}: {info.value}"


def test_b8_recover_while_the_cause_is_still_there_is_a_relocate_error(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M2's message says 'run recover once the cause is gone'. If the user runs it while the
    scanner handle is still open, _rollback_frozen's rename escapes raw (the CLI prints a
    traceback; the API reports 'unexpected error')."""
    _make_tree(env.source)
    mkey, skey = relocate._key(_moved(env)), relocate._key(env.source)
    real = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(a) == mkey and relocate._key(b) == skey:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        real(a, b)

    def boom(*_a: object, **_k: object) -> str:
        raise RelocateError("simulated: 1 files differ after copying them twice")

    monkeypatch.setattr(relocate, "_delta_sync", boom)
    monkeypatch.setattr(relocate, "_rename", rename)
    with pytest.raises(RelocateError):
        env.move()
    with pytest.raises(Exception) as info:
        recover(env.journal_path(), policy=env.policy)
    assert isinstance(info.value, RelocateError), f"raw {type(info.value).__name__}: {info.value}"


def test_b8_the_cli_recover_with_the_cause_still_there_prints_no_traceback(
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
        raise RelocateError("simulated")

    monkeypatch.setattr(relocate, "_delta_sync", boom)
    monkeypatch.setattr(relocate, "_rename", rename)
    common = ["relocate", "--config", str(tmp_path / "c.toml"), "--journal-dir"]
    journals = str(tmp_path / "journals")
    main([*common, journals, "move", str(src), "--target", str(d / "models"), "--yes"])
    (journal,) = relocate.journals(Path(journals))
    capsys.readouterr()
    try:
        code = main([*common, journals, "recover", journal.id])
    except Exception as exc:
        pytest.fail(f"raw {type(exc).__name__} escaped the CLI: {exc}")
    assert code == 1


# ------------------------------------------------------------------ M4: forward finish


def _killed_after_junction(e: Env) -> None:
    def hook(name: str) -> None:
        if name == "after_junction":
            raise SimulatedCrash(name)

    with pytest.raises(SimulatedCrash):
        e.move(checkpoint=hook)
    assert _stage(e) == Stage.SWAPPING


def test_b8_holds_a_foreign_folder_at_the_target_path_is_never_finished_forward(
    env: Env,
) -> None:
    _make_tree(env.source)
    _killed_after_junction(env)
    os.rename(_x(env.target), _x(env.d / "ours-set-aside"))
    _write(env.target / "foreign.txt", b"another folder at the same path")
    out = None
    try:
        out = recover(env.journal_path(), policy=env.policy)
    except RelocateError:
        pass
    assert out is None or out.journal.stage != Stage.DONE, out.message if out else ""
    assert _manifest(_moved(env))  # the old copy is untouched


def test_b8_holds_a_kill_before_the_junction_reparse_data_is_never_finished_forward(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    with monkeypatch.context() as m:
        m.setattr(
            relocate._kernel32,
            "DeviceIoControl",
            lambda *a: (_ for _ in ()).throw(SimulatedCrash("inside the junction")),
        )
        with pytest.raises(SimulatedCrash):
            env.move()
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_b8_sweep_a_kill_anywhere_in_a_forward_finishing_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = 1
    failures: list[str] = []
    while True:
        base = tmp_path / f"f{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        _killed_after_junction(e)
        _write(e.source / "written-after-restart.log", b"WRITTEN-THROUGH-JUNCTION")
        with monkeypatch.context() as m:
            _arm6(m, at, relocate._key(_restore(e)))
            try:
                recover(e.journal_path(), policy=e.policy)
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
        if not crashed:
            break
        try:
            out = recover(e.journal_path(), policy=e.policy)
            survivors = []
            for root in (e.source, e.target, _moved(e), _restore(e)):
                if _exists(root) and not _is_link(root):
                    survivors += _all_bytes(root)
            problems = []
            if b"WRITTEN-THROUGH-JUNCTION" not in survivors:
                problems.append("lost the write")
            if out.journal.stage == Stage.DONE:
                if not _is_link(e.source):
                    problems.append("DONE without a junction")
            elif out.journal.stage == Stage.ROLLED_BACK:
                if _is_link(e.source) or not _exists(e.source / "written-after-restart.log"):
                    problems.append("ROLLED_BACK without the write at the path")
            else:
                problems.append(f"ended {out.journal.stage}")
            if problems:
                failures.append(f"kill at {at} ({where}): {'; '.join(problems)}")
        except (RelocateError, OSError) as exc:
            failures.append(f"kill at {at} ({where}): {type(exc).__name__}: {str(exc)[:120]}")
        at += 1
    print(f"B8-SWEEP points={at - 1} failures={len(failures)}")
    assert at > 1
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


# ------------------------------------------------------------------ chained sweep (L1/L2)


@pytest.mark.parametrize("first", ["rollback", "use_old_copy"])
def test_b8_sweep_rollback_after_an_interrupted_delete_killed_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """DONE, an edit through the junction, delete-old interrupted half-way, then a rollback
    (plain, or use_old_copy which must refuse) killed at every point, then recover twice, then
    delete-old. The edit must survive, the path must never be left empty, and a final
    ROLLED_BACK must hold the edit at the path."""
    at = 1
    failures: list[str] = []
    edit = b"TARGET-SIDE edit through the junction"
    while True:
        base = tmp_path / f"c{at}"
        base.mkdir()
        e = Env(base)
        _vol(monkeypatch, e)
        _make_tree(e.source)
        e.move()
        _write(e.source / "sub" / "b.txt", edit)
        _interrupted_delete(e, monkeypatch)
        refused = False
        with monkeypatch.context() as m:
            _arm6(m, at, relocate._key(_restore(e)))
            try:
                rollback(e.journal_path(), use_old_copy=first == "use_old_copy")
                crashed = False
            except SimulatedCrash as crash:
                where = str(crash)
                crashed = True
            except RelocateError as exc:
                where = f"refused: {str(exc)[:60]}"
                crashed = refused = True
        if not crashed:
            break
        problems = []
        if not _exists(e.source):
            print(f"B8-TRANSIENT-EMPTY at={at} {where}")
        out = None
        for _ in range(2):
            try:
                out = recover(e.journal_path(), policy=e.policy)
            except RelocateError:
                out = None
        if out is None or out.journal.stage != Stage.ROLLED_BACK:
            # recover honours a use_old_copy choice that must refuse: a plain rollback then.
            try:
                out = rollback(e.journal_path())
            except RelocateError as exc:
                problems.append(f"plain rollback refused: {str(exc)[:100]}")
        if out is not None and out.journal.stage == Stage.ROLLED_BACK:
            if _is_link(e.source) or (e.source / "sub" / "b.txt").read_bytes() != edit:
                problems.append("ROLLED_BACK without the edit at the path")
            live = _manifest(e.source)
            try:
                delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
            except RelocateError:
                pass
            if _manifest(e.source) != live:
                problems.append("delete-old changed the restored folder")
        survivors = []
        for root in (e.source, e.target, _moved(e), _restore(e)):
            if _exists(root) and not _is_link(root):
                survivors += _all_bytes(root)
        if edit not in survivors:
            problems.append("lost the edit")
        if problems:
            failures.append(f"kill at {at} ({where}): {'; '.join(problems)}")
        at += 1
        if refused:
            break
    print(f"B8-SWEEP points={at - 1} failures={len(failures)}")
    assert at > 2
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)
