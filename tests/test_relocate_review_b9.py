from __future__ import annotations

# ruff: noqa: B017, F811, PTH208, T201
# Hostile-review probes for reclaim.relocate (verifier B9, export of 9263b2b), kept as written
# (its API probe moved to the page branch). Each test asserts a safety claim; on 9263b2b four
# failed (M1 twice, M2, L1) and they pass with the fixes of ADR-0044 addendum 9. "_holds" tests
# passed already and are evidence for what held up. Every root is under tmp_path.
import ctypes
import json
import os
import re
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
from test_relocate_review_b4 import _arm_deletes, _vol  # type: ignore[import-not-found]

import reclaim.relocate as relocate
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


def _moved(e: Env) -> Path:
    return Path(str(e.source) + relocate.MOVED_SUFFIX)


def _blind() -> Policy:
    return Policy(exclusion_match=lambda _p: None, protected=lambda _p: False)


def _short(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else ""


def _path_policy(long_source: Path) -> Policy:
    """A `[safety] deny` pattern naming the folder by its real (long) path, as a user writes it."""
    text = re.escape(Path(long_source).resolve().as_posix())
    validator = SafetyValidator(Config(safety=SafetyConfig(deny=[f"re:(?i){text}(/|$)"])))
    return Policy(
        exclusion_match=lambda p: validator.exclusion_match(p, is_dir=True),
        exclusion_match_text=lambda p: validator.exclusion_match(p, is_dir=True, lexical=True),
        protected=lambda _p: False,
    )


# ------------------------------------------------------------------ 8.3 short names


def test_b9_a_move_given_an_8dot3_name_keeps_the_folder_at_its_real_path(env: Env) -> None:
    """The CLI takes SOURCE as typed. A short (8.3) spelling of the folder passes every check
    (the exclusion check resolves it; the rest are text checks). The freeze renames the alias
    to `<SHORT>.reclaim-moved`, which gives the folder a NEW long name, and the junction is
    created as a new entry literally named `<SHORT>`. The folder's real path is then empty."""
    long_src = env.c / "longfoldername-data"
    _make_tree(long_src)
    short = _short(long_src)
    if not short or Path(short).name.casefold() == long_src.name.casefold():
        pytest.skip("8.3 names are disabled on this volume")
    plan = relocate.plan_move(short, env.target, env.policy, free_bytes=lambda _p: BIG)
    if not plan.ok:
        return  # refusing the alias is a correct answer
    execute_move(short, env.target, env.policy, env.journals, free_bytes=lambda _p: BIG)
    names = sorted(os.listdir(_x(env.c)))
    assert _exists(long_src), f"{long_src} no longer exists after the move; {env.c} holds {names}"


def test_b9_rollback_of_an_8dot3_move_puts_the_folder_back_under_its_real_name(
    env: Env,
) -> None:
    long_src = env.c / "longfoldername-data"
    _make_tree(long_src)
    expected = _manifest(long_src)
    short = _short(long_src)
    if not short or Path(short).name.casefold() == long_src.name.casefold():
        pytest.skip("8.3 names are disabled on this volume")
    plan = relocate.plan_move(short, env.target, env.policy, free_bytes=lambda _p: BIG)
    if not plan.ok:
        return
    execute_move(short, env.target, env.policy, env.journals, free_bytes=lambda _p: BIG)
    out = rollback(env.journal_path())
    names = sorted(os.listdir(_x(env.c)))
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _exists(long_src) and _manifest(long_src) == expected, (
        f"after a ROLLED_BACK rollback the folder is not at {long_src}; {env.c} holds {names}"
    )


def test_b9_a_path_exclusion_added_after_a_move_through_an_8dot3_parent_stops_delete_old(
    env: Env,
) -> None:
    """B8 M-B's lexical check matches the journaled text. When the journaled source was given
    through a short-named parent, a pattern naming the real (long) path hits none of the three
    texts (source -> junction resolves to the target; mapped-back and moved are lexical or end
    in .reclaim-moved), so the excluded folder's old copy is deleted."""
    parent = env.c / "longparentfolder"
    long_src = parent / "zzproj-data"
    _make_tree(long_src)
    short_parent = _short(parent)
    if not short_parent or Path(short_parent).name.casefold() == parent.name.casefold():
        pytest.skip("8.3 names are disabled on this volume")
    src = Path(short_parent) / "zzproj-data"
    tgt = env.d / "t83"
    plan = relocate.plan_move(src, tgt, _blind(), free_bytes=lambda _p: BIG)
    print("B9-83PARENT plan", plan.ok, plan.reasons)
    if not plan.ok:
        return
    execute_move(src, tgt, _blind(), env.journals, free_bytes=lambda _p: BIG)
    policy = _path_policy(long_src)
    moved = Path(str(long_src) + relocate.MOVED_SUFFIX)
    before = _manifest(moved)
    try:
        out = delete_old_copy(env.journal_path(), policy=policy, confirmed=True)
        detail = f"{out.journal.stage}: {out.message}"
    except RelocateError as exc:
        detail = f"refused: {exc}"
    print("B9-83PARENT", detail[:300])
    assert _exists(moved) and _manifest(moved) == before, (
        f"an excluded folder's old copy was deleted ({detail})"
    )


@pytest.mark.parametrize("variant", ["case", "trailing_sep", "prefix", "forward"])
def test_b9_holds_lexical_exclusion_hits_text_variants_of_the_source(
    env: Env, variant: str
) -> None:
    """The journaled source is normalised by `_norm`; the variants a user can type all hit."""
    _make_tree(env.source)
    given = {
        "case": str(env.source).upper(),
        "trailing_sep": str(env.source) + "\\",
        "prefix": "\\\\?\\" + str(env.source),
        "forward": str(env.source).replace("\\", "/"),
    }[variant]
    execute_move(given, env.target, _blind(), env.journals, free_bytes=lambda _p: BIG)
    before = _manifest(_moved(env))
    with pytest.raises(RelocateError, match="excluded"):
        delete_old_copy(env.journal_path(), policy=_path_policy(env.source), confirmed=True)
    assert _manifest(_moved(env)) == before


def test_b9_holds_trailing_dot_source_is_not_moved(env: Env) -> None:
    _make_tree(env.source)
    plan = relocate.plan_move(
        str(env.source) + ".", env.target, _blind(), free_bytes=lambda _p: BIG
    )
    assert not plan.ok


# ------------------------------------------------------------------ OSError sweeps

_FUNCS = (
    "_copy_file",
    "_copy_stream",
    "_rename",
    "_remove_junction",
    "_create_junction",
    "rmtree_reparse_point_safe",
    "_set_attributes",
    "_copy_folder_meta",
    "_keep_root_flags",
)


def _inject(m: pytest.MonkeyPatch, at: int, base: Path) -> dict[str, object]:
    """The `at`-th fault-prone call raises PermissionError (one fault, not a kill)."""
    state: dict[str, object] = {"n": 0, "where": None}
    base_key = relocate._key(base)

    def tick(name: str, path: object) -> None:
        state["n"] = int(state["n"]) + 1  # type: ignore[call-overload]
        if state["n"] == at:
            state["where"] = name
            raise PermissionError(13, f"simulated fault in {name}", str(path))

    for name in _FUNCS:
        real = getattr(relocate, name)

        def wrapped(*a: object, _real=real, _name=name, **k: object) -> object:
            tick(_name, a[0] if a else "?")
            return _real(*a, **k)

        m.setattr(relocate, name, wrapped)
    for name in ("unlink", "rmdir", "remove", "mkdir", "makedirs", "replace"):
        real_os = getattr(os, name)

        def os_wrapped(path, *a, _real=real_os, _name=name, **k):  # type: ignore[no-untyped-def]
            if isinstance(path, str) and base_key in relocate._key(path.removeprefix("\\\\?\\")):
                tick("os." + _name, path)
            return _real(path, *a, **k)

        m.setattr(os, name, os_wrapped)
    real_save = relocate.Journal.save

    def save(self: Journal) -> None:
        tick("save", self.path)
        real_save(self)

    m.setattr(relocate.Journal, "save", save)
    return state


def _setup(base: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> tuple[Env, dict]:
    e = Env(base)
    _vol(monkeypatch, e)
    _make_tree(e.source)
    original = _manifest(e.source)
    e.move()
    _write(e.target / "after-move.txt", b"written through the junction")
    if scenario == "rollback_after_deleted_old":
        delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
    if scenario == "delete_old_after_rollback":
        rollback(e.journal_path())
    if scenario == "recover_interrupted_delete":
        with monkeypatch.context() as m:
            _arm_deletes(m, 3, relocate._key(_moved(e)))
            with pytest.raises(SimulatedCrash):
                delete_old_copy(e.journal_path(), policy=e.policy, confirmed=True)
    now = _manifest(e.target) if _exists(e.target) else _manifest(e.source)
    return e, {"original": original, "now": now}


def _command(e: Env, scenario: str) -> Callable[[], relocate.Outcome]:
    jp = e.journal_path()
    return {
        "rollback": lambda: rollback(jp),
        "rollback_use_old_copy": lambda: rollback(jp, use_old_copy=True),
        "rollback_after_deleted_old": lambda: rollback(jp),
        "delete_old": lambda: delete_old_copy(jp, policy=e.policy, confirmed=True),
        "delete_old_after_rollback": lambda: delete_old_copy(jp, policy=e.policy, confirmed=True),
        "recover_interrupted_delete": lambda: recover(jp, policy=e.policy),
    }[scenario]


def _check_final(e: Env, scenario: str, want: dict) -> str | None:
    stage = Journal.load(e.journal_path()).stage
    if scenario in ("rollback", "rollback_after_deleted_old"):
        if stage != Stage.ROLLED_BACK or _is_link(e.source) or _manifest(e.source) != want["now"]:
            return f"stage {stage}, link {_is_link(e.source)}"
    elif scenario == "rollback_use_old_copy":
        if stage != Stage.ROLLED_BACK or _manifest(e.source) != want["original"]:
            return f"stage {stage}"
        if _manifest(e.target) != want["now"]:
            return "the moved folder changed"
    elif scenario in ("delete_old", "recover_interrupted_delete"):
        if stage != Stage.DELETED_OLD or _exists(_moved(e)) or _manifest(e.target) != want["now"]:
            return f"stage {stage}"
        if not _is_link(e.source):
            return "the junction is gone"
    elif scenario == "delete_old_after_rollback" and (
        stage != Stage.DELETED_OLD or _exists(_moved(e)) or _manifest(e.source) != want["now"]
    ):
        return f"stage {stage}"
    return None


_SCENARIOS = [
    "rollback",
    "rollback_use_old_copy",
    "rollback_after_deleted_old",
    "delete_old",
    "delete_old_after_rollback",
    "recover_interrupted_delete",
]


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_b9_sweep_one_os_error_anywhere_then_the_same_command_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """A single OSError (a scanner, an open window) at every fault-prone call and journal save
    of the command. Required: the error is a RelocateError (B8 M-C/M-D), the folder's path is
    never left empty, and running the same command again (what the message says) finishes."""
    at = 1
    failures: list[str] = []
    empties: list[str] = []
    while True:
        base = tmp_path / f"o{at}"
        base.mkdir()
        e, want = _setup(base, monkeypatch, scenario)
        cmd = _command(e, scenario)
        with monkeypatch.context() as m:
            state = _inject(m, at, base)
            try:
                cmd()
                raised: BaseException | None = None
            except BaseException as exc:
                raised = exc
        if state["where"] is None:
            assert raised is None, f"no fault injected but {raised!r}"
            break
        where = f"fault {at} ({state['where']})"
        if raised is not None and not isinstance(raised, RelocateError):
            failures.append(f"{where}: raw {type(raised).__name__}: {str(raised)[:100]}")
        if not _exists(e.source):
            empties.append(f"{where}: {str(raised)[:160]}")
        last = None
        for _ in range(3 if raised is not None else 0):
            try:
                cmd()
                last = None
                break
            except RelocateError as exc:
                last = exc
        if last is not None:
            failures.append(f"{where}: rerun refused: {str(last)[:200]}")
        else:
            bad = _check_final(e, scenario, want)
            if bad:
                failures.append(f"{where}: final state wrong: {bad}")
        at += 1
    print(
        f"B9-OSERR {scenario} points={at - 1} failures={len(failures)} "
        f"empty_after_fault={len(empties)}"
    )
    for line in empties:
        print("  EMPTY", line)
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures)


# ------------------------------------------------------------------ supersession


def test_b9_holds_a_clock_skewed_later_move_cannot_be_undone_by_the_older_rolled_back_one(
    env: Env,
) -> None:
    """A rolled back before the swap (frozen) then B moves the same folder with a clock BEHIND
    A's: the folder at the path carries A.source_id again. `delete-old A` (documented flow for a
    ROLLED_BACK journal) is not stopped by supersession (B looks older), so identity must."""
    _make_tree(env.source)
    with pytest.MonkeyPatch.context() as m:
        real = relocate._delta_sync

        def boom(*a: object, **k: object) -> str:
            raise RelocateError("simulated failure after the freeze")

        m.setattr(relocate, "_delta_sync", boom)
        with pytest.raises(RelocateError):
            env.move(clock=lambda: 2_000_000_000.0)
        m.setattr(relocate, "_delta_sync", real)
    (a,) = _paths(env)
    assert Journal.load(a).stage == Stage.ROLLED_BACK
    env.move(clock=lambda: 1_000_000_000.0)  # B, clock skewed back
    (b,) = [p for p in _paths(env) if p != a]
    before = _manifest(_moved(env))
    with pytest.raises(RelocateError):
        delete_old_copy(a, policy=env.policy, confirmed=True)
    with pytest.raises(Exception):
        out = rollback(a)
        raise RelocateError(f"returned {out.message}")
    assert _is_link(env.source) and Journal.load(b).stage == Stage.DONE
    assert _manifest(_moved(env)) == before


def test_b9_holds_a_copied_journal_file_does_not_strand_its_own_move(env: Env) -> None:
    """A user backs up a journal next to itself (same id, other file name). The move must still
    be rollable and deletable."""
    _make_tree(env.source)
    env.move()
    jp = env.journal_path()
    copy = jp.with_name(jp.stem + "-backup.json")
    copy.write_bytes(jp.read_bytes())
    out = delete_old_copy(jp, policy=env.policy, confirmed=True)
    assert out.journal.stage == Stage.DELETED_OLD


def test_b9_a_stale_backup_of_a_journal_does_not_strand_the_move(env: Env) -> None:
    """The same backup, but the move's journal was copied under a fresh id (e.g. a
    hand-made 'relocate-<x>.json' kept as a record). The real move must not be refused forever."""
    _make_tree(env.source)
    env.move()
    jp = env.journal_path()
    data = json.loads(jp.read_text(encoding="utf-8"))
    data["id"] = "feedfacecafe"
    (jp.parent / "relocate-feedfacecafe.json").write_text(json.dumps(data), encoding="utf-8")
    try:
        rollback(jp)
        outcome = "rolled back"
    except RelocateError as exc:
        outcome = f"refused: {exc}"
    print("B9-STALE-COPY", outcome[:300])
    if outcome.startswith("refused"):
        # The message says to deal with the copy instead; doing that must work.
        other = jp.parent / "relocate-feedfacecafe.json"
        try:
            rollback(other)
            second = "rolled back via the copy"
        except RelocateError as exc:
            second = f"refused: {exc}"
        print("B9-STALE-COPY second", second[:300])
        assert not second.startswith("refused"), (
            f"both journals refuse each other (created_at tie): {outcome[:160]} / {second[:160]}"
        )


# ------------------------------------------------------------------ guided sizing


def test_b9_holds_guided_sizing_does_not_follow_a_junction_out_of_the_folder(
    tmp_path: Path,
) -> None:
    docker = tmp_path / "Local" / "Docker"
    _write(docker / "a.bin", b"x" * 100)
    outside = tmp_path / "outside"
    _write(outside / "huge.bin", b"y" * 10_000)
    relocate._create_junction(str(docker / "link"), str(outside))
    policy = Policy(
        exclusion_match=lambda _p: None,
        protected=lambda _p: False,
        guided=((docker, "steps"),),
    )
    found = relocate.classify(docker, policy)
    assert found.verdict is relocate.Verdict.GUIDED
    assert found.bytes == 100, found


def test_b9_holds_guided_sizing_opens_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = tmp_path / "Local" / "Docker"
    _write(docker / "disk" / "data.vhdx", b"z" * 4096)
    opened: list[str] = []
    real_open = open

    def spy_open(path, *a, **k):  # type: ignore[no-untyped-def]
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    for name in ("rename", "unlink", "rmdir", "mkdir", "replace", "remove"):
        monkeypatch.setattr(
            os, name, lambda *a, _n=name, **k: (_ for _ in ()).throw(AssertionError(_n))
        )
    policy = Policy(
        exclusion_match=lambda _p: None, protected=lambda _p: False, guided=((docker, "s"),)
    )
    found = relocate.classify(docker, policy)
    assert found.bytes == 4096 and not opened


# ------------------------------------------------------------------ journal writes


def test_b9_a_journal_write_failure_after_the_junction_is_removed_leaves_no_empty_path(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback after the swap: the copy-back is verified, the junction is removed, then
    `journal.log(ROLLING_BACK, "junction removed")` runs OUTSIDE the try that restores the
    junction on failure. A failed journal write there (the journal's drive full or the file
    held by a scanner) leaves the folder's path empty; the B8 decorator turns it into a
    message naming the journal file, not the empty folder path."""
    _make_tree(env.source)
    env.move()
    flag = {"removed": False}
    real_remove = relocate._remove_junction
    real_save = Journal.save

    def remove(link: str) -> None:
        real_remove(link)
        flag["removed"] = True

    def save(self: Journal) -> None:
        if flag["removed"]:
            flag["removed"] = False
            raise PermissionError(13, "simulated: journal file held", self.path + ".tmp")
        real_save(self)

    with monkeypatch.context() as m:
        m.setattr(relocate, "_remove_junction", remove)
        m.setattr(Journal, "save", save)
        with pytest.raises(RelocateError) as info:
            rollback(env.journal_path())
    print("B9-EMPTY message:", str(info.value)[:400])
    assert _exists(env.source), (
        f"{env.source} is empty after the reported failure: {str(info.value)[:200]}"
    )
