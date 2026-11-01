from __future__ import annotations

# ruff: noqa: F811
# Tests of the B12 fixes (ADR-0044 addendum 12) beyond the reviewer's probes: one transient
# failure of an in-use probe's rename back (a scanner opening the folder) is retried, so the
# command finishes in one go and the path never points at nothing. Every root is under
# `tmp_path` (the `env` fixture).
from pathlib import Path

import pytest
from test_relocate import (  # type: ignore[import-not-found]
    BIG,
    Env,
    _is_link,
    _make_tree,
    _manifest,
    env,  # noqa: F401 -- the fixture
)

import reclaim.relocate as relocate
from reclaim.relocate import Stage, rollback


def _fail_once_renaming_onto(m: pytest.MonkeyPatch, dst: Path) -> dict[str, int]:
    real = relocate._rename
    seen = {"failed": 0}

    def rename(src: str, to: str) -> None:
        if not seen["failed"] and relocate._key(to) == relocate._key(dst):
            seen["failed"] += 1
            raise PermissionError(5, "Access is denied (simulated scanner handle)", to)
        real(src, to)

    m.setattr(relocate, "_rename", rename)
    m.setattr(relocate, "_RENAME_BACK_WAIT_S", 0.0)
    return seen


def test_a_transient_hold_on_the_plans_rename_back_is_retried(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    with monkeypatch.context() as m:
        seen = _fail_once_renaming_onto(m, env.source)
        plan = relocate.plan_move(env.source, env.target, env.policy, free_bytes=lambda _p: BIG)
    assert seen["failed"] == 1
    assert plan.ok, plan.reasons
    assert env.source.is_dir() and not Path(str(env.source) + relocate.PROBE_SUFFIX).exists()


def test_a_transient_hold_on_rollbacks_rename_back_is_retried(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_tree(env.source)
    before = _manifest(env.source)
    env.move()
    with monkeypatch.context() as m:
        seen = _fail_once_renaming_onto(m, env.target)
        out = rollback(env.journal_path())  # one call: no dangling junction to repair later
    assert seen["failed"] == 1
    assert out.journal.stage == Stage.ROLLED_BACK
    assert not _is_link(env.source) and _manifest(env.source) == before
