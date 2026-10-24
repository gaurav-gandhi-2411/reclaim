from __future__ import annotations

# ruff: noqa: PTH101, PTH103, PTH106, PTH107, PTH115, PTH116, PTH123, S603, S607
# The trees here go past 260 characters, so every call takes an extended-length string
# (`long_path`), which pathlib would normalise away; icacls sets the ACL test's deny entry.
import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import reclaim.relocate as relocate
from reclaim.config import Config, ExclusionsConfig
from reclaim.relocate import (
    KEEP_OLD_COPY_DAYS,
    MOVED_SUFFIX,
    InUseError,
    Journal,
    Policy,
    RelocateError,
    Stage,
    Verdict,
    classify,
    default_policy,
    delete_old_copy,
    execute_move,
    known_locations,
    plan_move,
    propose_candidates,
    recover,
    rollback,
    suggested_target,
)
from reclaim.safety import SafetyValidator
from reclaim.scanner import long_path

BIG = 10**15  # "plenty of free space" for the injected free-space probe
DAY = 86400.0


class SimulatedCrash(BaseException):
    """Stands in for the process dying: not an Exception, so the move's own rollback never runs
    and the journal is left exactly as a kill would leave it."""


def _x(path: Path | str) -> str:
    return long_path(Path(path))


def _write(path: Path, data: bytes) -> None:
    os.makedirs(_x(path.parent), exist_ok=True)
    with open(_x(path), "wb") as fh:
        fh.write(data)


def _manifest(root: Path | str) -> dict[str, str]:
    """rel -> sha256 for files, rel -> "<dir>" for directories, never following a link."""
    out: dict[str, str] = {}
    base = _x(root)
    stack = [("", base)]
    while stack:
        rel_dir, current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                rel = rel_dir + "\\" + entry.name if rel_dir else entry.name
                if entry.is_junction() or entry.is_symlink():
                    continue  # a link is not data of this tree
                if entry.is_dir(follow_symlinks=False):
                    out[rel] = "<dir>"
                    stack.append((rel, entry.path))
                else:
                    with open(entry.path, "rb") as fh:
                        out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _attrs(path: Path) -> int:
    return os.stat(_x(path), follow_symlinks=False).st_file_attributes


def _is_link(path: Path | str) -> bool:
    try:
        st = os.stat(_x(path), follow_symlinks=False)
    except OSError:
        return False
    return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _exists(path: Path | str) -> bool:
    try:
        os.stat(_x(path), follow_symlinks=False)
    except OSError:
        return False
    return True


def _make_tree(root: Path) -> None:
    _write(root / "a.bin", os.urandom(300_000))
    _write(root / "sub" / "b.txt", b"hello")
    _write(root / "sub" / "deeper" / "c [1].txt", b"bracketed")
    _write(root / "sub" / "zero.bin", b"")
    _write(root / "ünïcode-名前.txt", b"names")
    os.makedirs(_x(root / "empty-dir"))
    ro = root / "readonly.txt"
    _write(ro, b"read only")
    os.chmod(_x(ro), stat.S_IREAD)
    hidden = root / "hidden.txt"
    _write(hidden, b"hidden")
    relocate._set_attributes(_x(hidden), stat.FILE_ATTRIBUTE_HIDDEN)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.c = tmp / "C"
        self.d = tmp / "D"
        self.c.mkdir()
        self.d.mkdir()
        self.journals = tmp / "journals"
        self.source = self.c / "data"
        self.target = self.d / "relocated" / "data"
        self.config = Config(exclusions=ExclusionsConfig(project_names=["zzexclproj"]))
        validator = SafetyValidator(self.config)
        self.policy = Policy(
            exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
            protected=lambda p: False,
        )

    def move(self, **kwargs: object) -> relocate.Outcome:
        return execute_move(
            self.source,
            self.target,
            self.policy,
            self.journals,
            free_bytes=lambda _p: BIG,
            **kwargs,  # type: ignore[arg-type]
        )

    def journal_path(self) -> Path:
        (found,) = sorted(self.journals.glob("relocate-*.json"))
        return found


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    e = Env(tmp_path)
    d_key = relocate._key(e.d)

    def serial(path: str) -> int:
        return 2 if relocate._key(path).startswith(d_key) else 1

    # The test machine's tmp dir is on one volume; the volume check reads "D" as another one.
    monkeypatch.setattr(relocate, "_volume_serial", serial)
    return e


