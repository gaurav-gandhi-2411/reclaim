from __future__ import annotations

# ruff: noqa: F811, PTH102, PTH104, PTH106, PTH107, PTH116, PTH123
# Hostile-review probes for reclaim.relocate (verifier B1). Each test asserts the safety claim;
# a failure on the current code demonstrates the finding.
# F811: the `env` fixture is imported from test_relocate and then named as a test argument.
import ctypes
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

import reclaim.relocate as relocate
from reclaim.relocate import (
    MOVED_SUFFIX,
    Policy,
    RelocateError,
    Stage,
    Verdict,
    classify,
    delete_old_copy,
    execute_move,
    plan_move,
    recover,
    rollback,
)


def _move(env: Env, source: Path, target: Path, **kw: object) -> relocate.Outcome:
    return execute_move(source, target, env.policy, env.journals, free_bytes=lambda _p: BIG, **kw)  # type: ignore[arg-type]


def _journal_for(env: Env, source: Path) -> Path:
    for p in sorted(env.journals.glob("relocate-*.json")):
        if relocate._key(relocate.Journal.load(p).source) == relocate._key(source):
            return p
    raise AssertionError("no journal")


# ------------------------------------------------------------------ claim 1: kills and states


def test_kill_inside_junction_creation_is_not_reported_as_restored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between _create_junction's mkdir and its FSCTL leaves an empty plain folder at the
    source. recover/rollback must not report the original as restored while it is empty."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    real_create = relocate._create_junction

    def mkdir_then_die(link: str, target: str) -> None:
        os.mkdir(_x(link))
        raise SimulatedCrash("killed between mkdir and FSCTL_SET_REPARSE_POINT")

    monkeypatch.setattr(relocate, "_create_junction", mkdir_then_die)
    with pytest.raises(SimulatedCrash):
        env.move()
    monkeypatch.setattr(relocate, "_create_junction", real_create)

    try:
        outcome = recover(env.journal_path(), policy=env.policy)
    except RelocateError:
        outcome = None  # a refusal would be acceptable; a false "restored" is not
    if outcome is not None and outcome.journal.stage == Stage.ROLLED_BACK:
        assert _manifest(env.source) == expected, (
            f"reported '{outcome.message}' but the source holds {_manifest(env.source)}"
        )


