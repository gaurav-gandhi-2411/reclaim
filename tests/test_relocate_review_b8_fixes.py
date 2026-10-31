from __future__ import annotations

# ruff: noqa: F811, PTH104, PTH112
# Tests of the fixes for the eighth hostile review (B8; ADR-0044 addendum 8) that the reviewer's
# probes (`test_relocate_review_b8.py`) do not cover: the second layer of M-A, the production
# wiring of M-B, the retry after M-C/M-D, the BELIEVED items and G1. Every root is under
# `tmp_path` (the `env` fixture); nothing here touches a real profile.
import dataclasses
import json
import os
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
from test_relocate_review_b4 import _arm_deletes  # type: ignore[import-not-found]

import reclaim.relocate as relocate
from reclaim.config import Config, SafetyConfig
from reclaim.relocate import (
    Journal,
    RelocateError,
    Stage,
    delete_old_copy,
    recover,
    rollback,
)


def _paths(e: Env) -> list[Path]:
    return sorted(e.journals.glob("relocate-*.json"))


def _moved(e: Env) -> Path:
    return Path(str(e.source) + relocate.MOVED_SUFFIX)


def _restore(e: Env) -> Path:
    return Path(str(e.source) + relocate.RESTORE_SUFFIX)


def _stage(e: Env) -> str:
    return Journal.load(e.journal_path()).stage


# ------------------------------------------------------------------ M-A


def test_an_undone_move_is_inert_for_rollback_recover_and_delete_old(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    expected = _manifest(env.source)
    raw = env.journal_path().read_bytes()
    for out in (
        rollback(env.journal_path()),
        rollback(env.journal_path(), use_old_copy=True),
        recover(env.journal_path(), policy=env.policy),
    ):
        assert out.journal.stage == Stage.ROLLED_BACK
    assert env.journal_path().read_bytes() == raw  # not even re-saved
    assert _manifest(env.source) == expected and _exists(_moved(env))
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _stage(env) == Stage.DELETED_OLD
    raw = env.journal_path().read_bytes()
    out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert "nothing to do" in out.message
    assert env.journal_path().read_bytes() == raw
    assert rollback(env.journal_path()).message.startswith("already rolled back")
    assert _manifest(env.source) == expected


def test_a_finished_move_with_its_old_copy_deleted_is_not_inert(env: Env) -> None:
    """DELETED_OLD without `rolled_back` is a live junction: its rollback must still work."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert not Journal.load(env.journal_path()).rolled_back
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source) and _manifest(env.source) == expected


def _clone_as_later_move(e: Env, stage: str | None = None) -> Journal:
    first = Journal.load(e.journal_path())
    later = dataclasses.replace(
        first,
        id="feedfacefeed",
        created_at=first.created_at + 10,
        path=str(e.journals / "relocate-feedfacefeed.json"),
    )
    if stage is not None:
        later.stage = stage
    later.save()
    return later


def test_rollback_refuses_while_a_later_live_move_of_the_folder_exists(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    first = e_path = env.journal_path()
    _clone_as_later_move(env)
    before = e_path.read_bytes()
    with pytest.raises(RelocateError, match="feedfacefeed"):
        rollback(first)
    assert first.read_bytes() == before and _is_link(env.source)
    # Same source spelled in another case is the same folder on NTFS.
    data = json.loads((env.journals / "relocate-feedfacefeed.json").read_text("utf-8"))
    data["source"] = data["source"].upper()
    (env.journals / "relocate-feedfacefeed.json").write_text(json.dumps(data), "utf-8")
    with pytest.raises(RelocateError, match="feedfacefeed"):
        rollback(first)
    # Once the later move is undone itself, the older journal is no longer in its way... and
    # is inert anyway only if it was undone; here it is DONE, so the rollback proceeds.
    data["stage"] = Stage.ROLLED_BACK.value
    (env.journals / "relocate-feedfacefeed.json").write_text(json.dumps(data), "utf-8")
    assert rollback(first).journal.stage == Stage.ROLLED_BACK


def test_delete_old_refuses_while_a_later_live_move_exists(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    first = env.journal_path()
    _clone_as_later_move(env)
    with pytest.raises(RelocateError, match="feedfacefeed"):
        delete_old_copy(first, policy=env.policy, confirmed=True)
    assert _exists(_moved(env))


def test_an_unreadable_journal_folder_refuses_the_rollback(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    first = env.journal_path()
    (env.journals / "relocate-garbage.json").write_text("not json", "utf-8")
    with pytest.raises(RelocateError, match="could not be read"):
        rollback(first)
    assert _is_link(env.source)


# ------------------------------------------------------------------ M-B


def test_the_production_policy_checks_the_original_path_text_of_a_junction(
    env: Env, tmp_path: Path
) -> None:
    _make_tree(env.source)
    env.move()
    # A path pattern naming the original folder: the junction's text hits it. The default check
    # resolves the junction to the target, but since #190 a directory check also walks the
    # exact path when it differs from the resolved one, so it sees the original text too.
    config = Config(safety=SafetyConfig(deny=["re:" + env.source.as_posix().replace(".", r"\.")]))
    home = tmp_path / "home"
    home.mkdir()
    policy = relocate.default_policy(config, environ={"USERPROFILE": str(home)}, app_dirs=[])
    assert policy.exclusion_match_text is not None
    assert policy.exclusion_match_text(env.source) is not None
    assert policy.exclusion_match(env.source) is not None
    with pytest.raises(RelocateError, match="excluded"):
        delete_old_copy(env.journal_path(), policy=policy, confirmed=True)
    assert _exists(_moved(env)) and os.path.isdir(_x(env.target))


# ------------------------------------------------------------------ M-C, M-D


class _Held:
    """A rename that a scanner handle refuses until `release()` (the cause going away)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, frm: str, to: str) -> None:
        self.real = relocate._rename
        self.frm, self.to = relocate._key(frm), relocate._key(to)
        self.held = True
        monkeypatch.setattr(relocate, "_rename", self._rename)

    def _rename(self, a: str, b: str) -> None:
        if self.held and relocate._key(a) == self.frm and relocate._key(b) == self.to:
            raise PermissionError(5, "Access is denied (simulated scanner handle)", a)
        self.real(a, b)

    def release(self) -> None:
        self.held = False


