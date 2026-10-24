from __future__ import annotations

# ruff: noqa: F811, T201, PTH104, PTH106, PTH110, PTH112, PTH208, RUF001
# Hostile-review probes (verifier B10, export of 55a81ab) for ADR-0044 addendum 9's four fixes:
# the on-disk spelling check, the "junction removed" log inside the guard, the created_at tie
# broken by id, and the rerun of an interrupted use_old_copy rollback. "_holds" tests assert a
# safety claim expected to hold; the others assert the claim the reviewer expected to break.
# Kept in the repo: 7 failed on 55a81ab and pass with addendum 10's fixes. One was changed by a
# decision (the double-fault probe now asserts the message, see its docstring).
# Every root is under tmp_path (the `env` fixture); no excluded project is touched (zzexclproj).
import ctypes
import os
import unicodedata
import uuid
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
from reclaim.config import Config, ExclusionsConfig
from reclaim.relocate import (
    Journal,
    Policy,
    RelocateError,
    Stage,
    Verdict,
    delete_old_copy,
    execute_move,
    recover,
    rollback,
)
from reclaim.safety import SafetyValidator


def _short_leaf(path: Path) -> str:
    """The 8.3 alias of `path`'s last component only ("" when 8.3 names are off)."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    if not n:
        return ""
    leaf = Path(buf.value).name
    return "" if leaf.casefold() == path.name.casefold() else leaf


def _plan(src: object, tgt: object, policy: Policy) -> relocate.Plan:
    return relocate.plan_move(src, tgt, policy, free_bytes=lambda _p: BIG)  # type: ignore[arg-type]


def _names(folder: Path) -> list[str]:
    return sorted(os.listdir(_x(folder)))


# ------------------------------------------------------------------ 1. the spelling check


def test_b10_holds_an_8dot3_name_in_a_parent_only_is_refused(env: Env) -> None:
    parent = env.c / "longparentfolder"
    src = parent / "data"
    _make_tree(src)
    alias = _short_leaf(parent)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    plan = _plan(env.c / alias / "data", env.target, env.policy)
    print("B10-83PARENT", plan.ok, plan.reasons)
    assert not plan.ok
    assert any("short or alternate name" in r and str(src) in r for r in plan.reasons)
    assert _names(env.c) == ["longparentfolder"] and _names(parent) == ["data"]


@pytest.mark.parametrize("variant", ["dotdot", "dot", "trailing_space", "trailing_dot"])
def test_b10_holds_non_canonical_text_spellings_are_refused(env: Env, variant: str) -> None:
    _make_tree(env.source)
    (env.c / "x").mkdir()
    given = {
        "dotdot": f"{env.c}\\x\\..\\data",
        "dot": f"{env.c}\\.\\data",
        "trailing_space": f"{env.source} ",
        "trailing_dot": f"{env.source}.",
    }[variant]
    plan = _plan(given, env.target, env.policy)
    print("B10-TEXT", variant, plan.ok, plan.reasons[:3])
    if plan.ok:
        # Accepted: then the move must keep the folder at its real path.
        execute_move(given, env.target, env.policy, env.journals, free_bytes=lambda _p: BIG)
        assert _is_link(env.source), f"{variant}: {_names(env.c)}"
    else:
        assert _names(env.c) == ["data", "x"]


def test_b10_holds_a_junction_in_a_parent_component_is_refused(env: Env) -> None:
    real = env.c / "realparent"
    _make_tree(real / "data")
    relocate._create_junction(str(env.c / "via"), str(real))
    plan = _plan(env.c / "via" / "data", env.target, env.policy)
    print("B10-JPARENT", plan.ok, plan.reasons)
    assert not plan.ok and any("reached through a link" in r for r in plan.reasons)


@pytest.mark.parametrize("on_disk", ["NFC", "NFD"])
def test_b10_holds_another_unicode_normalisation_is_another_name(env: Env, on_disk: str) -> None:
    name = "café-donnees"
    disk = unicodedata.normalize(on_disk, name)  # type: ignore[arg-type]
    typed = unicodedata.normalize("NFD" if on_disk == "NFC" else "NFC", name)
    assert disk != typed
    _make_tree(env.c / disk)
    plan = _plan(env.c / typed, env.target, env.policy)
    print("B10-NFx", on_disk, plan.ok, plan.reasons[:2])
    assert not plan.ok  # NTFS does not normalise: the other spelling does not exist
    assert _plan(env.c / disk, env.target, env.policy).ok


def test_b10_ntfs_case_equal_but_casefold_different_spelling(env: Env) -> None:
    """NTFS compares names through its upcase table; `_key` uses Python's casefold. A spelling
    NTFS treats as the same entry but casefold does not (or the reverse) is where a "case only"
    allowance can let a non-disk spelling into the journal."""
    _make_tree(env.c / "idata")
    alias = "ıdata"  # dotless i
    same_entry = _exists(env.c / alias)
    print("B10-DOTLESS same entry on NTFS:", same_entry)
    if not same_entry:
        return  # NTFS treats it as another name: nothing to alias
    plan = _plan(env.c / alias, env.target, env.policy)
    print("B10-DOTLESS", plan.ok, plan.reasons)
    assert not plan.ok


def test_b10_ntfs_distinct_names_that_casefold_together_only_over_refuse(env: Env) -> None:
    """KELVIN SIGN folds to 'k' in Python but is its own name on NTFS: two real folders. The
    journal checks conflate them; expected only an over-refusal (low), never an action."""
    kelvin, ascii_ = env.c / "Kdata", env.c / "kdata"
    _make_tree(kelvin)
    if _exists(ascii_):
        pytest.skip("NTFS treats KELVIN SIGN as k here")
    _make_tree(ascii_)
    execute_move(kelvin, env.d / "t-kelvin", env.policy, env.journals, free_bytes=lambda _p: BIG)
    plan = relocate.plan_move(
        ascii_, env.d / "t-ascii", env.policy, free_bytes=lambda _p: BIG, journal_dir=env.journals
    )
    print("B10-KELVIN", plan.ok, plan.reasons)
    assert _is_link(kelvin) and not _is_link(ascii_)


def test_b10_a_case_only_spelling_keeps_the_folder_name_on_disk(env: Env) -> None:
    """Case-only differences are allowed. The freeze renames the entry to the TYPED spelling
    (`DATA.reclaim-moved`), the junction is created as `DATA`, and a rollback puts the folder
    back as `DATA`: the folder's on-disk name changes case for good."""
    _make_tree(env.source)
    typed = str(env.source).upper()
    execute_move(typed, env.target, env.policy, env.journals, free_bytes=lambda _p: BIG)
    after_move = _names(env.c)
    rollback(env.journal_path())
    after = _names(env.c)
    print("B10-CASE after move", after_move, "after rollback", after)
    assert "data" in after, f"the folder 'data' is now spelled {after}"


