from __future__ import annotations

# ruff: noqa: F811, T201, PTH100, PTH103, PTH104, PTH112, PTH208
# Hostile-review probes (verifier B12, export of 240078c) for ADR-0044 addendum 11:
# `_final_name`, `_bad_text`, the probe-leftover spelling, the try around the first journal
# writes, and a one-OSError sweep over plan + move + rollback. "_holds" tests assert a claim
# expected to hold; the others assert the claim the reviewer expected to break. Every root is
# under tmp_path; zzexclproj stands in for an excluded project. Kept in the repo with three
# changes, each marked where it is made: its two ACL probes are removed (they ran `icacls
# /deny`, and an earlier run of them left folders whose deny needs elevation to remove, so a
# suite that runs on every build changes no ACLs; both held for the reviewer); the long-name
# probes assert addendum 12's refusal at the plan instead of a rollback after a move.
import contextlib
import ctypes
import os
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
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_INVALID = wintypes.HANDLE(-1).value


def _plan(src: object, tgt: object, e: Env, *, run_probe: bool = True) -> relocate.Plan:
    return relocate.plan_move(
        src,  # type: ignore[arg-type]
        tgt,  # type: ignore[arg-type]
        e.policy,
        free_bytes=lambda _p: BIG,
        journal_dir=e.journals,
        run_probe=run_probe,
    )


def _move(src: object, tgt: object, e: Env) -> relocate.Outcome:
    return execute_move(src, tgt, e.policy, e.journals, free_bytes=lambda _p: BIG)  # type: ignore[arg-type]


def _names(folder: Path) -> list[str]:
    return sorted(os.listdir(_x(folder)))


def _handles() -> int:
    n = wintypes.DWORD(0)
    assert _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n))
    return int(n.value)


@contextlib.contextmanager
def _held_exclusively(path: Path):  # type: ignore[no-untyped-def]
    """Opens the folder for read with NO sharing (as a program holding it would)."""
    h = _k32.CreateFileW(_x(path), 0x80000000, 0, None, 3, 0x02000000, None)
    assert h not in (None, _INVALID), ctypes.get_last_error()
    try:
        yield
    finally:
        _k32.CloseHandle(h)


# ------------------------------------------------------------------ 1. _final_name


def test_b12_holds_final_name_reads_a_folder_held_without_sharing(env: Env) -> None:
    _make_tree(env.source)
    with _held_exclusively(env.source):
        name = relocate._final_name(str(env.source))
        spelled = relocate._as_spelled_on_disk(relocate._norm(env.source))
    print("B12-HELD", name, spelled)
    assert name is not None and relocate._key(name) == relocate._key(env.source)
    assert spelled[1] is None


def test_b12_holds_final_name_leaks_no_handle(env: Env) -> None:
    _make_tree(env.source)
    missing = env.c / "missing"
    for p in (env.source, missing, env.c / "a" / "b"):
        relocate._final_name(str(p))
    before = _handles()
    for _ in range(500):
        relocate._final_name(str(env.source))
        relocate._final_name(str(missing))
    with _held_exclusively(env.source):
        for _ in range(100):
            relocate._final_name(str(env.source))
    after = _handles()
    print("B12-HANDLES", before, after)
    assert after - before < 20


def test_b12_holds_lowercase_drive_and_long_path_spell_as_typed(env: Env) -> None:
    deep = env.c / "\\".join(["segment-" + "y" * 50] * 6) / "data"
    _make_tree(deep)
    typed = str(deep)
    typed = typed[0].lower() + typed[1:]
    spelled, reason = relocate._as_spelled_on_disk(relocate._norm(typed))
    print("B12-LONG", len(typed), reason, spelled[:12])
    assert (
        reason is None and spelled[0].isupper() and relocate._key(spelled) == relocate._key(typed)
    )
    plan = _plan(typed, env.target, env)
    print("B12-LONG plan", plan.ok, plan.reasons)
    assert plan.ok, plan.reasons


def test_b12_an_existing_source_with_a_trailing_space_is_refused_with_a_way_forward(
    env: Env,
) -> None:
    """A folder `data ` exists (made with `\\\\?\\`). `_bad_text` refuses it as non-creatable;
    is the refusal actionable (it says to rename?)"""
    src = env.c / "data "
    os.makedirs(_x(env.c) + "\\data \\sub")
    _write(Path(_x(env.c) + "\\data \\sub\\f.txt"), b"x")
    final = relocate._final_name(_x(env.c) + "\\data ")
    plan = _plan(_x(env.c) + "\\data ", env.target, env)
    print("B12-TRAILSPACE final", repr(final), "| reasons", plan.reasons)
    assert not plan.ok
    assert any("rename" in r.casefold() for r in plan.reasons), plan.reasons
    assert _exists(src)


