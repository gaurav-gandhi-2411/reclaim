from __future__ import annotations

# ruff: noqa: F811, T201, PTH104, PTH106, PTH110, PTH118, PTH208, TRY300
# Hostile-review probes (verifier B11, export of 6139589) for ADR-0044 addendum 10's fixes:
# `_as_spelled_on_disk` on both paths, the network-path refusal, `_put_junction_back` and the
# rerun branches. "_holds" tests assert a safety claim expected to hold; the others assert the
# claim the reviewer expected to break. Every root is under tmp_path; zzexclproj stands in for
# an excluded project. Kept in the repo: 12 failed on 6139589 and pass with addendum 11's fixes;
# the sweep's report file (written next to this file when a folder existed) was removed, since
# a test writes only under tmp_path.
import ctypes
import os
import string
from collections.abc import Callable
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
from test_relocate_review_b4 import _vol  # type: ignore[import-not-found]
from test_relocate_review_b9 import _inject  # type: ignore[import-not-found]

import reclaim.relocate as relocate
from reclaim.relocate import (
    Journal,
    RelocateError,
    Stage,
    delete_old_copy,
    execute_move,
    recover,
    rollback,
)


def _short_leaf(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    if not n:
        return ""
    leaf = Path(buf.value).name
    return "" if leaf.casefold() == path.name.casefold() else leaf


def _plan(src: object, tgt: object, e: Env) -> relocate.Plan:
    return relocate.plan_move(
        src,  # type: ignore[arg-type]
        tgt,  # type: ignore[arg-type]
        e.policy,
        free_bytes=lambda _p: BIG,
        journal_dir=e.journals,
    )


def _move(src: object, tgt: object, e: Env) -> relocate.Outcome:
    return execute_move(src, tgt, e.policy, e.journals, free_bytes=lambda _p: BIG)  # type: ignore[arg-type]


def _names(folder: Path) -> list[str]:
    return sorted(os.listdir(_x(folder)))


def _all_entries(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(_x(root)):
        for n in dirnames + filenames:
            out.append(os.path.join(dirpath, n))
    return sorted(out)


# ------------------------------------------------------------------ 1. targets


def test_b11_a_target_with_dotdot_is_refused_by_the_plan(env: Env) -> None:
    """`_norm` keeps `..` (pathlib's absolute() does not collapse it) and every call adds `\\\\?\\`,
    which passes `..` to NTFS literally (ERROR_INVALID_NAME). A dry run that says "ok" for a
    target no step can create is a false OK; the move itself must still fail cleanly."""
    _make_tree(env.source)
    before = _manifest(env.source)
    (env.d / "x").mkdir()
    given = f"{env.d}\\x\\..\\relocated\\data"
    plan = _plan(env.source, given, env)
    print("B11-DOTDOT plan", plan.ok, plan.target, plan.reasons)
    raised = None
    if plan.ok:
        try:
            _move(env.source, given, env)
        except RelocateError as exc:
            raised = exc
        print("B11-DOTDOT move:", str(raised)[:300])
        print("B11-DOTDOT D after:", _all_entries(env.d))
        assert raised is not None
    # Never a lost byte or a changed source, whatever the plan said.
    assert not _is_link(env.source) and _manifest(env.source) == before
    assert _names(env.c) == ["data"]
    assert plan.ok is False, f"the dry run accepted {given}"


_BAD_TAILS = {
    "dotdot": "x\\..\\relocated\\data",
    "colon_stream": "relocated\\data:alt",
    "question": "relocated\\da?ta",
    "pipe": "relocated\\da|ta",
    "long_component": "relocated\\" + "n" * 300,
}


@pytest.mark.parametrize("kind", sorted(_BAD_TAILS))
def test_b11_a_failed_dotdot_move_leaves_a_journal_that_can_be_finished(
    env: Env, kind: str
) -> None:
    """The `..` target's move fails at the target's creation and its own rollback fails too
    ("could not be rolled back"). Required: the journal it leaves can be finished by the command
    its message names (recover), and the folder can then be moved normally. Siblings: other
    target texts NTFS rejects with an error other than "not found"."""
    _make_tree(env.source)
    before = _manifest(env.source)
    (env.d / "x").mkdir()
    given = f"{env.d}\\{_BAD_TAILS[kind]}"
    pre = _plan(env.source, given, env)
    print("B11-BADTGT", kind, "plan ok:", pre.ok, pre.reasons[:2])
    if not pre.ok:
        return  # refused up front: nothing to get stuck
    with pytest.raises(RelocateError) as info:
        _move(env.source, given, env)
    print("B11-DOTDOT2 move:", str(info.value))
    jp = env.journal_path()
    j = Journal.load(jp)
    print("B11-DOTDOT2 journal", j.stage, j.target, j.created_parents, j.target_planned)
    outcomes = []
    for name, cmd in (
        ("recover", lambda: recover(jp, policy=env.policy)),
        ("rollback", lambda: rollback(jp)),
    ):
        try:
            outcomes.append(f"{name}: {cmd().message[:200]}")
        except RelocateError as exc:
            outcomes.append(f"{name}: refused {str(exc)[:300]}")
    print("B11-DOTDOT2 then", outcomes, Journal.load(jp).stage)
    assert not _is_link(env.source) and _manifest(env.source) == before
    plan = _plan(env.source, env.target, env)
    print("B11-DOTDOT2 new plan", plan.ok, plan.reasons)
    assert Journal.load(jp).stage == Stage.ROLLED_BACK, "the failed move's journal is stuck"
    assert plan.ok, "the folder can no longer be moved"


def test_b11_holds_a_dotdot_target_cannot_nest_inside_a_live_target(env: Env) -> None:
    """Text-wise `D\\holder\\zz\\..\\a\\b` is not inside A's target `D\\holder\\a`; on disk it
    would be. Required: B never lands inside A's target, and A is untouched."""
    holder = env.d / "holder"
    a_src, a_tgt, b_src = env.c / "adata", holder / "a", env.c / "bdata"
    _make_tree(a_src)
    _write(b_src / "b-only.txt", b"only B has this")
    _move(a_src, a_tgt, env)
    a_live = _manifest(a_tgt)
    given = f"{holder}\\zz\\..\\a\\b"
    plan = _plan(b_src, given, env)
    print("B11-DOTDOT-NEST plan", plan.ok, plan.reasons)
    try:
        _move(b_src, given, env)
        moved = True
    except RelocateError as exc:
        print("B11-DOTDOT-NEST move refused:", str(exc)[:300])
        moved = False
    assert not moved
    assert not _exists(a_tgt / "b") and _manifest(a_tgt) == a_live
    assert (b_src / "b-only.txt").read_bytes() == b"only B has this" and not _is_link(b_src)


def test_b11_a_target_under_a_file_is_refused_by_the_plan(env: Env) -> None:
    """The nearest existing ancestor of the target is a FILE: `_as_spelled_on_disk` stops there
    happily; nothing in the plan checks it is a folder."""
    _make_tree(env.source)
    before = _manifest(env.source)
    _write(env.d / "afile.txt", b"a file, not a folder")
    given = env.d / "afile.txt" / "sub" / "data"
    plan = _plan(env.source, given, env)
    print("B11-FILEPARENT plan", plan.ok, plan.reasons)
    if plan.ok:
        try:
            _move(env.source, given, env)
            raise AssertionError("a move under a file succeeded")
        except RelocateError as exc:
            print("B11-FILEPARENT move:", str(exc)[:300])
    assert not _is_link(env.source) and _manifest(env.source) == before
    assert _names(env.d) == ["afile.txt"]
    assert plan.ok is False, "the dry run accepted a target whose parent is a file"


def test_b11_holds_a_target_on_a_drive_that_does_not_exist_plans_without_error(env: Env) -> None:
    used = {d[0].upper() for d in os.listdrives()} if hasattr(os, "listdrives") else set()
    free = [
        c for c in string.ascii_uppercase[3:] if c not in used and not os.path.exists(f"{c}:\\")
    ]
    if not free:
        pytest.skip("no unused drive letter")
    _make_tree(env.source)
    given = f"{free[-1]}:\\nowhere\\deeper\\data"
    spelled = relocate._as_spelled_on_disk(relocate._norm(given))
    plan = _plan(env.source, given, env)
    print("B11-NODRIVE", spelled, plan.ok, plan.reasons)
    assert spelled == (relocate._norm(given), None)
    assert not plan.ok and _names(env.c) == ["data"]


def test_b11_holds_a_drive_root_ancestor_is_spelled_as_the_drive(tmp_path: Path) -> None:
    drive = str(tmp_path)[:2]
    given = f"{drive.lower()}\\zz-b11-not-there\\x"
    spelled, reason = relocate._as_spelled_on_disk(relocate._norm(given))
    print("B11-ROOT", spelled, reason)
    assert reason is None and relocate._key(spelled) == relocate._key(given)
    assert not os.path.exists(f"{drive}\\zz-b11-not-there")


@pytest.mark.parametrize("kind", ["live", "dangling"])
def test_b11_holds_a_junction_ancestor_of_the_target_is_refused(env: Env, kind: str) -> None:
    real = env.d / "realtargets"
    real.mkdir()
    relocate._create_junction(str(env.d / "via"), str(real))
    if kind == "dangling":
        os.rmdir(_x(real))
    _make_tree(env.source)
    given = env.d / "via" / "sub" / "data"
    spelled = relocate._as_spelled_on_disk(relocate._norm(given))
    plan = _plan(env.source, given, env)
    print("B11-JTGT", kind, spelled, plan.ok, plan.reasons)
    assert not plan.ok and any("reached through a link" in r for r in plan.reasons)
    assert _names(env.c) == ["data"]


def test_b11_holds_an_8dot3_junction_ancestor_of_the_target_is_refused(env: Env) -> None:
    real = env.d / "realtargets"
    real.mkdir()
    link = env.d / "longjunctionname"
    relocate._create_junction(str(link), str(real))
    alias = _short_leaf(link)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    _make_tree(env.source)
    plan = _plan(env.source, env.d / alias / "data", env)
    print("B11-J83", alias, plan.ok, plan.reasons)
    assert not plan.ok


@pytest.mark.parametrize("how", ["case", "short"])
def test_b11_holds_a_target_whose_nearest_ancestor_is_the_source_is_refused(
    env: Env, how: str
) -> None:
    src = env.c / "longsourcefoldername"
    _make_tree(src)
    if how == "case":
        given = env.c / "LONGSOURCEFOLDERNAME" / "new" / "deeper"
    else:
        alias = _short_leaf(src)
        if not alias:
            pytest.skip("8.3 names are disabled on this volume")
        given = env.c / alias / "new"
    plan = _plan(src, given, env)
    print("B11-INSRC", how, plan.target, plan.reasons)
    assert not plan.ok and _names(src) == _names(src)
    assert not _exists(src / "new")


def test_b11_holds_a_long_tail_under_a_case_variant_parent_takes_the_disk_case(env: Env) -> None:
    (env.d / "Targets").mkdir()
    _make_tree(env.source)
    before = _manifest(env.source)
    tail = "\\".join(["segment-" + "x" * 40] * 7)  # > 300 characters, none of it exists
    given = f"{env.d}\\TARGETS\\{tail}\\data"
    plan = _plan(env.source, given, env)
    print("B11-LONGTAIL", plan.ok, len(plan.target), plan.reasons)
    assert plan.ok, plan.reasons
    assert plan.target.startswith(str(env.d / "Targets") + "\\")
    _move(env.source, given, env)
    assert _names(env.d) == ["Targets"]
    assert _is_link(env.source) and _manifest(env.source) == before
    rollback(env.journal_path())
    assert not _is_link(env.source) and _manifest(env.source) == before


# ------------------------------------------------------------------ 2. the source spelling


def test_b11_a_probe_leftover_is_put_back_under_its_own_name(env: Env) -> None:
    """A kill inside the probe left `data.reclaim-probe`, nothing at `data`. The user types
    `DATA`. `_as_spelled_on_disk` cannot see the leaf (it does not exist), so the probe restore
    renames the leftover to the TYPED case: the folder's name on disk changes, which B10 L2
    says no longer happens."""
    _make_tree(env.source)
    before = _manifest(env.source)
    os.rename(_x(env.source), _x(Path(str(env.source) + relocate.PROBE_SUFFIX)))
    plan = _plan(str(env.source).upper(), env.target, env)
    print("B11-PROBECASE", plan.ok, plan.source, _names(env.c))
    assert _manifest(env.c / "data") == before  # no byte lost either way
    assert _names(env.c) == ["data"], f"the folder is now spelled {_names(env.c)}"


def test_b11_holds_a_legacy_8dot3_journal_still_rolls_back_and_deletes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal written before the spelling check (simulated: the check off at move time)
    with an 8.3 parent in its source. The new code never re-spells a journal: rollback and
    delete-old must still act on the folder it names, and a new plan by the long name must
    not be accepted while that move is live."""
    parent = env.c / "longparentfolder"
    src = parent / "data"
    _make_tree(src)
    alias = _short_leaf(parent)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    before = _manifest(src)
    legacy = env.c / alias / "data"
    with monkeypatch.context() as m:
        m.setattr(relocate, "_as_spelled_on_disk", lambda p: (p, None))
        _move(legacy, env.target, env)
    j = Journal.load(env.journal_path())
    print("B11-LEGACY journal source", j.source, _names(parent))
    assert alias in j.source and _is_link(src)
    again = _plan(src, env.d / "other", env)
    print("B11-LEGACY new plan", again.ok, again.reasons)
    assert not again.ok
    out = rollback(env.journal_path())
    print("B11-LEGACY rollback", out.message[:200], _names(parent))
    assert not _is_link(src) and _manifest(src) == before
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _names(parent) == ["data"] and _manifest(src) == before


def test_b11_holds_a_legacy_8dot3_journal_killed_mid_copy_cannot_undo_a_new_move(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy journal (8.3 parent) killed during the copy, never recovered. A new move of the
    same folder by its long name: the overlap and supersession checks compare text (casefold
    only), so they cannot tie the two together. Recovering the legacy journal afterwards must
    not undo or damage the new, live move."""
    parent = env.c / "longparentfolder"
    src = parent / "data"
    _make_tree(src)
    alias = _short_leaf(parent)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    before = _manifest(src)
    legacy = env.c / alias / "data"

    def die(name: str) -> None:
        if name == "after_copy":
            raise SimulatedCrash("killed after the copy")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_as_spelled_on_disk", lambda p: (p, None))
        with pytest.raises(SimulatedCrash):
            execute_move(
                legacy,
                env.d / "legacy-t",
                env.policy,
                env.journals,
                free_bytes=lambda _p: BIG,
                checkpoint=die,
            )
    (old,) = sorted(env.journals.glob("relocate-*.json"))
    plan = _plan(src, env.target, env)
    print("B11-LEGACY2 new plan", plan.ok, plan.reasons)
    if not plan.ok:
        return  # refused: nothing can be undone
    _move(src, env.target, env)
    (new,) = [p for p in env.journals.glob("relocate-*.json") if p != old]
    live = _manifest(env.target)
    try:
        out = recover(old, policy=env.policy)
        print("B11-LEGACY2 recover old:", out.message[:300])
    except RelocateError as exc:
        print("B11-LEGACY2 recover old refused:", str(exc)[:300])
    assert _is_link(src) and Journal.load(new).stage == Stage.DONE
    assert _manifest(env.target) == live
    assert _manifest(Path(str(src) + relocate.MOVED_SUFFIX)) == before
    out = rollback(new)
    assert out.journal.stage == Stage.ROLLED_BACK and _manifest(src) == before


# ------------------------------------------------------------------ 3. network and device paths


def _volume_guid(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(64)
    root = str(path)[:3]
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 64):
        return ""
    return buf.value  # \\?\Volume{...}\


@pytest.mark.parametrize("form", ["fwd_unc", "prefixed_unc", "device"])
def test_b11_holds_other_spellings_of_a_share_or_device_are_refused(env: Env, form: str) -> None:
    _make_tree(env.source)
    drive, rest = str(env.source)[0], str(env.source)[2:]
    given = {
        "fwd_unc": f"//localhost/{drive}$" + rest.replace("\\", "/"),
        "prefixed_unc": f"\\\\?\\UNC\\localhost\\{drive}$" + rest,
        "device": f"\\\\.\\{drive}:" + rest,
    }[form]
    plan = _plan(given, env.target, env)
    tplan = _plan(env.c / "nothing", given.replace("data", "t-new"), env)
    print("B11-NET", form, plan.source, plan.reasons[:3], "| target:", tplan.reasons[:3])
    assert not plan.ok and any("network path" in r or "not" in r for r in plan.reasons)
    assert not tplan.ok
    assert _names(env.c) == ["data"]


def test_b11_a_volume_guid_target_means_that_volume(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`\\\\?\\Volume{GUID}\\...` is a local path by volume name. `_norm` strips `\\\\?\\` and
    makes `Volume{GUID}\\...` absolute against the CURRENT DIRECTORY: the plan silently names a
    folder under wherever the command was run."""
    guid = _volume_guid(env.d)
    if not guid:
        pytest.skip("no volume name for the test drive")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    intended = env.d / "voltarget"
    given = guid + str(intended)[3:]
    _make_tree(env.source)
    plan = _plan(env.source, given, env)
    print("B11-VOLGUID", given, "->", plan.target, plan.ok, plan.reasons)
    # The fixture reads every non-D path as the source's volume, so the cwd folder is refused
    # here only as "same volume"; on a machine whose cwd is on another fixed drive it is not.
    assert relocate._key(plan.target) == relocate._key(intended) or any(
        "Volume{" in r for r in plan.reasons
    ), f"{given} was taken as {plan.target}"


def test_b11_case_of_a_long_named_source_is_kept_on_disk(env: Env) -> None:
    """B10 L2 says the path is taken with the on-disk case. GetLongPathNameW only rewrites the
    components it looks up (8.3-shaped ones); a long name keeps the typed case."""
    src = env.c / "mylongfoldername-data"
    _make_tree(src)
    before = _manifest(src)
    typed = str(src).upper()
    plan = _plan(typed, env.target, env)
    print("B11-LONGCASE plan.source", plan.source)
    _move(typed, env.target, env)
    after_move = _names(env.c)
    rollback(env.journal_path())
    print("B11-LONGCASE after move", after_move, "after rollback", _names(env.c))
    assert _manifest(env.c / "mylongfoldername-data") == before
    assert "mylongfoldername-data" in _names(env.c), _names(env.c)


# ------------------------------------------------------------------ 4. _put_junction_back


def test_b11_double_fault_in_use_old_copy_names_the_command_the_user_ran(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """use_old_copy: the junction is removed, the rename of the old copy fails, the junction's
    re-creation fails. The message says run `reclaim relocate rollback <id>` again, without
    `--use-old-copy`: run as written, the rerun copies the moved folder back instead of putting
    back the copy from before the move that the user chose."""
    _make_tree(env.source)
    before = _manifest(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    real_rename = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(b) == relocate._key(env.source):
            raise PermissionError(13, "simulated: old copy held", a)
        real_rename(a, b)

    def create(link: str, target: str) -> None:
        raise PermissionError(13, "simulated: junction creation refused", link)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_rename", rename)
        m.setattr(relocate, "_create_junction", create)
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path(), use_old_copy=True)
    text = str(info.value)
    print("B11-UOC-DOUBLE", text[:400], "| stage", Journal.load(env.journal_path()).stage)
    assert "is empty right now" in text and not _exists(env.source)
    # The message's own command, as written:
    out = rollback(env.journal_path(), use_old_copy="--use-old-copy" in text)
    print("B11-UOC-DOUBLE rerun:", out.message[:300])
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == before, "the rerun did not put back the copy the user chose"


def test_b11_holds_put_back_refusal_with_a_failed_junction_recreate_then_rerun_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old copy deleted; a copy-back is killed right after the junction's removal (stage at
    ROLLING_BACK, path empty). Then use_old_copy: no old copy, so it puts the junction back
    before refusing; that fails too: RelocateError, not NothingChangedError, so the stage is
    not restored. The rerun without use_old_copy (what the message says) must finish."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    expected = _manifest(env.target)
    real_remove = relocate._remove_junction

    def remove_then_die(link: str) -> None:
        real_remove(link)
        raise SimulatedCrash("killed right after the junction's removal")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", remove_then_die)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert not _exists(env.source)
    stage_before = Journal.load(env.journal_path()).stage

    def create(link: str, target: str) -> None:
        raise PermissionError(13, "simulated: junction creation refused", link)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_create_junction", create)
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path(), use_old_copy=True)
    j = Journal.load(env.journal_path())
    print("B11-PUTBACK-REFUSE", type(info.value).__name__, str(info.value)[:300])
    print("B11-PUTBACK-REFUSE stage", stage_before, "->", j.stage, "uoc", j.use_old_copy)
    assert "is empty right now" in str(info.value)
    try:
        rec = recover(env.journal_path(), policy=env.policy)
        print("B11-PUTBACK-REFUSE recover:", rec.message[:200])
    except RelocateError as exc:
        print("B11-PUTBACK-REFUSE recover refused:", str(exc)[:300])
    assert _exists(env.source), "recover left the path empty"
    out = rollback(env.journal_path())
    print("B11-PUTBACK-REFUSE rerun:", out.message[:200])
    assert out.journal.stage == Stage.ROLLED_BACK and _manifest(env.source) == expected


@pytest.mark.parametrize("create_fails", [False, True])
def test_b11_holds_a_junction_removed_by_hand_then_use_old_copy_without_an_old_copy(
    env: Env, monkeypatch: pytest.MonkeyPatch, create_fails: bool
) -> None:
    """DELETED_OLD, the user removed the junction by hand (path empty, stage not ROLLING_BACK).
    use_old_copy finds no old copy and puts the junction back before refusing. Succeeds: the
    stage must be DELETED_OLD again (nothing else changed). Fails: RelocateError, the stage
    is left ROLLING_BACK, and `rollback` (as the message says) must finish."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    expected = _manifest(env.target)
    os.rmdir(_x(env.source))
    if create_fails:

        def create(link: str, target: str) -> None:
            raise PermissionError(13, "simulated: junction creation refused", link)

        with monkeypatch.context() as m:
            m.setattr(relocate, "_create_junction", create)
            with pytest.raises(RelocateError) as info:
                rollback(env.journal_path(), use_old_copy=True)
        print("B11-HAND fail", type(info.value).__name__, Journal.load(env.journal_path()).stage)
        assert "is empty right now" in str(info.value)
        out = rollback(env.journal_path())
        assert out.journal.stage == Stage.ROLLED_BACK and _manifest(env.source) == expected
    else:
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path(), use_old_copy=True)
        j = Journal.load(env.journal_path())
        print("B11-HAND ok", type(info.value).__name__, j.stage, str(info.value)[:120])
        assert _is_link(env.source) and j.stage == Stage.DELETED_OLD
        assert _manifest(env.source) == expected


def test_b11_holds_keep_root_flags_failing_in_the_guard_still_restores_the_junction(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    real_rename = relocate._rename

    def rename(a: str, b: str) -> None:
        if relocate._key(b) == relocate._key(env.source):
            raise PermissionError(13, "simulated: restore held", a)
        real_rename(a, b)

    def flags(moved: str, link: str) -> None:
        raise PermissionError(13, "simulated: attributes refused", link)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_rename", rename)
        m.setattr(relocate, "_keep_root_flags", flags)
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path())
    print("B11-FLAGS", str(info.value)[:300])
    assert _is_link(env.source)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK


# ------------------------------------------------------------------ 5. one-OSError sweeps


def _sweep_setup(base: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> tuple[Env, dict]:
    e = Env(base)
    _vol(monkeypatch, e)
    _make_tree(e.source)
    (e.d / "Relocated").mkdir()
    original = _manifest(e.source)
    if scenario == "normal":
        src, tgt = str(e.source), str(e.d / "Relocated" / "data")
    elif scenario == "case_variant":
        src, tgt = str(e.source).upper(), str(e.d / "RELOCATED" / "DATA")
    else:  # target_83_parent
        holder = e.d / "longtargetholderfolder"
        holder.mkdir()
        alias = _short_leaf(holder)
        if not alias:
            pytest.skip("8.3 names are disabled on this volume")
        src, tgt = str(e.source), str(e.d / alias / "data")
    return e, {"original": original, "src": src, "tgt": tgt}


def _settle(e: Env, want: dict) -> str | None:
    """Runs the move again (recovering any live journal first, as its message says) until
    done; returns what went wrong, if anything."""
    last = ""
    for _ in range(4):
        for jp in sorted(e.journals.glob("relocate-*.json")) if e.journals.exists() else []:
            j = Journal.load(jp)
            if j.stage not in {Stage.DONE, Stage.DELETED_OLD, Stage.ROLLED_BACK}:
                try:
                    recover(jp, policy=e.policy)
                except RelocateError as exc:
                    last = f"recover refused: {str(exc)[:160]}"
        try:
            _move(want["src"], want["tgt"], e)
            return None
        except RelocateError as exc:
            last = f"move refused: {str(exc)[:200]}"
            if _is_link(e.source):
                return None  # an earlier attempt finished it
            if "delete-old" in str(exc):
                # A rolled-back swap keeps the old copy (ADR-0044 addendum 5); the refusal names
                # the command, which is the way forward.
                move_id = str(exc).split("relocate delete-old ")[1].split("`")[0]
                delete_old_copy(
                    e.journals / f"relocate-{move_id}.json", policy=e.policy, confirmed=True
                )
    return last


@pytest.mark.parametrize("scenario", ["normal", "case_variant", "target_83_parent"])
def test_b11_sweep_one_os_error_in_plan_and_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    at = 1
    failures: list[str] = []
    empties: list[str] = []
    while True:
        base = tmp_path / f"o{at}"
        base.mkdir()
        e, want = _sweep_setup(base, monkeypatch, scenario)
        with monkeypatch.context() as m:
            state = _inject(m, at, base)
            try:
                _move(want["src"], want["tgt"], e)
                raised: BaseException | None = None
            except BaseException as exc:
                raised = exc
        where = f"fault {at} ({state['where']})"
        if not _exists(e.source):
            empties.append(f"{where}: {str(raised)[:160]}")
        if scenario == "target_83_parent":
            if raised is None or "short or alternate name" not in str(raised):
                failures.append(f"{where}: not refused by spelling: {raised!r}")
            if _names(e.c) != ["data"] or _is_link(e.source) or e.journals.exists():
                failures.append(f"{where}: something changed: {_names(e.c)}")
            if state["where"] is None:
                break
            at += 1
            continue
        if state["where"] is None:
            assert raised is None, f"no fault injected but {raised!r}"
            if _names(e.c) != ["data", "data.reclaim-moved"]:
                failures.append(f"clean run: C holds {_names(e.c)}")
            if _names(e.d) != ["Relocated"]:
                failures.append(f"clean run: D holds {_names(e.d)}")
            break
        if raised is not None and not isinstance(raised, RelocateError):
            failures.append(f"{where}: raw {type(raised).__name__}: {str(raised)[:100]}")
        if _exists(e.source) and _manifest(e.source) != want["original"]:
            failures.append(f"{where}: the folder's content changed")
        if raised is not None:
            bad = _settle(e, want)
            if bad:
                failures.append(f"{where}: {bad}")
        if not _is_link(e.source) or _manifest(e.source) != want["original"]:
            failures.append(f"{where}: final state: link {_is_link(e.source)}")
        if sorted(n.casefold() for n in _names(e.c)) != ["data", "data.reclaim-moved"] or (
            "data" not in _names(e.c)
        ):
            failures.append(f"{where}: C holds {_names(e.c)}")
        if _names(e.d) != ["Relocated"]:
            failures.append(f"{where}: D holds {_names(e.d)}")
        at += 1
    print(
        f"B11-SWEEP {scenario} points={at - 1} failures={len(failures)} "
        f"empty_after_fault={len(empties)}"
    )
    for line in empties:
        print("  EMPTY", line)
    for line in failures:
        print("  FAIL", line)
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures[:6])


def _unused(_: Callable[..., object]) -> None:  # keeps helper imports referenced
    return None
