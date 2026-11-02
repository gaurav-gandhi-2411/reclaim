"""Agent Leftovers never starts git in a directory that CONTAINS an excluded project.

The string-only exclusion check at the top of each evaluator cannot see an excluded project that
sits INSIDE a worktree, clone or scratch directory (its own path says nothing). The subtree walk
(W7/C7/S5) used to run only after `git status` and the other proofs had already run in it. These
tests count every git invocation: it must be zero, and the verdict must stay "excluded".

The made-up excluded name (`zqexcl`) is absent from every test function name, because pytest's
`tmp_path` embeds the test name and the pattern matches the whole path.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent_fixtures import add_worktree, commit_file, make_repo, make_session, make_world

from reclaim import agent_artifacts as aa

_NAME = "zqexcl"


class _Counting:
    """A `GitRunner` that records every call and then runs the real git."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> aa.GitResult:
        self.calls.append((tuple(args), cwd))
        return aa.run_git(args, cwd)


def test_worktree_containing_a_protected_project_runs_no_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    (wt / "vendor" / f"{_NAME}-copy").mkdir(parents=True)
    (wt / "vendor" / f"{_NAME}-copy" / "notes.txt").write_text("x\n", encoding="utf-8")
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)

    verdict = aa.evaluate_worktree(world.env, wt, repo)
    via_path = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)

    assert verdict.excluded and not verdict.offered and verdict.failed_rule == "W7"
    assert via_path.excluded and not via_path.offered
    assert run.calls == []


def test_clone_containing_a_protected_project_runs_no_git(tmp_path: Path) -> None:
    clone = make_repo(tmp_path, "proj-verify")
    commit_file(clone, f"{_NAME}-sub/readme.txt", "inside\n", "excluded name inside")
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)

    verdict = aa.evaluate_clone(world.env, clone)

    assert verdict.excluded and not verdict.offered and verdict.failed_rule == "C7"
    assert run.calls == []


def test_scratch_containing_a_protected_project_runs_no_git(tmp_path: Path) -> None:
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    session = make_session(world, age_days=30)
    nested = make_repo(session / "scratchpad", "inner")
    (nested / f"{_NAME}-dir").mkdir()

    verdict = aa.evaluate_scratch(world.env, session)

    # Scratch runs no git before its exclusion proof (S5), so this pins that order.
    assert verdict.excluded and not verdict.offered
    assert run.calls == []


def test_a_worktree_without_a_protected_project_inside_still_gets_its_git_proofs(
    tmp_path: Path,
) -> None:
    """Teeth: the early check must not short-circuit ordinary artifacts."""
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)

    verdict = aa.evaluate_worktree(world.env, wt, repo)

    assert not verdict.excluded
    assert run.calls  # git ran for the proofs