# ------------------------------------------------------------------ 2. _bad_text


@pytest.mark.parametrize(
    "leaf",
    # 239 is the longest name that leaves room for `.reclaim-restore` (addendum 12); the
    # reviewer had 255 here, which addendum 12 refuses on purpose (see below).
    [" leading", "ünïcode-名前", "x" * 239, "a.b.c", "COM¹x"],
)
def test_b12_holds_legitimate_target_names_pass_and_round_trip(env: Env, leaf: str) -> None:
    _make_tree(env.source)
    before = _manifest(env.source)
    tgt = env.d / "relocated" / leaf
    reason = relocate._bad_text(relocate._norm(tgt))
    print("B12-GOODNAME", leaf[:20], len(leaf), reason)
    assert reason is None
    plan = _plan(env.source, tgt, env)
    assert plan.ok, plan.reasons
    _move(env.source, tgt, env)
    assert _is_link(env.source) and _manifest(env.source) == before


_UNCREATABLE = {
    "emoji_256_units": "\U0001f600" * 128,  # len 128, 256 UTF-16 units: over NTFS's 255
    "emoji_mixed": "a" * 200 + "\U0001f600" * 30,  # len 230, 260 units
}


@pytest.mark.parametrize("kind", sorted(_UNCREATABLE))
def test_b12_a_name_over_255_utf16_units_is_refused_or_finishable(env: Env, kind: str) -> None:
    """`_bad_text` counts code points (`len`), NTFS counts UTF-16 units. A name of 128 emoji is
    128 long to Python and 256 to NTFS. Required, as for B11 M1: refused by the plan, or the
    failed move's journal can be finished and the folder moved normally."""
    _make_tree(env.source)
    before = _manifest(env.source)
    given = env.d / "relocated" / _UNCREATABLE[kind]
    pre = _plan(env.source, given, env)
    print("B12-UTF16", kind, "plan ok:", pre.ok, pre.reasons[:2])
    if not pre.ok:
        return
    with pytest.raises(RelocateError) as info:
        _move(env.source, given, env)
    print("B12-UTF16 move:", str(info.value)[:400])
    jp = env.journal_path()
    outcomes = []
    for name, cmd in (
        ("recover", lambda: recover(jp, policy=env.policy)),
        ("rollback", lambda: rollback(jp)),
    ):
        try:
            outcomes.append(f"{name}: {cmd().message[:200]}")
        except RelocateError as exc:
            outcomes.append(f"{name}: refused ...{str(exc)[-220:]!a}")
    print("B12-UTF16 then", outcomes, Journal.load(jp).stage)
    assert not _is_link(env.source) and _manifest(env.source) == before
    plan = _plan(env.source, env.target, env)
    print("B12-UTF16 new plan", plan.ok, plan.reasons)
    assert Journal.load(jp).stage == Stage.ROLLED_BACK, "the failed move's journal is stuck"
    assert plan.ok, "the folder can no longer be moved"
    assert pre.ok is False, "the dry run accepted a name NTFS cannot create"


@pytest.mark.parametrize("leaf", ["CON", "NUL", "COM1", "con.txt", "AUX.data", "LPT1"])
def test_b12_a_reserved_device_name_target_is_refused(env: Env, leaf: str) -> None:
    """Windows' own rules: CON, NUL, COM1... (and, before Windows 11, `CON.txt`) cannot be a
    name. Through `\\\\?\\` NTFS creates them anyway, and Explorer and most programs then cannot
    open or delete that folder. Observed: what the plan says, and whether a move and rollback
    go through."""
    _make_tree(env.source)
    before = _manifest(env.source)
    tgt = env.d / "relocated" / leaf
    plan = _plan(env.source, tgt, env)
    print("B12-RESERVED", leaf, "plan", plan.ok, plan.reasons[:2])
    moved = None
    if plan.ok:
        try:
            _move(env.source, tgt, env)
            moved = "moved"
            plain = os.path.isdir(str(tgt))
            print(
                "B12-RESERVED", leaf, "plain isdir:", plain, "abspath:", os.path.abspath(str(tgt))
            )
            out = rollback(env.journal_path())
            moved += f"; rollback: {out.message[:120]}"
        except RelocateError as exc:
            moved = f"refused/failed: {str(exc)[:300]}"
        print("B12-RESERVED", leaf, moved)
    assert not _is_link(env.source) and _manifest(env.source) == before
    assert not plan.ok, f"{tgt} was accepted"