def test_recover_with_the_cause_still_there_is_a_relocate_error_and_a_rerun_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    held = _Held(monkeypatch, str(_moved(env)), str(env.source))

    def boom(*_a: object, **_k: object) -> str:
        raise RelocateError("simulated: 1 files differ after copying them twice")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_delta_sync", boom)
        with pytest.raises(RelocateError):
            env.move()
    journal = Journal.load(env.journal_path())
    with pytest.raises(RelocateError) as info:
        recover(env.journal_path(), policy=env.policy)
    text = str(info.value)
    assert journal.id in text and "Access is denied" in text and "again" in text
    assert not isinstance(info.value.__cause__, RelocateError)  # the wrapped OSError
    assert _exists(_moved(env))  # nothing was lost or half-done
    held.release()
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected and not _exists(_moved(env))
    assert not _exists(env.target)


def test_delete_old_with_an_os_error_is_a_relocate_error_and_a_rerun_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    real = relocate._sha256
    state = {"fail": True}

    def sha(path: str) -> str:
        if state["fail"] and relocate._key(path).startswith(relocate._key(_moved(env))):
            raise PermissionError(32, "The process cannot access the file", path)
        return real(path)

    monkeypatch.setattr(relocate, "_sha256", sha)
    with pytest.raises(RelocateError) as info:
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert "delete-old" in str(info.value) and "again" in str(info.value)
    assert _exists(_moved(env)) and _stage(env) == Stage.DONE
    state["fail"] = False
    out = delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD and not _exists(_moved(env))