def test_rollback_with_a_recreated_plain_source_is_not_reported_as_restored(env: Env) -> None:
    """After the move, the junction is removed and a program recreates its folder (a common
    reaction to a missing cache dir). rollback must not call that 'restored'."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    os.rmdir(_x(env.source))  # junction removed (user, another cleaner, an installer)
    _write(env.source / "fresh.log", b"recreated by the program")

    try:
        outcome = rollback(env.journal_path())
    except RelocateError:
        return  # refusing is fine
    manifest = _manifest(env.source)
    for rel, digest in expected.items():
        assert manifest.get(rel) == digest, (
            f"rollback said '{outcome.message}' but {rel} is not at the original path; "
            f"stage={outcome.journal.stage}"
        )


def test_kill_during_rollbacks_target_probe_is_recoverable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill inside _probe_target (target renamed to .reclaim-probe) must be recoverable by the
    next rollback/recover; nothing renames a target probe back."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    real_probe = relocate._probe_target

    def rename_then_die(target: str) -> None:
        relocate._rename(target, target + relocate.PROBE_SUFFIX)
        raise SimulatedCrash("killed inside the target probe")

    monkeypatch.setattr(relocate, "_probe_target", rename_then_die)
    with pytest.raises(SimulatedCrash):
        rollback(env.journal_path())
    monkeypatch.setattr(relocate, "_probe_target", real_probe)

    outcome = recover(env.journal_path(), policy=env.policy)
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source)
    assert _manifest(env.source) == expected


def test_kill_during_the_plan_probe_leaves_the_source_or_a_journal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan_move's probe renames the source away before any journal exists. A kill between the
    two renames leaves the original path missing with nothing for `recover` to act on."""
    _make_tree(env.source)
    real_rename = relocate._rename
    calls = {"n": 0}

    def rename(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise SimulatedCrash("killed inside the probe")
        real_rename(src, dst)

    monkeypatch.setattr(relocate, "_rename", rename)
    with pytest.raises(SimulatedCrash):
        env.move()
    journals = list(env.journals.glob("relocate-*.json")) if env.journals.exists() else []
    assert _exists(env.source) or journals, (
        "source is at .reclaim-probe and there is no journal to recover from"
    )


def test_a_journal_dir_inside_the_source_does_not_lose_the_original_path(env: Env) -> None:
    """`--journal-dir` is free-form and not on the must-stay list. Pointing it inside the source
    must be refused, or at least never leave the source missing."""
    _make_tree(env.source)
    expected = _manifest(env.source)
    jdir = env.source / "journals-here"
    with pytest.raises(RelocateError, match="journal folder"):
        execute_move(env.source, env.target, env.policy, jdir, free_bytes=lambda _p: BIG)
    assert _manifest(env.source) == expected
    assert not _exists(env.target)


# ------------------------------------------------------------------ claim 1: two moves


def test_rollback_of_a_move_whose_target_holds_another_moves_target(env: Env) -> None:
    """Move B's target inside move A's target would let A's rollback remove B's live data:
    the plan now refuses any overlap with another open move."""
    _make_tree(env.source)
    _move(env, env.source, env.target)
    b_src = env.c / "other"
    _write(b_src / "only-in-b.bin", os.urandom(4096))
    expected_b = _manifest(b_src)
    plan = plan_move(
        b_src,
        env.target / "nested",
        env.policy,
        free_bytes=lambda _p: BIG,
        journal_dir=env.journals,
    )
    assert not plan.ok
    assert any("overlaps move" in r for r in plan.reasons), plan.reasons
    with pytest.raises(RelocateError, match="overlaps move"):
        _move(env, b_src, env.target / "nested")
    assert _manifest(b_src) == expected_b
    # And the other way round: a new move whose source holds an open move's target.
    plan = plan_move(
        env.d, env.c / "x", env.policy, free_bytes=lambda _p: BIG, journal_dir=env.journals
    )
    assert any("overlaps move" in r for r in plan.reasons), plan.reasons


def test_a_target_through_a_link_or_in_a_working_folder_is_refused(env: Env) -> None:
    _make_tree(env.source)
    linked = env.d / "linked"
    linked.mkdir()
    relocate._create_junction(str(env.d / "alias"), str(linked))
    for target, words in (
        (env.d / "alias" / "data", "reached through a link"),
        (env.d / ("x" + MOVED_SUFFIX) / "data", "Reclaim uses while moving"),
    ):
        plan = plan_move(env.source, target, env.policy, free_bytes=lambda _p: BIG, run_probe=False)
        assert any(words in r for r in plan.reasons), (target, plan.reasons)


def test_an_unreadable_journal_blocks_the_plan(env: Env) -> None:
    _make_tree(env.source)
    env.journals.mkdir()
    (env.journals / "relocate-broken.json").write_text("{not json", encoding="utf-8")
    plan = plan_move(
        env.source, env.target, env.policy, free_bytes=lambda _p: BIG, journal_dir=env.journals
    )
    assert any("could not be read" in r for r in plan.reasons), plan.reasons


def test_a_recreated_folder_after_the_old_copy_was_deleted_is_not_called_restored(
    env: Env,
) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    os.rmdir(_x(env.source))  # the junction, removed by something else
    _write(env.source / "fresh.log", b"recreated")
    with pytest.raises(RelocateError, match="something else created"):
        rollback(env.journal_path())
    assert _manifest(env.target) == expected  # the data is safe where it was
    os.remove(_x(env.source / "fresh.log"))
    os.rmdir(_x(env.source))
    outcome = rollback(env.journal_path())
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_an_empty_recreated_folder_is_replaced_by_the_original(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"edited through the junction")
    key = str(Path("sub") / "b.txt")
    expected[key] = _manifest(env.target)[key]
    os.rmdir(_x(env.source))
    os.mkdir(_x(env.source))
    outcome = rollback(env.journal_path())
    assert outcome.journal.stage == Stage.ROLLED_BACK
    assert _manifest(env.source) == expected


def test_rollback_of_a_move_whose_old_copy_holds_another_moves_target(env: Env) -> None:
    """Move B's target is inside move A's `.reclaim-moved` folder. Rolling back A renames that
    folder, so B's junction dangles."""
    _make_tree(env.source)
    _move(env, env.source, env.target)
    b_src = env.d / "bsrc"
    _write(b_src / "only-in-b.bin", os.urandom(4096))
    expected_b = _manifest(b_src)
    b_tgt = Path(str(env.source) + MOVED_SUFFIX) / "nested"
    plan = plan_move(b_src, b_tgt, env.policy, free_bytes=lambda _p: BIG, run_probe=False)
    if not plan.ok:
        return  # refused up front: the claim holds
    _move(env, b_src, b_tgt)
    delete_old_copy(_journal_for(env, b_src), policy=env.policy, confirmed=True)
    rollback(_journal_for(env, env.source))
    assert _manifest(b_src) == expected_b


# ------------------------------------------------------------------ claim 1: content fidelity


def test_alternate_data_streams_survive_the_move_and_delete(env: Env) -> None:
    """_copy_file copies only the unnamed stream. Named streams (Zone.Identifier, app metadata)
    are silently dropped, then delete_old_copy removes the only copy."""
    _make_tree(env.source)
    with open(_x(env.source / "a.bin") + ":meta", "wb") as fh:
        fh.write(b"data that lives in a named stream")
    env.move()
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    with open(_x(env.source / "a.bin") + ":meta", "rb") as fh:
        assert fh.read() == b"data that lives in a named stream"


def test_a_write_that_restores_its_mtime_before_the_freeze_reaches_the_target(env: Env) -> None:
    """A writer that keeps size and restores mtime (robocopy/7-zip/cp -p style) between verify
    and freeze: the delta pass skips it, the move reports done, and the junction serves stale
    bytes."""
    _make_tree(env.source)
    path = env.source / "sub" / "b.txt"
    st = os.stat(_x(path))

    def hook(name: str) -> None:
        if name == "after_verify":
            with open(_x(path), "wb") as fh:
                fh.write(b"HELLO")
            os.utime(_x(path), ns=(st.st_atime_ns, st.st_mtime_ns))

    # The file must be older than the 2 s window for the skip to apply.
    old = st.st_mtime_ns - 10_000_000_000
    os.utime(_x(path), ns=(old, old))
    st = os.stat(_x(path))
    outcome = env.move(checkpoint=hook)
    assert outcome.journal.stage == Stage.DONE
    with open(_x(path), "rb") as fh:  # through the junction
        assert fh.read() == b"HELLO", "the move reported done but serves the pre-write bytes"


def test_a_case_only_rename_before_the_freeze_moves_cleanly(env: Env) -> None:
    _make_tree(env.source)

    def hook(name: str) -> None:
        if name == "after_verify":
            os.rename(_x(env.source / "sub" / "b.txt"), _x(env.source / "sub" / "B.txt"))

    expected_after = None
    try:
        env.move(checkpoint=hook)
    except RelocateError as exc:
        expected_after = str(exc)
    # Original must be intact either way.
    assert _exists(env.source)
    with open(_x(env.source / "sub" / "B.txt"), "rb") as fh:
        assert fh.read() == b"hello"
    assert expected_after is None, f"case-only rename failed the move: {expected_after}"


# ------------------------------------------------------------------ claim 2: exclusion aliases


def _no_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_scan(_root: str) -> None:
        raise AssertionError("an excluded tree was scanned")

    monkeypatch.setattr(relocate, "scan_tree", no_scan)


def test_exclusion_through_a_junction_alias(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    excl = env.c / "zzexclproj"
    _make_tree(excl / "data")
    alias = env.c / "alias"
    relocate._create_junction(str(alias), str(excl))
    _no_scan(monkeypatch)
    found = classify(alias / "data", env.policy)
    assert found.verdict is Verdict.MUST_STAY
    plan = plan_move(alias / "data", env.d / "t", env.policy, free_bytes=lambda _p: BIG)
    assert not plan.ok
    monkeypatch.undo()
    monkeypatch.setattr(
        relocate,
        "_volume_serial",
        lambda p: 2 if relocate._key(p).startswith(relocate._key(env.d)) else 1,
    )
    _make_tree(env.source)
    plan = plan_move(
        env.source, alias / "newdir", env.policy, free_bytes=lambda _p: BIG, run_probe=False
    )
    assert not plan.ok


def test_exclusion_through_an_8dot3_name(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    excl = env.c / "zzexclproj-long-name"
    _make_tree(excl)
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(excl), buf, 1024)
    short = buf.value if n else ""
    if not short or Path(short).name.casefold() == excl.name.casefold():
        pytest.skip("8.3 names are disabled on this volume")
    _no_scan(monkeypatch)
    assert classify(Path(short), env.policy).verdict is Verdict.MUST_STAY


def test_exclusion_trailing_dot_and_case(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    excl = env.c / "ZZEXCLPROJ"
    _make_tree(excl)
    _no_scan(monkeypatch)
    for p in (
        str(env.c / "zzExclProj"),
        str(env.c / "zzexclproj") + ".",
        str(env.c / "zzexclproj "),
    ):
        assert classify(p, env.policy).verdict is Verdict.MUST_STAY, p


def test_a_folder_holding_a_junction_to_an_excluded_project(env: Env) -> None:
    """B1 finding 9, accepted: the shared exclusion walk ()
    descends junctions on purpose (content reached through a junction counts, the pnpm case;
    #190 bounds cycles), so it reads names under the link. The verdict is what matters here:
    the folder must stay, and nothing under it is sized, probed or copied."""
    excl = env.c / "zzexclproj"
    _make_tree(excl)
    holder = env.c / "holder"
    _write(holder / "f.txt", b"x")
    relocate._create_junction(str(holder / "link"), str(excl))
    found = classify(holder, env.policy)
    assert found.verdict is Verdict.MUST_STAY
    assert any("links" in r for r in found.reasons), found.reasons


# ------------------------------------------------------------------ claim 5: delete_old_copy


def test_delete_old_copy_after_a_target_file_was_replaced_by_an_older_version(env: Env) -> None:
    """Spec: a different mtime is accepted without hashing. A program restoring an older backup
    of a file through the junction is a real edit, so this is by design -- recorded as held."""
    _make_tree(env.source)
    env.move()
    p = env.target / "sub" / "b.txt"
    st = os.stat(_x(p))
    os.utime(_x(p), ns=(st.st_atime_ns, st.st_mtime_ns + 1))  # mtime moved, bytes same
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    with open(_x(env.source / "sub" / "b.txt"), "rb") as fh:
        assert fh.read() == b"hello"


def test_policy_without_exclusions_still_runs(tmp_path: Path) -> None:
    # Sanity: the probes above use the fixture's policy.
    Policy(exclusion_match=lambda p: None, protected=lambda p: False)


def test_a_stream_that_differs_in_the_copy_refuses_the_delete(env: Env) -> None:
    """Mutation R7 (streams left out of the hash): a copy whose named stream differs, with its
    main content and mtime intact, is a bad copy and must not let the old copy go."""
    _make_tree(env.source)
    with open(_x(env.source / "a.bin") + ":meta", "wb") as fh:
        fh.write(b"original stream")
    env.move()
    victim = _x(env.target / "a.bin")
    st = os.stat(victim)
    relocate._set_attributes(victim, 0)
    with open(victim + ":meta", "wb") as fh:
        fh.write(b"altered stream!")
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(RelocateError, match="differ"):
        delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    assert _exists(str(env.source) + MOVED_SUFFIX)


def test_a_bad_copy_back_stops_the_rollback_with_the_junction_in_place(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation R13 (copy-back unverified): if what rollback copies back is not byte-equal to
    the target, the rollback must stop before the junction is removed."""
    _make_tree(env.source)
    env.move()
    _write(env.source / "new-after-move.bin", b"written through the junction")
    expected = _manifest(env.target)
    real_copy = relocate._copy_file

    def bad_copy(src: str, dst: str) -> None:
        real_copy(src, dst)
        relocate._set_attributes(dst, 0)
        with open(dst, "r+b") as fh:
            fh.write(b"X")

    monkeypatch.setattr(relocate, "_copy_file", bad_copy)
    with pytest.raises(RelocateError, match="Nothing changed"):
        rollback(env.journal_path())
    assert _is_link(env.source)
    assert _manifest(env.target) == expected