# ------------------------------------------------------------------ 2b. derived working names


# The reviewer's three probes here (a 250-character target, its ways forward, a 240-character
# source) moved such a folder and found every rollback refused with a wrong reason. Addendum 12
# refuses those names at the plan instead, so they are replaced by the test below; the 239
# case above shows the longest accepted name still moves and comes back.


@pytest.mark.parametrize("length", [240, 250, 255])
@pytest.mark.parametrize("side", ["source", "target"])
def test_b12_a_name_without_room_for_the_working_copies_is_refused_by_the_plan(
    env: Env, side: str, length: int
) -> None:
    src = env.c / ("s" * length) if side == "source" else env.source
    tgt = env.d / "relocated" / ("t" * length) if side == "target" else env.target
    _make_tree(src)
    before = _manifest(src)
    plan = _plan(src, tgt, env)
    assert not plan.ok
    assert any("too long to move safely" in r for r in plan.reasons), plan.reasons
    assert not any("in use" in r for r in plan.reasons), plan.reasons
    with pytest.raises(RelocateError, match="too long to move safely"):
        _move(src, tgt, env)
    assert _manifest(src) == before and not _is_link(src)
    assert not list(env.journals.glob("relocate-*.json"))


def test_b12_the_longest_accepted_source_name_moves_and_rolls_back(env: Env) -> None:
    src = env.c / ("s" * 239)
    _make_tree(src)
    before = _manifest(src)
    _move(src, env.target, env)
    out = rollback(env.journal_path())
    assert out.journal.stage == Stage.ROLLED_BACK
    assert _manifest(src) == before


@pytest.mark.parametrize("length", [240, 245])
def test_b12_a_source_name_near_255_is_refused_with_its_real_reason(env: Env, length: int) -> None:
    """`.reclaim-probe`/`-moved` add 14 characters and `.reclaim-restore` 16. A source leaf of
    242..255 cannot be probed: the plan says the folder is "in use", for good. A leaf of 240 or
    241 moves, but the rollback's restore folder (256+) can never be made."""
    src = env.c / ("s" * length)
    _make_tree(src)
    before = _manifest(src)
    plan = _plan(src, env.target, env)
    print("B12-SRCLEN", length, "plan", plan.ok, plan.reasons)
    if not plan.ok:
        assert not any("in use" in r for r in plan.reasons), plan.reasons
        return
    _move(src, env.target, env)
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    results = []
    for attempt in range(2):
        try:
            out = rollback(env.journal_path())
            results.append(f"ok: {out.message[:120]}")
            break
        except RelocateError as exc:
            results.append(f"{attempt}: {str(exc)[:260]}")
    print("B12-SRCLEN", length, results, Journal.load(env.journal_path()).stage)
    assert _is_link(src) or _exists(src)
    assert Journal.load(env.journal_path()).stage == Stage.ROLLED_BACK, results
    assert _manifest(src) == before


# ------------------------------------------------------------------ 3. probe-leftover spelling


def test_b12_holds_leftover_in_another_case_is_restored_in_its_own_case(env: Env) -> None:
    _make_tree(env.c / "Data")
    before = _manifest(env.c / "Data")
    os.rename(_x(env.c / "Data"), _x(env.c / ("Data" + relocate.PROBE_SUFFIX)))
    plan = _plan(env.c / "data", env.target, env)
    print("B12-LEFT-CASE", plan.source, _names(env.c), plan.reasons)
    assert _names(env.c) == ["Data"] and _manifest(env.c / "Data") == before


def test_b12_holds_folder_and_other_case_leftover_both_exist(env: Env) -> None:
    _make_tree(env.source)
    _write(env.c / ("DATA" + relocate.PROBE_SUFFIX) / "other.txt", b"other")
    plan = _plan(env.source, env.target, env)
    print("B12-LEFT-BOTH", plan.ok, plan.reasons, _names(env.c))
    assert not plan.ok and _names(env.c) == ["DATA.reclaim-probe", "data"]


def test_b12_holds_a_leftover_that_is_a_junction_is_not_renamed(env: Env) -> None:
    elsewhere = env.d / "elsewhere"
    _make_tree(elsewhere)
    before = _manifest(elsewhere)
    relocate._create_junction(str(env.c / ("data" + relocate.PROBE_SUFFIX)), str(elsewhere))
    plan = _plan(str(env.source).upper(), env.target, env)
    print("B12-LEFT-JUNC", plan.ok, plan.source, plan.reasons, _names(env.c))
    assert not plan.ok
    assert _names(env.c) == ["data.reclaim-probe"] and _is_link(env.c / "data.reclaim-probe")
    assert _manifest(elsewhere) == before