def _assert_original(env: Env, expected: dict[str, str]) -> None:
    assert not _is_link(env.source)
    assert _manifest(env.source) == expected
    assert not _exists(str(env.source) + MOVED_SUFFIX)
    assert not _exists(str(env.source) + relocate.PROBE_SUFFIX)
    assert not _exists(env.target)


def _assert_moved(env: Env, expected: dict[str, str]) -> None:
    assert _is_link(env.source)
    assert relocate._key(os.readlink(_x(env.source))[4:]) == relocate._key(env.target)
    assert _manifest(env.target) == expected
    assert _manifest(env.source) == expected


# ------------------------------------------------------------------ the move


def test_move_keeps_every_file_and_leaves_a_junction(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    ro_mtime = os.stat(_x(env.source / "readonly.txt")).st_mtime_ns

    outcome = env.move()

    assert outcome.journal.stage == Stage.DONE
    _assert_moved(env, expected)
    assert _manifest(str(env.source) + MOVED_SUFFIX) == expected
    assert _attrs(env.target / "readonly.txt") & stat.FILE_ATTRIBUTE_READONLY
    assert _attrs(env.target / "hidden.txt") & stat.FILE_ATTRIBUTE_HIDDEN
    assert os.stat(_x(env.target / "readonly.txt")).st_mtime_ns == ro_mtime
    stages = [step[1] for step in Journal.load(env.journal_path()).history]
    assert stages == [
        "planned",
        "copying",
        "copied",
        "freezing",
        "frozen",
        "syncing",
        "synced",
        "swapping",
        "done",
    ]


def test_an_edit_an_addition_and_a_deletion_before_the_freeze_reach_the_target(env: Env) -> None:
    _make_tree(env.source)

    def during(name: str) -> None:
        if name == "after_verify":
            _write(env.source / "sub" / "b.txt", b"edited after the copy")
            _write(env.source / "new" / "added.txt", b"added after the copy")
            os.remove(_x(env.source / "a.bin"))

    env.move(checkpoint=during)

    expected = _manifest(str(env.source) + MOVED_SUFFIX)
    assert "new\\added.txt" in expected and "a.bin" not in expected
    _assert_moved(env, expected)


def test_a_file_rewritten_while_the_copy_runs_is_recopied(env: Env) -> None:
    _write(env.source / "first.txt", b"v1")
    _write(env.source / "second.txt", b"other")
    calls: list[str] = []

    def during(name: str) -> None:
        calls.append(name)
        if name == "copy_file" and calls.count("copy_file") == 1:
            # Same size, rewritten inside the copy window: only the delta's window rule sees it.
            for leaf in ("first.txt", "second.txt"):
                _write(env.source / leaf, b"V2" if leaf == "first.txt" else b"OTHER")

    env.move(checkpoint=during)

    assert (env.target / "first.txt").read_bytes() == b"V2"
    assert (env.target / "second.txt").read_bytes() == b"OTHER"


@pytest.mark.parametrize(
    "point",
    [
        "copy_file",
        "after_copy",
        "after_verify",
        "after_freeze",
        "delta_file",
        "after_delta",
        "after_junction",
    ],
)
def test_a_kill_at_any_step_recovers_to_the_original_or_a_finished_move(
    env: Env, point: str
) -> None:
    _make_tree(env.source)

    def crash(name: str) -> None:
        if name == point:
            raise SimulatedCrash(name)

    def during(name: str) -> None:
        if name == "after_freeze":
            # Guarantees a delta file exists, so "delta_file" is reached.
            pass
        crash(name)

    if point == "delta_file":
        # Make the delta pass copy something: rewrite a file between verify and freeze.
        def edit_then_crash(name: str) -> None:
            if name == "after_verify":
                _write(env.source / "sub" / "b.txt", b"changed")
            crash(name)

        hook: Callable[[str], None] = edit_then_crash
    else:
        hook = during
    with pytest.raises(SimulatedCrash):
        env.move(checkpoint=hook)
    snapshot_dir = env.source if _exists(env.source) and not _is_link(env.source) else None
    moved = Path(str(env.source) + MOVED_SUFFIX)
    expected = _manifest(moved if _exists(moved) else (snapshot_dir or env.source))

    outcome = recover(env.journal_path())

    if outcome.journal.stage == Stage.DONE:
        assert point == "after_junction"
        _assert_moved(env, expected)
    else:
        assert outcome.journal.stage == Stage.ROLLED_BACK
        _assert_original(env, expected)


def test_a_bad_copy_is_caught_by_the_hash_check_and_rolled_back(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)

    def corrupt(name: str) -> None:
        if name == "after_copy":
            victim = env.target / "a.bin"
            st = os.stat(_x(env.source / "a.bin"))
            data = bytearray(victim.read_bytes())
            data[1000] ^= 0xFF
            _write(victim, bytes(data))  # same size, same timestamps, one flipped byte
            os.utime(_x(victim), ns=(st.st_atime_ns, st.st_mtime_ns))

    old = os.stat(_x(env.source / "a.bin")).st_mtime_ns - 10 * relocate._DELTA_WINDOW_NS
    os.utime(_x(env.source / "a.bin"), ns=(old, old))  # older than the copy window
    expected = _manifest(env.source)
    with pytest.raises(RelocateError, match="differ from their source"):
        env.move(checkpoint=corrupt)
    _assert_original(env, expected)


def test_an_ordinary_failure_rolls_back_before_returning(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)

    def fail(name: str) -> None:
        if name == "after_delta":
            raise OSError("disk went away")

    with pytest.raises(RelocateError, match="rolled back"):
        env.move(checkpoint=fail)
    _assert_original(env, expected)
    assert Journal.load(env.journal_path()).stage == Stage.ROLLED_BACK


def test_a_file_open_under_the_source_refuses_the_plan_and_the_move(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    with open(_x(env.source / "sub" / "b.txt"), "rb"):
        plan = plan_move(env.source, env.target, env.policy, free_bytes=lambda _p: BIG)
        assert not plan.ok
        assert any("in use" in reason for reason in plan.reasons)
        with pytest.raises(RelocateError, match="in use"):
            env.move()
    _assert_original(env, expected)


def test_the_plan_is_a_dry_run(env: Env) -> None:
    _make_tree(env.source)
    expected = _manifest(env.source)
    plan = plan_move(env.source, env.target, env.policy, free_bytes=lambda _p: BIG)
    assert plan.ok, plan.reasons
    assert plan.files == sum(1 for v in expected.values() if v != "<dir>")
    _assert_original(env, expected)
    assert not _exists(env.target.parent)
    assert not _exists(env.journals)


def test_a_link_appearing_after_the_plan_stops_the_copy(env: Env) -> None:
    _make_tree(env.source)
    elsewhere = env.c / "elsewhere"
    elsewhere.mkdir()

    def add_link(name: str) -> None:
        if name == "copy_file" and not _exists(env.source / "sub" / "zlink"):
            relocate._create_junction(str(env.source / "sub" / "zlink"), str(elsewhere))

    with pytest.raises(RelocateError, match="is a link"):
        env.move(checkpoint=add_link)
    assert not _is_link(env.source)
    assert not _exists(env.target)
    assert _is_link(env.source / "sub" / "zlink")


def test_long_paths_move_and_roll_back(env: Env) -> None:
    deep = Path(env.source, *(["d" * 60] * 5))
    _write(deep / ("f" * 50 + ".txt"), b"deep")
    long_target = Path(env.d, *(["t" * 60] * 4), "data")
    assert len(str(deep)) > 260 and len(str(long_target)) > 260
    env.target = long_target
    expected = _manifest(env.source)

    env.move()
    _assert_moved(env, expected)
    rollback(env.journal_path())
    _assert_original(env, expected)


def test_brackets_in_source_and_target_names(env: Env) -> None:
    env.source = env.c / "data [x]"
    env.target = env.d / "[target] (1)" / "data [x]"
    _make_tree(env.source)
    expected = _manifest(env.source)
    env.move()
    _assert_moved(env, expected)


def test_an_unreadable_file_fails_the_copy_and_rolls_back(env: Env) -> None:
    _make_tree(env.source)
    locked = env.source / "sub" / "locked.bin"
    _write(locked, b"secret")
    expected = _manifest(env.source)
    everyone = "*S-1-1-0"
    subprocess.run(
        ["icacls", str(locked), "/deny", f"{everyone}:(RD)"], check=True, capture_output=True
    )
    try:
        with pytest.raises(RelocateError, match="rolled back"):
            env.move()
    finally:
        subprocess.run(
            ["icacls", str(locked), "/remove:d", everyone], check=True, capture_output=True
        )
    _assert_original(env, expected)


# ------------------------------------------------------------------ rollback


def test_rollback_after_the_move_brings_back_changes_made_through_the_junction(
    env: Env,
) -> None:
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"written through the junction")
    _write(env.source / "brand-new.txt", b"created after the move")
    os.remove(_x(env.source / "a.bin"))
    after = _manifest(env.source)

    outcome = rollback(env.journal_path())

    assert outcome.journal.stage == Stage.ROLLED_BACK
    restored = _manifest(env.source)
    # Nothing in the old copy is deleted by a rollback: a.bin comes back, the rest is current.
    assert {k: v for k, v in restored.items() if k != "a.bin"} == after
    assert "a.bin" in restored
    assert not _is_link(env.source)
    assert not _exists(env.target)


def test_rollback_after_the_old_copy_was_deleted_copies_the_data_back(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True, free_bytes=lambda _p: BIG)
    _write(env.source / "late.txt", b"after delete")
    expected = _manifest(env.source)

    rollback(env.journal_path())

    _assert_original(env, expected)
    assert not _exists(str(env.source) + relocate.RESTORE_SUFFIX)


def test_rollback_refuses_while_the_moved_folder_is_in_use(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    with open(_x(env.source / "sub" / "b.txt"), "rb"), pytest.raises(InUseError):
        rollback(env.journal_path())
    assert _is_link(env.source)
    rollback(env.journal_path())
    assert not _is_link(env.source)


def test_rollback_refuses_a_junction_it_did_not_make(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    other = env.d / "other"
    other.mkdir()
    relocate._remove_junction(str(env.source))
    relocate._create_junction(str(env.source), str(other))
    with pytest.raises(RelocateError, match="not to"):
        rollback(env.journal_path())
    assert _is_link(env.source)


def test_a_kill_after_the_junction_is_removed_still_restores_later_writes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    _write(env.source / "sub" / "b.txt", b"written through the junction")
    expected = _manifest(env.source)
    real_sync = relocate._sync_back
    calls = {"n": 0}

    def die_once(target: str, moved: str) -> int:
        calls["n"] += 1
        if calls["n"] == 2:  # the first copy-back runs before the junction is removed
            raise SimulatedCrash("after the junction was removed")
        return real_sync(target, moved)

    monkeypatch.setattr(relocate, "_sync_back", die_once)
    with pytest.raises(SimulatedCrash):
        rollback(env.journal_path())
    assert not _exists(env.source)

    rollback(env.journal_path())

    restored = _manifest(env.source)
    assert restored == expected
    assert not _exists(env.target)


def test_a_kill_while_restoring_a_deleted_old_copy_resumes_from_the_restore(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    delete_old_copy(env.journal_path(), confirmed=True, free_bytes=lambda _p: BIG)
    _write(env.source / "late.txt", b"after delete")
    expected = _manifest(env.source)
    real_sync = relocate._sync_back
    calls = {"n": 0}

    def die_once(target: str, moved: str) -> int:
        calls["n"] += 1
        if calls["n"] == 2:  # the first copy-back runs before the junction is removed
            raise SimulatedCrash("after the junction was removed")
        return real_sync(target, moved)

    monkeypatch.setattr(relocate, "_sync_back", die_once)
    with pytest.raises(SimulatedCrash):
        rollback(env.journal_path())
    assert not _exists(env.source)
    assert _exists(str(env.source) + relocate.RESTORE_SUFFIX)

    rollback(env.journal_path())

    _assert_original(env, expected)
    assert not _exists(str(env.source) + relocate.RESTORE_SUFFIX)


def test_a_rollback_keeps_a_bad_target_copy_and_restores_the_old_bytes(env: Env) -> None:
    _make_tree(env.source)
    original = _manifest(env.source)
    env.move()
    victim = env.target / "sub" / "b.txt"
    st = os.stat(_x(victim))
    _write(victim, b"HELLO")  # same size and mtime, other bytes: a bad copy, not an edit
    os.utime(_x(victim), ns=(st.st_atime_ns, st.st_mtime_ns))

    outcome = rollback(env.journal_path())

    assert _manifest(env.source) == original
    assert outcome.kept and outcome.kept[0] == relocate._norm(env.target)
    assert "differ" in outcome.kept[1]
    assert (env.target / "sub" / "b.txt").read_bytes() == b"HELLO"


def test_a_failed_copy_back_after_the_junction_is_removed_puts_it_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    env.move()
    expected = _manifest(env.target)
    real_sync = relocate._sync_back
    calls = {"n": 0}

    def fail_second(target: str, moved: str) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RelocateError("a file vanished")
        return real_sync(target, moved)

    monkeypatch.setattr(relocate, "_sync_back", fail_second)
    with pytest.raises(RelocateError, match="vanished"):
        rollback(env.journal_path())
    _assert_moved(env, expected)


def test_the_plan_refuses_leftover_probe_and_restore_folders(env: Env) -> None:
    _make_tree(env.source)
    for suffix in (relocate.PROBE_SUFFIX, relocate.RESTORE_SUFFIX):
        leftover = str(env.source) + suffix
        os.makedirs(_x(leftover))
        plan = plan_move(env.source, env.target, env.policy, free_bytes=lambda _p: BIG)
        assert not plan.ok
        assert any("left by an earlier run" in r for r in plan.reasons)
        os.rmdir(_x(leftover))


# ------------------------------------------------------------------ delete the old copy


def test_the_old_copy_is_kept_for_seven_days_unless_confirmed(env: Env) -> None:
    _make_tree(env.source)
    now = [1_000_000.0]
    env.move(clock=lambda: now[0])
    with pytest.raises(RelocateError, match="kept for"):
        delete_old_copy(env.journal_path(), clock=lambda: now[0] + DAY)
    now[0] += KEEP_OLD_COPY_DAYS * DAY + 1
    outcome = delete_old_copy(env.journal_path(), clock=lambda: now[0], free_bytes=lambda _p: BIG)
    assert outcome.journal.stage == Stage.DELETED_OLD
    assert not _exists(str(env.source) + MOVED_SUFFIX)


def test_a_same_size_corrupted_target_refuses_the_delete(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    victim = env.target / "sub" / "b.txt"
    before = os.stat(_x(victim))
    _write(victim, b"HELLO")  # same size, different bytes
    os.utime(_x(victim), ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(RelocateError, match="differ"):
        delete_old_copy(env.journal_path(), confirmed=True)
    assert _exists(str(env.source) + MOVED_SUFFIX)


def test_a_file_changed_through_the_junction_after_the_move_does_not_block_the_delete(
    env: Env,
) -> None:
    _make_tree(env.source)
    env.move(clock=lambda: 1_000_000.0)
    _write(env.source / "sub" / "b.txt", b"updated by its program")
    os.utime(_x(env.source / "sub" / "b.txt"), (1_000_100.0, 1_000_100.0))
    delete_old_copy(env.journal_path(), confirmed=True, clock=lambda: 1_000_200.0)
    assert not _exists(str(env.source) + MOVED_SUFFIX)


def test_a_file_missing_from_the_target_needs_an_explicit_accept(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    os.remove(_x(env.target / "a.bin"))
    with pytest.raises(RelocateError, match="no longer in the moved folder"):
        delete_old_copy(env.journal_path(), confirmed=True)
    assert _exists(str(env.source) + MOVED_SUFFIX)
    delete_old_copy(env.journal_path(), confirmed=True, accept_missing=True)
    assert not _exists(str(env.source) + MOVED_SUFFIX)


def test_the_freed_figure_is_the_volume_free_space_delta(env: Env) -> None:
    _make_tree(env.source)
    readings = iter([BIG, 9_000])  # the target volume, then the source volume
    execute_move(
        env.source, env.target, env.policy, env.journals, free_bytes=lambda _p: next(readings)
    )
    outcome = delete_old_copy(env.journal_path(), confirmed=True, free_bytes=lambda _p: 12_345)
    assert outcome.journal.source_free_after == 12_345
    assert outcome.journal.source_free_before == 9_000
    assert outcome.journal.history[-1][2] == "volume free space changed by 3345 bytes"


def test_delete_refuses_unless_the_move_is_done(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    with pytest.raises(RelocateError, match="not done"):
        delete_old_copy(env.journal_path(), confirmed=True)


# ------------------------------------------------------------------ classification


def _hardlinked(root: Path) -> None:
    _write(root / "one.bin", b"shared")
    os.link(_x(root / "one.bin"), _x(root / "two.bin"))


def _offline(root: Path) -> None:
    _write(root / "cloud.bin", b"placeholder")
    relocate._set_attributes(_x(root / "cloud.bin"), 0)
    import ctypes

    ctypes.windll.kernel32.SetFileAttributesW(_x(root / "cloud.bin"), 0x1000)


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (lambda r: _write(r / ".venv" / "pyvenv.cfg", b"home ="), "Python environment"),
        (
            lambda r: _write(r / "deep" / "lib" / "site-packages" / "x.py", b""),
            "Python environment",
        ),
        (_hardlinked, "hardlinked files"),
        (_offline, "cloud placeholders"),
        (lambda r: os.makedirs(_x(r / "only-dirs")), "holds no files"),
    ],
)
def test_trees_that_cannot_move_safely_must_stay(
    env: Env, build: Callable[[Path], None], reason: str
) -> None:
    env.source.mkdir()
    _write(env.source / "keep.txt", b"x") if reason != "holds no files" else None
    build(env.source)
    found = classify(env.source, env.policy)
    assert found.verdict is Verdict.MUST_STAY
    assert any(reason in r for r in found.reasons), found.reasons


def test_a_tree_containing_a_link_must_stay(env: Env) -> None:
    _make_tree(env.source)
    relocate._create_junction(str(env.source / "link"), str(env.c))
    found = classify(env.source, env.policy)
    assert found.verdict is Verdict.MUST_STAY
    assert any("links" in r for r in found.reasons)


def test_an_excluded_project_is_refused_before_anything_is_read(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = env.c / "zzexclproj" / "data"
    _make_tree(inside)
    containing = env.c / "holder"
    _make_tree(containing / "x-zzexclproj-y")

    def no_scan(_root: str) -> None:
        raise AssertionError("an excluded tree was scanned")

    monkeypatch.setattr(relocate, "scan_tree", no_scan)
    for path in (inside, containing):
        found = classify(path, env.policy)
        assert found.verdict is Verdict.MUST_STAY
        assert found.reasons == ("it is, or contains, an excluded project",)
        assert found.files == 0
        plan = plan_move(path, env.d / "t", env.policy, free_bytes=lambda _p: BIG)
        assert not plan.ok


def test_the_plan_refuses_unsafe_targets_and_states(env: Env) -> None:
    _make_tree(env.source)

    def reasons(target: Path, free: int = BIG) -> str:
        plan = plan_move(env.source, target, env.policy, free_bytes=lambda _p: free)
        assert not plan.ok
        return " | ".join(plan.reasons)

    assert "same volume" in reasons(env.c / "elsewhere")
    assert "inside it" in reasons(env.source / "inner")
    assert "excluded project" in reasons(env.d / "zzexclproj" / "data")
    (env.d / "exists").mkdir()
    assert "already exists" in reasons(env.d / "exists")
    assert "GiB free" in reasons(env.target, free=10)
    os.makedirs(_x(str(env.source) + MOVED_SUFFIX))
    assert "left by an earlier move" in reasons(env.target)
    assert _manifest(env.source)  # untouched


def test_policy_lists_must_stay_with_their_reason(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    for path in (local / "uv" / "cache", local / "Docker" / "wsl", home / "proj"):
        _write(path / "f.bin", b"x")
    _write(local / "Packages" / "CanonicalGroupLimited.Ubuntu_x" / "LocalState" / "ext4.vhdx", b"x")
    vault = home / "Reclaim" / "data"
    _write(vault / "quarantine" / "f", b"x")
    policy = default_policy(
        Config(),
        environ={"USERPROFILE": str(home), "LOCALAPPDATA": str(local)},
        app_dirs=[vault],
    )
    assert classify(local / "uv" / "cache", policy).verdict is Verdict.MUST_STAY
    assert classify(local / "Docker", policy).verdict is Verdict.GUIDED
    ubuntu = local / "Packages" / "CanonicalGroupLimited.Ubuntu_x"
    assert classify(ubuntu, policy).verdict is Verdict.GUIDED
    assert classify(vault / "quarantine", policy).verdict is Verdict.MUST_STAY
    assert classify(home, policy).verdict is Verdict.MUST_STAY
    assert classify(local, policy).verdict is Verdict.MUST_STAY
    assert classify(home / "proj", policy).verdict is Verdict.MOVABLE


def test_an_already_moved_folder_is_reported_as_such(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    found = classify(env.source, env.policy)
    assert found.verdict is Verdict.ALREADY_MOVED
    assert relocate._key(found.link_target or "") == relocate._key(env.target)


def test_candidates_are_ranked_and_get_a_collision_free_target(env: Env) -> None:
    small, large = env.c / "small", env.c / "large"
    _write(small / "f", b"x" * 10)
    _write(large / "f", b"x" * 1000)
    _write(env.c / "venvish" / ".venv" / "pyvenv.cfg", b"")
    _write(env.c / "venvish" / "f", b"x" * 5000)
    paths = [small, env.c / "venvish", large, env.c / "absent"]
    found = propose_candidates(paths, env.policy, env.d)
    assert [Path(c.path).name for c in found] == ["large", "small", "venvish"]
    assert found[0].suggested_target == suggested_target(large, env.d)
    assert Path(found[0].suggested_target or "").is_relative_to(env.d / "reclaim-relocated")


def test_known_locations_are_under_the_profile(tmp_path: Path) -> None:
    home, local = tmp_path / "h", tmp_path / "h" / "AppData" / "Local"
    paths = known_locations({"USERPROFILE": str(home), "LOCALAPPDATA": str(local)})
    assert home / ".cache" / "huggingface" / "hub" in paths
    assert all(p.is_relative_to(home) for p in paths)


@pytest.fixture
def _no_stray_journals(env: Env) -> Iterator[None]:
    yield
    for path in env.journals.glob("*.tmp"):
        raise AssertionError(f"a journal write was left half-done: {path}")


@pytest.mark.usefixtures("_no_stray_journals")
def test_the_journal_is_written_atomically(env: Env) -> None:
    _make_tree(env.source)
    env.move()
    journal = Journal.load(env.journal_path())
    assert journal.source == relocate._norm(env.source)
    assert journal.target_created