def test_a_failed_cleanup_of_our_restore_changes_nothing_and_a_rerun_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cleanup runs before the old copy goes back, so a failure leaves the junction up, the
    old copy where it was and the journal at ROLLING_BACK; the rerun finishes the rollback."""
    _make_tree(env.source)
    before_move = _manifest(env.source)
    env.move()
    real = relocate._copy_file
    n = {"i": 0}

    def copy(src: str, dst: str) -> None:
        if relocate._key(dst).startswith(relocate._key(_restore(env))):
            n["i"] += 1
            if n["i"] == 3:
                raise OSError(5, "in the copy-back")
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_copy_file", copy)
        with pytest.raises(RelocateError):
            rollback(env.journal_path())
    assert _exists(_restore(env)) and _is_link(env.source)
    moved_before = _manifest(_moved(env))

    def refuse(_path: str) -> None:
        raise PermissionError(5, "Access is denied (simulated scanner handle)")

    with monkeypatch.context() as m:
        m.setattr(relocate, "rmtree_reparse_point_safe", refuse)
        with pytest.raises(RelocateError, match="again"):
            rollback(env.journal_path(), use_old_copy=True)
    assert _is_link(env.source) and _exists(_restore(env))  # nothing else changed
    assert _manifest(_moved(env)) == moved_before
    assert _stage(env) == Stage.ROLLING_BACK
    out = rollback(env.journal_path(), use_old_copy=True)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source) and _manifest(env.source) == before_move
    assert not _exists(_restore(env))
    assert _exists(env.target)  # use_old_copy leaves the moved folder as it is


# ------------------------------------------------------------------ BELIEVED 1


def test_a_rollback_refused_for_space_leaves_a_done_move_done(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    path = env.journal_path()
    before = Journal.load(path)
    with pytest.raises(RelocateError, match="needs"):
        rollback(path, free_bytes=lambda _p: 0)
    after = Journal.load(path)
    assert after.stage == Stage.DONE and not after.use_old_copy
    assert after.history == before.history and _is_link(env.source)
    out = delete_old_copy(path, policy=env.policy, confirmed=True)  # still works
    assert out.journal.stage == Stage.DELETED_OLD


def test_a_rollback_refused_because_the_moved_folder_is_in_use_leaves_a_done_move_done(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    path = env.journal_path()
    before = Journal.load(path)
    _Held(monkeypatch, str(env.target), str(env.target) + relocate.PROBE_SUFFIX)
    with pytest.raises(relocate.InUseError):
        rollback(path)
    after = Journal.load(path)
    assert after.stage == Stage.DONE and after.history == before.history
    assert _is_link(env.source) and _exists(env.target)
    out = delete_old_copy(path, policy=env.policy, confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD


def test_a_refused_use_old_copy_rollback_keeps_the_previous_choice(env: Env) -> None:
    """The refusal restores the whole previous state, `use_old_copy` included, so a `recover`
    never honours a choice whose rollback never started."""
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    path = env.journal_path()
    with pytest.raises(RelocateError, match="nothing"):
        rollback(path, use_old_copy=True)  # the old copy is gone: refused before any change
    after = Journal.load(path)
    assert after.stage == Stage.DELETED_OLD and not after.use_old_copy


# ------------------------------------------------------------------ L-1


def _interrupted_delete(e: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        _arm_deletes(m, 3, relocate._key(_moved(e)))
        with pytest.raises(SimulatedCrash):
            delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
    assert _stage(e) == Stage.DELETING_OLD


def test_recover_finishes_an_interrupted_delete_once_the_target_is_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    _interrupted_delete(env, monkeypatch)
    left = _manifest(_moved(env))
    os.rename(_x(env.target), _x(env.d / "unplugged"))
    with pytest.raises(RelocateError, match="Reconnect") as info:
        recover(env.journal_path(), policy=env.policy)
    assert Journal.load(env.journal_path()).id in str(info.value)
    assert _manifest(_moved(env)) == left and _stage(env) == Stage.DELETING_OLD
    os.rename(_x(env.d / "unplugged"), _x(env.target))
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.DELETED_OLD and not _exists(_moved(env))


def test_recover_does_not_finish_a_delete_when_another_folder_sits_at_the_target(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    _interrupted_delete(env, monkeypatch)
    left = _manifest(_moved(env))
    os.rename(_x(env.target), _x(env.d / "ours-set-aside"))
    _write(env.target / "foreign.txt", b"another folder, same path")  # reachable, wrong ID
    with pytest.raises(RelocateError, match="not reachable"):
        recover(env.journal_path(), policy=env.policy)
    assert _manifest(_moved(env)) == left


def test_recover_finishes_an_interrupted_delete_after_a_rollback(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a rollback the live folder is the restored one at the original path."""
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    _interrupted_delete(env, monkeypatch)
    live = _manifest(env.source)
    out = recover(env.journal_path(), policy=env.policy)
    assert out.journal.stage == Stage.DELETED_OLD and not _exists(_moved(env))
    assert _manifest(env.source) == live and not _is_link(env.source)