def test_b12_a_leftover_reached_through_a_junction_parent_is_not_renamed(env: Env) -> None:
    """`C\\via` is a junction to `C\\real`; `C\\real\\data.reclaim-probe` is a leftover. Typed
    `C\\via\\data`: the plan refuses (a link on the way) but the probe restore runs first, on
    the typed text. Required: the plan changes nothing reached through a link."""
    real = env.c / "real"
    _make_tree(real / ("data" + relocate.PROBE_SUFFIX))
    relocate._create_junction(str(env.c / "via"), str(real))
    plan = _plan(env.c / "via" / "data", env.target, env)
    print("B12-LEFT-VIA", plan.ok, plan.source, plan.reasons[:3], _names(real))
    assert not plan.ok
    assert _names(real) == ["data.reclaim-probe"], f"renamed through a link: {_names(real)}"


def test_b12_holds_an_excluded_leftover_through_a_junction_is_not_renamed(env: Env) -> None:
    real = env.c / "zzexclproj"
    _make_tree(real / ("data" + relocate.PROBE_SUFFIX))
    relocate._create_junction(str(env.c / "via"), str(real))
    plan = _plan(env.c / "via" / "data", env.target, env)
    print("B12-LEFT-EXCL", plan.ok, plan.reasons[:3], _names(real))
    assert not plan.ok
    assert _names(real) == ["data.reclaim-probe"], (
        f"renamed inside an excluded project: {_names(real)}"
    )


# ------------------------------------------------------------------ 4. first journal writes