def test_b10_holds_a_case_only_spelling_still_blocks_a_second_move(env: Env) -> None:
    _make_tree(env.source)
    execute_move(
        str(env.source).upper(), env.target, env.policy, env.journals, free_bytes=lambda _p: BIG
    )
    plan = relocate.plan_move(
        env.source,
        env.d / "other",
        env.policy,
        free_bytes=lambda _p: BIG,
        journal_dir=env.journals,
    )
    assert not plan.ok


def test_b10_a_loopback_unc_spelling_of_the_source_is_refused(env: Env) -> None:
    """`\\\\localhost\\C$\\...` names the same folder by another text: every journal check
    compares text, so the M1 claim "no journal can hold an alias" needs this refused."""
    _make_tree(env.source)
    drive, rest = str(env.source)[0], str(env.source)[2:]
    unc = f"\\\\localhost\\{drive}$" + rest
    if not os.path.isdir(unc):
        pytest.skip("the admin share is not reachable")
    spelled = relocate._final_name(relocate._norm(unc))
    plan = _plan(unc, env.target, env.policy)
    print("B10-UNC long_name:", spelled)
    print("B10-UNC plan", plan.ok, plan.reasons)
    assert not plan.ok, f"the UNC alias {unc} passed the plan"


def test_b10_holds_already_moved_and_dangling_sources_plan_without_error(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    found = _plan(env.source, env.d / "again", env.policy)
    print("B10-MOVED", found.reasons)
    assert not found.ok and any("already a link" in r for r in found.reasons)
    os.rename(_x(env.target), _x(Path(str(env.target) + "-away")))
    print("B10-MOVED long_name dangling:", relocate._final_name(str(env.source)))
    dangling = _plan(env.source, env.d / "again", env.policy)
    print("B10-DANGLING", dangling.reasons)
    assert not dangling.ok


def test_b10_a_target_through_an_8dot3_parent_is_checked_like_its_real_path(env: Env) -> None:
    """The spelling check runs on the source only. `app_dirs` and `hardlink_caches` are text
    checks on the target: an 8.3 alias of Reclaim's data folder passes them."""
    app = env.d / "reclaimappdata"
    app.mkdir()
    alias = _short_leaf(app)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    validator = SafetyValidator(Config(exclusions=ExclusionsConfig(project_names=["zzexclproj"])))
    policy = Policy(
        exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
        protected=lambda _p: False,
        app_dirs=(app,),
        hardlink_caches=(app,),
    )
    _make_tree(env.source)
    assert not _plan(env.source, app / "moved", policy).ok  # the real spelling is refused
    plan = _plan(env.source, env.d / alias / "moved", policy)
    print("B10-TGT83 alias", alias, plan.ok, plan.reasons)
    assert not plan.ok, f"target {env.d / alias / 'moved'} inside Reclaim's data passed"


def test_b10_a_target_alias_cannot_nest_a_move_inside_another_moves_target(env: Env) -> None:
    """Move A to D\\longtargetsfolder\\a. Move B gives its target through the 8.3 alias of
    D\\longtargetsfolder, inside A's live target. The overlap check compares text."""
    holder = env.d / "longtargetsfolder"
    a_src, a_tgt = env.c / "adata", holder / "a"
    b_src = env.c / "bdata"
    _make_tree(a_src)
    _write(b_src / "b-only.txt", b"only B has this")
    execute_move(a_src, a_tgt, env.policy, env.journals, free_bytes=lambda _p: BIG)
    alias = _short_leaf(holder)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    b_tgt = env.d / alias / "a" / "b"
    plan = relocate.plan_move(
        b_src, b_tgt, env.policy, free_bytes=lambda _p: BIG, journal_dir=env.journals
    )
    print("B10-NEST plan", plan.ok, plan.reasons)
    assert not plan.ok, f"B's target {b_tgt} is inside A's live target and was accepted"


def test_b10_consequence_of_a_nested_target_alias_after_a_rollback_of_a(env: Env) -> None:
    """Continues the previous probe when the plan accepts: B moved and its old copy deleted,
    then A rolled back. Does B's folder stay reachable at its own path?"""
    holder = env.d / "longtargetsfolder"
    a_src, a_tgt = env.c / "adata", holder / "a"
    b_src = env.c / "bdata"
    _make_tree(a_src)
    _write(b_src / "b-only.txt", b"only B has this")
    execute_move(a_src, a_tgt, env.policy, env.journals, free_bytes=lambda _p: BIG)
    a_journal = env.journal_path()
    alias = _short_leaf(holder)
    if not alias:
        pytest.skip("8.3 names are disabled on this volume")
    b_tgt = env.d / alias / "a" / "b"
    try:
        execute_move(b_src, b_tgt, env.policy, env.journals, free_bytes=lambda _p: BIG)
    except RelocateError as exc:
        print("B10-NEST2 B refused:", str(exc)[:200])
        return
    (b_journal,) = [p for p in env.journals.glob("relocate-*.json") if p != a_journal]
    delete_old_copy(b_journal, policy=env.policy, confirmed=True)
    try:
        out = rollback(a_journal)
        print("B10-NEST2 rollback A:", out.message[:300])
    except RelocateError as exc:
        print("B10-NEST2 rollback A refused:", str(exc)[:300])
    reachable = os.path.exists(_x(b_src / "b-only.txt"))
    print(
        "B10-NEST2 B reachable at its path:",
        reachable,
        "A src has b:",
        _exists(a_src / "b"),
        "A tgt exists:",
        _exists(a_tgt),
    )
    try:
        rb = rollback(b_journal)
        print("B10-NEST2 rollback B:", rb.message[:300])
    except RelocateError as exc:
        print("B10-NEST2 rollback B refused:", str(exc)[:300])
    # No byte lost: B's data went back with A's folder.
    assert (a_src / "b" / "b-only.txt").read_bytes() == b"only B has this"
    assert reachable, "B's folder is a dangling junction after A's rollback"


def test_b10_long_name_of_a_unc_path_is_a_unc_path(env: Env) -> None:
    """`_long_name` hands GetLongPathNameW a `\\\\?\\UNC\\` path and `_norm` strips only `\\\\?\\`,
    so the answer is `UNC\\host\\...` made absolute against the CURRENT DIRECTORY."""
    _make_tree(env.source)
    drive, rest = str(env.source)[0], str(env.source)[2:]
    unc = f"\\\\localhost\\{drive}$" + rest
    if not os.path.isdir(unc):
        pytest.skip("the admin share is not reachable")
    spelled = relocate._final_name(relocate._norm(unc))
    typed_prefixed = relocate._norm("\\\\?\\UNC\\localhost\\" + drive + "$" + rest)
    print("B10-UNC2", spelled, "|", typed_prefixed)
    assert spelled is not None and spelled.startswith("\\\\"), spelled
    assert typed_prefixed.startswith("\\\\"), typed_prefixed


# ------------------------------------------------------------------ 2. the rerun branch


def test_b10_rerun_of_an_interrupted_put_back_of_the_verified_restore_keeps_its_warning(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling of B9 L2. Old copy deleted; a copy-back made and verified the restore folder and
    was killed before the junction went; the target drive then went away; the next rollback
    puts the verified restore back (its first-run note warns that later writes are only on the
    target) and is killed before ROLLED_BACK. The rerun's note must keep that warning."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)

    def die_at_removal(link: str) -> None:
        raise SimulatedCrash("killed at the junction's removal")

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", die_at_removal)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    j = Journal.load(env.journal_path())
    assert j.restore_verified, "setup: the restore copy should be verified"
    away = Path(str(env.target) + "-away")
    os.rename(_x(env.target), _x(away))  # the target's drive is unplugged
    real = Journal.log

    def die_at_the_end(self: Journal, stage: str, *args: object, **kwargs: object) -> None:
        if stage == Stage.ROLLED_BACK:
            raise SimulatedCrash("before the journal recorded the rollback")
        real(self, stage, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(Journal, "log", die_at_the_end)
        with pytest.raises(SimulatedCrash):
            rollback(env.journal_path())
    assert _exists(env.source) and not _is_link(env.source)
    out = rollback(env.journal_path())
    print("B10-RERUN-RESTORE", out.journal.stage, out.message)
    assert out.journal.stage == Stage.ROLLED_BACK
    assert str(env.target) in out.message and "only there" in out.message, out.message


def test_b10_holds_rerun_branch_after_a_manual_undo_keeps_the_moved_folder(env: Env) -> None:
    """The user undid the move by hand (removed the junction, renamed the old copy back): the
    folder at the source carries source_id though no use_old_copy run happened. Rollback must
    not lose what was written through the junction and must say where it is."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    os.rmdir(_x(env.source))
    os.rename(_x(Path(str(env.source) + relocate.MOVED_SUFFIX)), _x(env.source))
    out = rollback(env.journal_path())
    print("B10-MANUAL", out.message)
    assert (env.target / "after-move.txt").read_bytes() == b"written through the junction"
    assert str(env.target) in out.message


# ------------------------------------------------------------------ 3. the tie-break


@pytest.mark.parametrize("order", ["a_lower", "a_higher"])
def test_b10_holds_a_tie_between_two_real_moves_never_lets_the_older_act(
    env: Env, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """A rolled back (frozen) at clock T, then B moves the same folder at the same T. Whatever
    the ids' order, no command on A may change B's live move, and B can still be rolled back."""
    ids = iter(
        ["000000000000", "ffffffffffff"] if order == "a_lower" else ["ffffffffffff", "000000000000"]
    )

    class _U:
        def __init__(self, h: str) -> None:
            self.hex = h

    monkeypatch.setattr(relocate.uuid, "uuid4", lambda: _U(next(ids)))
    _make_tree(env.source)
    real = relocate._delta_sync
    with monkeypatch.context() as m:
        m.setattr(
            relocate,
            "_delta_sync",
            lambda *a, **k: (_ for _ in ()).throw(
                RelocateError("simulated failure after the freeze")
            ),
        )
        with pytest.raises(RelocateError):
            env.move(clock=lambda: 1_500_000_000.0)
    assert relocate._delta_sync is real
    a = sorted(env.journals.glob("relocate-*.json"))[0]
    assert Journal.load(a).stage == Stage.ROLLED_BACK
    env.move(clock=lambda: 1_500_000_000.0)
    (b,) = [p for p in env.journals.glob("relocate-*.json") if p != a]
    moved = Path(str(env.source) + relocate.MOVED_SUFFIX)
    before_old, before_live = _manifest(moved), _manifest(env.target)
    results = []
    for name, cmd in (
        ("rollback", lambda: rollback(a)),
        ("rollback_old", lambda: rollback(a, use_old_copy=True)),
        ("delete_old", lambda: delete_old_copy(a, policy=env.policy, confirmed=True)),
        ("recover", lambda: recover(a, policy=env.policy)),
    ):
        try:
            results.append(f"{name}: {cmd().message[:80]}")
        except RelocateError as exc:
            results.append(f"{name}: refused {str(exc)[:80]}")
    print("B10-TIE", order, results)
    assert _is_link(env.source) and Journal.load(b).stage == Stage.DONE
    assert _manifest(moved) == before_old and _manifest(env.target) == before_live
    out = rollback(b)
    assert out.journal.stage == Stage.ROLLED_BACK


# ------------------------------------------------------------------ 4. after the junction's removal


def test_b10_a_failing_junction_recreate_in_the_guard_says_the_path_is_empty_and_a_rerun_fixes_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double fault: after the junction is removed, the rename of the restore folder fails AND
    the guard's `_create_junction` fails. As written by the reviewer this asserted the path is
    never empty; after two faults on the same path nothing can promise that, so the decision
    (ADR-0044 addendum 10) is an exact message instead: the path is empty, the data is on the
    target, and the same command finishes. The rerun restoring everything is still asserted."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "after-move.txt", b"written through the junction")
    expected = _manifest(env.target)
    state = {"removed": False}
    real_remove, real_rename = relocate._remove_junction, relocate._rename

    def remove(link: str) -> None:
        real_remove(link)
        state["removed"] = True

    def rename(src: str, dst: str) -> None:
        if state["removed"] and relocate._key(dst) == relocate._key(env.source):
            raise PermissionError(13, "simulated: restore held by a scanner", src)
        real_rename(src, dst)

    def create(link: str, target: str) -> None:
        raise PermissionError(13, "simulated: junction creation refused", link)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", remove)
        m.setattr(relocate, "_rename", rename)
        m.setattr(relocate, "_create_junction", create)
        with pytest.raises(Exception) as info:
            rollback(env.journal_path())
    empty = not _exists(env.source)
    print("B10-DOUBLE", type(info.value).__name__, str(info.value)[:300], "empty:", empty)
    out = rollback(env.journal_path())
    print("B10-DOUBLE rerun", out.message[:200])
    assert _manifest(env.source) == expected
    assert isinstance(info.value, RelocateError)
    text = str(info.value)
    assert empty and "is empty right now" in text and str(env.target) in text
    assert "Nothing is lost" in text and "again" in text


def test_b10_holds_every_journal_write_failing_after_removal_keeps_the_path(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    state = {"removed": False}
    real_remove, real_save = relocate._remove_junction, Journal.save

    def remove(link: str) -> None:
        real_remove(link)
        state["removed"] = True

    def save(self: Journal) -> None:
        if state["removed"]:
            raise PermissionError(13, "simulated: journal drive full", self.path + ".tmp")
        real_save(self)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", remove)
        m.setattr(Journal, "save", save)
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path())
    print("B10-ALLSAVES", str(info.value)[:300])
    assert _is_link(env.source)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK


def test_b10_holds_put_back_old_copy_journal_write_failure_after_removal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The use_old_copy path's removal-to-rename window has no journal write; a write failing
    at its ROLLED_BACK entry comes after the folder is back."""
    _make_tree(env.source)
    before = _manifest(env.source)
    env.move()
    state = {"removed": False}
    real_remove, real_save = relocate._remove_junction, Journal.save

    def remove(link: str) -> None:
        real_remove(link)
        state["removed"] = True

    def save(self: Journal) -> None:
        if state["removed"]:
            raise PermissionError(13, "simulated: journal drive full", self.path + ".tmp")
        real_save(self)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", remove)
        m.setattr(Journal, "save", save)
        with pytest.raises(RelocateError):
            rollback(env.journal_path(), use_old_copy=True)
    assert _exists(env.source)
    out = rollback(env.journal_path(), use_old_copy=True)
    assert out.journal.stage == Stage.ROLLED_BACK and _manifest(env.source) == before
    assert Verdict  # keeps the import used
    assert uuid