# ------------------------------------------------------------------ G1


def _guided_policy(root: Path) -> relocate.Policy:
    return relocate.Policy(
        exclusion_match=lambda _p: None,
        protected=lambda _p: False,
        guided=((root, "move it with its own program"),),
    )


def test_a_guided_folder_is_sized_from_its_files_without_opening_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Docker"
    _write(root / "disk.vhdx", b"d" * 5000)
    _write(root / "sub" / "a.bin", b"a" * 123)
    _write(root / "sub" / "deep" / "b.bin", b"b" * 7)
    # A junction to a big folder elsewhere: never followed, never counted.
    other = tmp_path / "elsewhere"
    _write(other / "big.bin", b"x" * 100_000)
    relocate._create_junction(str(root / "link"), str(other))

    def no_open(*_a: object, **_k: object) -> None:
        raise AssertionError("sizing a guided folder must not open a file")

    with monkeypatch.context() as m:
        m.setattr("builtins.open", no_open)
        found = relocate.classify(root, _guided_policy(root))
    assert found.verdict == relocate.Verdict.GUIDED
    assert found.bytes == 5000 + 123 + 7 and found.files == 3
    # A path inside the guided root is guided and sized too.
    inside = relocate.classify(root / "sub", _guided_policy(root))
    assert inside.verdict == relocate.Verdict.GUIDED and inside.bytes == 123 + 7


def test_a_missing_or_linked_guided_path_has_no_size_and_does_not_raise(tmp_path: Path) -> None:
    root = tmp_path / "wsl"
    assert relocate.classify(root, _guided_policy(root)).bytes == 0
    other = tmp_path / "elsewhere"
    _write(other / "big.bin", b"x" * 1000)
    relocate._create_junction(str(root), str(other))
    assert relocate.classify(root, _guided_policy(root)).bytes == 0


def test_candidates_are_ordered_movable_then_guided_by_size(tmp_path: Path) -> None:
    movable = tmp_path / "movable"
    _write(movable / "m.bin", b"m" * 10)
    big, small = tmp_path / "Docker", tmp_path / "wsl"
    _write(big / "d.vhdx", b"d" * 9000)
    _write(small / "w.vhdx", b"w" * 90)
    policy = relocate.Policy(
        exclusion_match=lambda _p: None,
        protected=lambda _p: False,
        guided=((big, "docker steps"), (small, "wsl steps")),
    )
    found = relocate.propose_candidates([small, movable, big], policy, None)
    assert [c.path for c in found] == [str(movable), str(big), str(small)]
    assert [c.classification.bytes for c in found] == [10, 9000, 90]