@pytest.mark.parametrize("fail_at", ["planned_replace", "probing_open", "probing_replace"])
def test_b12_first_journal_write_failure_then_the_named_rollback_clears_it(
    env: Env, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    _make_tree(env.source)
    before = _manifest(env.source)
    real_save = relocate.Journal.save
    real_replace = os.replace
    count = {"save": 0}

    def save(self: Journal) -> None:
        count["save"] += 1
        if fail_at == "probing_open" and count["save"] == 2:
            raise PermissionError(13, "simulated: journal folder refused", self.path + ".tmp")
        real_save(self)

    def replace(a, b, *args, **kw):  # type: ignore[no-untyped-def]
        n = count["save"]
        if str(b).endswith(".json") and (
            (fail_at == "planned_replace" and n == 1) or (fail_at == "probing_replace" and n == 2)
        ):
            raise PermissionError(13, "simulated: replace refused", str(b))
        return real_replace(a, b, *args, **kw)

    with monkeypatch.context() as m:
        m.setattr(relocate.Journal, "save", save)
        m.setattr(os, "replace", replace)
        with pytest.raises(RelocateError) as info:
            _move(env.source, env.target, env)
    text = str(info.value)
    print("B12-JW", fail_at, text[:400])
    print("B12-JW files", _names(env.journals) if env.journals.exists() else None)
    assert _names(env.c) == ["data"] and _manifest(env.source) == before
    written = sorted(env.journals.glob("relocate-*.json"))
    if written:
        (jp,) = written
        assert str(jp) in text and "rollback" in text
        move_id = jp.stem.removeprefix("relocate-")
        assert f"rollback {move_id}" in text
        blocked = _plan(env.source, env.target, env)
        print("B12-JW plan before rollback", blocked.ok, blocked.reasons)
        out = rollback(jp)
        print("B12-JW rollback", out.message, out.journal.stage)
        assert out.journal.stage == Stage.ROLLED_BACK
    plan = _plan(env.source, env.target, env)
    print("B12-JW plan after", plan.ok, plan.reasons)
    assert plan.ok, plan.reasons
    _move(env.source, env.target, env)
    assert _is_link(env.source) and _manifest(env.source) == before


# ------------------------------------------------------------------ 5. one-OSError sweep


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
    else:  # long_tail: a target over 260 characters, mostly still to be made
        tail = "\\".join(["seg-" + "z" * 40] * 6)
        src, tgt = str(e.source), str(e.d / "Relocated" / tail / "data")
    return e, {"original": original, "src": src, "tgt": tgt}


def _sequence(e: Env, want: dict, log: list[str]) -> None:
    plan = _plan(want["src"], want["tgt"], e)
    log.append(f"plan ok={plan.ok}")
    _move(want["src"], want["tgt"], e)
    log.append("moved")
    (jp,) = [p for p in e.journals.glob("relocate-*.json") if Journal.load(p).stage == Stage.DONE]
    rollback(jp)
    log.append("rolled back")


def _settle(e: Env, want: dict) -> str | None:
    """Follow what the messages say until the folder is back and no journal is live."""
    last = ""
    for _ in range(4):
        live = False
        for jp in sorted(e.journals.glob("relocate-*.json")) if e.journals.exists() else []:
            j = Journal.load(jp)
            if j.stage not in {Stage.DONE, Stage.DELETED_OLD, Stage.ROLLED_BACK}:
                live = True
                try:
                    recover(jp, policy=e.policy)
                except RelocateError as exc:
                    last = f"recover refused: {str(exc)[:160]}"
            elif j.stage == Stage.DONE:
                live = True
                try:
                    rollback(jp)
                except RelocateError as exc:
                    last = f"rollback refused: {str(exc)[:160]}"
        if not live and _exists(e.source) and not _is_link(e.source):
            return None
    return last or "did not settle"


@pytest.mark.parametrize("scenario", ["normal", "case_variant", "long_tail"])
def test_b12_sweep_one_os_error_in_plan_move_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    at = 1
    failures: list[str] = []
    empties: list[str] = []
    while True:
        base = tmp_path / f"o{at}"
        base.mkdir()
        e, want = _sweep_setup(base, monkeypatch, scenario)
        log: list[str] = []
        with monkeypatch.context() as m:
            state = _inject(m, at, base)
            try:
                _sequence(e, want, log)
                raised: BaseException | None = None
            except BaseException as exc:
                raised = exc
        where = f"fault {at} ({state['where']}, after {log[-1] if log else 'nothing'})"
        if _is_link(e.source) and not os.path.isdir(_x(e.source)):
            empties.append(f"{where}: DANGLING junction: {str(raised)[:220]}")
            rerun = _settle(e, want)
            if rerun or not _exists(e.source) or _manifest(e.source) != want["original"]:
                failures.append(f"{where}: dangling junction not repaired: {rerun}")
            at += 1
            continue
        if not _exists(e.source):
            empties.append(f"{where}: {str(raised)[:160]}")
        if state["where"] is None:
            assert raised is None, f"no fault injected but {raised!r}"
            break
        if raised is not None and not isinstance(raised, RelocateError):
            failures.append(f"{where}: raw {type(raised).__name__}: {str(raised)[:120]}")
        if _exists(e.source) and not _is_link(e.source) and _manifest(e.source) != want["original"]:
            failures.append(f"{where}: the folder's content changed")
        if _is_link(e.source) and _manifest(e.source) != want["original"]:
            failures.append(f"{where}: the junction shows other content")
        bad = _settle(e, want)
        if bad:
            failures.append(f"{where}: {bad}")
        if _is_link(e.source) or not _exists(e.source) or _manifest(e.source) != want["original"]:
            failures.append(f"{where}: final state: link {_is_link(e.source)}")
        names = sorted(n.casefold() for n in _names(e.c))
        if names not in (["data"], ["data", "data.reclaim-moved"]) or "data" not in _names(e.c):
            failures.append(f"{where}: C holds {_names(e.c)}")
        if _names(e.d) != ["Relocated"]:
            failures.append(f"{where}: D holds {_names(e.d)}")
        if _names(e.d / "Relocated"):
            # Two leftovers are accepted on purpose (ADR-0044 addendum 12), and only these:
            # (L4) a fault in an unlink while our target copy was being removed: the rerun keeps
            # what is left and its journal says so ("kept ..."), since a copy that no longer
            # compares equal is never deleted; (rmdir) a fault removing the empty parent folders
            # this move made: empty folders only, no file.
            history = Journal.load(e.journal_path()).history
            note = str(history[-1][2]) if history else ""
            files = [f for _, _, fs in os.walk(_x(e.d / "Relocated")) for f in fs]
            known = (state["where"] == "os.unlink" and "; kept " in note) or (
                state["where"] == "os.rmdir" and not files
            )
            if not known:
                failures.append(f"{where}: D\\Relocated holds {_names(e.d / 'Relocated')}")
        at += 1
    print(
        f"B12-SWEEP {scenario} points={at - 1} failures={len(failures)} "
        f"empty_after_fault={len(empties)}"
    )
    for line in empties:
        print("  EMPTY", line)
    for line in failures:
        print("  FAIL", line)
    assert not failures, f"{len(failures)} of {at - 1}: " + " || ".join(failures[:6])


def _unused() -> object:  # keeps helper imports referenced
    return SimulatedCrash
