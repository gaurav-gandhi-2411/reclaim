"""Agent Leftovers never starts git in a worktree's MAIN repository when that repository IS, is
INSIDE or CONTAINS an excluded project (the #239 rule, applied to the repository side).

`evaluate_worktree` walked the worktree's own tree but ran `git worktree list` in the main
repository, whose subtree it never looked at; the vault bookkeeping after a move or a restore ran
`rev-list` and `worktree repair` there too. Every test records each git invocation.

The made-up excluded name (`zqexcl`) is absent from every test function name, because pytest's
`tmp_path` embeds the test name and the pattern matches the whole path.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from agent_fixtures import World, add_worktree, make_junction, make_repo, make_world

from reclaim import agent_artifacts as aa
from reclaim import executor

_NAME = "zqexcl"


class _Counting:
    """A `GitRunner` that records every call and then runs the real git (in a tmp repo)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> aa.GitResult:
        self.calls.append((tuple(args), cwd))
        return aa.run_git(args, cwd)


def test_main_repo_holding_a_protected_project_runs_no_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    deep = repo / "vendor" / "a" / "b" / f"{_NAME}-copy"
    deep.mkdir(parents=True)
    (deep / "notes.txt").write_text("x\n", encoding="utf-8")
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)

    verdict = aa.evaluate_worktree(world.env, wt, repo)
    via_path = aa.evaluate_path(world.env, wt, aa.CAT_WORKTREE)

    assert verdict.excluded and not verdict.offered and verdict.failed_rule == "W7"
    assert via_path.excluded and not via_path.offered
    assert run.calls == []


def test_main_repo_reaching_a_protected_name_only_through_a_junction(tmp_path: Path) -> None:
    """The walk descends a junction that leads outside the repository, so a protected NAME inside
    the target is found."""
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    outside = tmp_path / "elsewhere"
    (outside / f"{_NAME}-inner").mkdir(parents=True)
    make_junction(repo / "link", outside)
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    try:
        verdict = aa.evaluate_worktree(world.env, wt, repo)
    finally:
        (repo / "link").rmdir()  # removes the junction only, never its target

    assert verdict.excluded and not verdict.offered
    assert run.calls == []


def test_a_failing_walk_of_the_main_repo_counts_as_excluded(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    real_match = world.env.exclusion_match

    def match(path: Path, is_dir: bool) -> str | None:
        if path == repo:
            raise PermissionError("walk failed")
        return real_match(path, is_dir)

    world.env.exclusion_match = match

    verdict = aa.evaluate_worktree(world.env, wt, repo)

    assert verdict.excluded and not verdict.offered
    assert run.calls == []


def test_the_main_repo_is_walked_once_for_many_worktrees(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    worktrees = [add_worktree(repo, f"proj-wt-{i}", f"feat{i}") for i in range(2)]
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    real_match = world.env.exclusion_match
    walked: list[Path] = []

    def match(path: Path, is_dir: bool) -> str | None:
        walked.append(path)
        return real_match(path, is_dir)

    world.env.exclusion_match = match

    for wt in worktrees:
        aa.evaluate_worktree(world.env, wt, repo)

    assert walked.count(repo) == 1
    assert run.calls  # the clean repository still got its git proofs


def test_a_fresh_env_walks_again_so_apply_never_trusts_scan_time_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    scan_env = make_world(tmp_path, exclusions=(_NAME,)).env
    assert not aa.evaluate_worktree(scan_env, wt, repo).excluded
    (repo / f"{_NAME}-late").mkdir()

    # Same env: the cached answer (scan time). A rebuilt env (what `reverify` does): live state.
    assert not aa.evaluate_worktree(scan_env, wt, repo).excluded
    apply_env = make_world(tmp_path, exclusions=(_NAME,)).env
    assert aa.evaluate_worktree(apply_env, wt, repo).excluded


def _vaulted_worktree(tmp_path: Path, run: aa.GitRunner) -> tuple[Path, Path, Path, World]:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    (repo / "vendor" / f"{_NAME}-copy").mkdir(parents=True)
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    vaulted = world.vault / "item"
    vaulted.parent.mkdir(parents=True)
    wt.rename(vaulted)
    return repo, wt, vaulted, world


def test_after_vault_move_runs_no_git_in_a_main_repo_holding_a_protected_project(
    tmp_path: Path,
) -> None:
    run = _Counting()
    repo, wt, vaulted, world = _vaulted_worktree(tmp_path, run)

    note = aa.after_vault_move(wt, vaulted, run, repo_excluded=aa.repo_guard(world.safety))

    assert note is not None and "excluded project" in note
    assert run.calls == []
    assert any((repo / ".git" / "worktrees").iterdir())  # the admin entry was kept


def test_after_restore_runs_no_git_in_a_main_repo_holding_a_protected_project(
    tmp_path: Path,
) -> None:
    run = _Counting()
    repo, wt, vaulted, world = _vaulted_worktree(tmp_path, run)
    backup = vaulted / aa.ADMIN_BACKUP_DIR
    (backup / "admin").mkdir(parents=True)
    admin = next((repo / ".git" / "worktrees").iterdir())
    meta = {"original_path": str(wt), "repo_root": str(repo), "admin": str(admin)}
    (backup / "meta.json").write_text(json.dumps({**meta, "root_commits": []}), encoding="utf-8")

    note = aa.after_restore(vaulted, run, repo_excluded=aa.repo_guard(world.safety))

    assert note is not None and "excluded project" in note
    assert run.calls == []
    assert backup.exists()  # nothing was consumed


def test_the_bookkeeping_guard_is_required_and_the_executor_passes_it() -> None:
    """The chain is `apply_batch` / `restore_batch` -> `after_*(..., repo_excluded=repo_guard(
    safety))` with no default: calling either without the guard is a TypeError, so a caller that
    stops passing it fails loudly instead of running git unguarded."""
    with pytest.raises(TypeError):
        aa.after_vault_move(Path("a"), Path("b"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        aa.after_restore(Path("a"))  # type: ignore[call-arg]
    source = inspect.getsource(executor)
    assert source.count("repo_excluded=agent_artifacts.repo_guard(safety)") == 2


def test_the_bookkeeping_guard_lets_a_clean_repository_through(tmp_path: Path) -> None:
    world = make_world(tmp_path, exclusions=(_NAME,))
    repo = make_repo(tmp_path, "proj")
    guard = aa.repo_guard(world.safety)

    assert guard(repo) is False
    (repo / f"{_NAME}-x").mkdir()
    assert guard(repo) is True


def test_the_bookkeeping_guard_blocks_when_its_walk_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #247 verifier mutant M5: a guard that answered "free" on a walk error survived.
    world = make_world(tmp_path, exclusions=(_NAME,))
    repo = make_repo(tmp_path, "proj")

    def failing_walk(*args: object, **kwargs: object) -> str | None:
        raise PermissionError("walk failed")

    monkeypatch.setattr(world.safety, "exclusion_match", failing_walk)

    assert aa.repo_guard(world.safety)(repo) is True


def test_the_walk_cache_is_per_repository(tmp_path: Path) -> None:
    # #247 verifier mutant M10: one cached answer shared by every repository survived. In one env,
    # a clean repository and one holding a protected project must each get their own answer,
    # in either order.
    clean = make_repo(tmp_path / "a", "proj")
    held = make_repo(tmp_path / "b", "proj")
    (held / "vendor" / f"{_NAME}-copy").mkdir(parents=True)
    wt_clean = add_worktree(clean, "proj-wt-a", "feat-a")
    wt_held = add_worktree(held, "proj-wt-b", "feat-b")

    for order in ((wt_clean, clean), (wt_held, held)), ((wt_held, held), (wt_clean, clean)):
        env = make_world(tmp_path, exclusions=(_NAME,)).env
        verdicts = {repo: aa.evaluate_worktree(env, wt, repo) for wt, repo in order}
        assert not verdicts[clean].excluded
        assert verdicts[held].excluded


# Git for Windows lists a junction's content as the repository's own (checked 2026-11-03:
# `git status -uall` in a repo with `link` -> a directory printed `link/secret.txt`), so a
# junction whose TARGET is the protected directory must count, though no name under it matches.
def test_main_repo_junction_straight_to_a_protected_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    target = tmp_path / f"{_NAME}-proj"
    target.mkdir()
    (target / "src.txt").write_text("x\n", encoding="utf-8")
    make_junction(repo / "link", target)
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    try:
        verdict = aa.evaluate_worktree(world.env, wt, repo)
        guarded = aa.repo_guard(world.safety)(repo)
    finally:
        (repo / "link").rmdir()  # removes the junction only, never its target

    assert verdict.excluded and verdict.failed_rule == "W7"
    assert run.calls == []
    assert guarded is True
    assert (target / "src.txt").is_file()


def test_worktree_junction_straight_to_a_protected_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    target = tmp_path / f"{_NAME}-proj"
    target.mkdir()
    (target / "src.txt").write_text("x\n", encoding="utf-8")
    make_junction(wt / "link", target)
    run = _Counting()
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    try:
        verdict = aa.evaluate_worktree(world.env, wt, repo)
    finally:
        (wt / "link").rmdir()  # removes the junction only, never its target

    assert verdict.excluded and verdict.failed_rule == "W7"
    assert run.calls == []  # excluded BEFORE any git: git status would read through the link
    assert (target / "src.txt").is_file()


# --- the git dir of the main repository (#247 verifier F2) -------------------------------------
# `repo_guard` checked path strings and the subtree walk only, so a main repository that BORROWS
# an excluded project through its git dir (the project sits beside it, not beneath) still got git.


class _Recorder:
    """A `GitRunner` that records every call and never runs git."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> aa.GitResult:
        self.calls.append((tuple(args), cwd))
        return aa.GitResult(1, "")


def _borrow_alternates(repo: Path, project: Path) -> None:
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{project / '.git' / 'objects'}\n", encoding="utf-8")


def _borrow_commondir(repo: Path, project: Path) -> None:
    (repo / ".git" / "commondir").write_text(f"{project / '.git'}\n", encoding="utf-8")


def _borrow_core_worktree(repo: Path, project: Path) -> None:
    config = repo / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    config.write_text(f"{text}[core]\n\tworktree = {project.as_posix()}\n", encoding="utf-8")


_BORROWS = {
    "alternates": _borrow_alternates,
    "commondir": _borrow_commondir,
    "core_worktree": _borrow_core_worktree,
}


def _borrowing_repo(tmp_path: Path, how: str, run: aa.GitRunner) -> tuple[Path, Path, Path, World]:
    repo = make_repo(tmp_path, "proj")
    wt = add_worktree(repo, "proj-wt-feat", "feat")
    project = tmp_path / f"{_NAME}-proj"  # beside the repository, not beneath it
    (project / ".git" / "objects").mkdir(parents=True)
    _BORROWS[how](repo, project)
    world = make_world(tmp_path, exclusions=(_NAME,), run=run)
    vaulted = world.vault / "item"
    vaulted.parent.mkdir(parents=True)
    wt.rename(vaulted)
    return repo, wt, vaulted, world


@pytest.mark.parametrize("how", sorted(_BORROWS))
def test_guard_blocks_a_repo_whose_git_dir_borrows_a_protected_project(
    tmp_path: Path, how: str
) -> None:
    run = _Recorder()
    repo, _wt, _vaulted, world = _borrowing_repo(tmp_path, how, run)

    assert aa.repo_guard(world.safety)(repo) is True
    assert run.calls == []


@pytest.mark.parametrize("how", sorted(_BORROWS))
def test_after_vault_move_runs_no_git_in_a_repo_borrowing_a_protected_project(
    tmp_path: Path, how: str
) -> None:
    run = _Recorder()
    repo, wt, vaulted, world = _borrowing_repo(tmp_path, how, run)

    note = aa.after_vault_move(wt, vaulted, run, repo_excluded=aa.repo_guard(world.safety))

    assert note is not None and "excluded project" in note
    assert run.calls == []
    assert any((repo / ".git" / "worktrees").iterdir())  # the admin entry was kept


@pytest.mark.parametrize("how", sorted(_BORROWS))
def test_after_restore_runs_no_git_in_a_repo_borrowing_a_protected_project(
    tmp_path: Path, how: str
) -> None:
    run = _Recorder()
    repo, wt, vaulted, world = _borrowing_repo(tmp_path, how, run)
    backup = vaulted / aa.ADMIN_BACKUP_DIR
    (backup / "admin").mkdir(parents=True)
    admin = next((repo / ".git" / "worktrees").iterdir())
    meta = {"original_path": str(wt), "repo_root": str(repo), "admin": str(admin)}
    (backup / "meta.json").write_text(
        json.dumps({**meta, "root_commits": ["deadbeef"]}), encoding="utf-8"
    )

    note = aa.after_restore(vaulted, run, repo_excluded=aa.repo_guard(world.safety))

    assert note is not None and "excluded project" in note
    assert run.calls == []  # no `rev-list --max-parents=0 --all`
    assert backup.exists()


@pytest.mark.parametrize("pointer", ["not a pointer\n", "gitdir: {project}/.git\n"])
def test_guard_blocks_a_repo_whose_git_pointer_is_unparseable_or_names_a_protected_project(
    tmp_path: Path, pointer: str
) -> None:
    world = make_world(tmp_path, exclusions=(_NAME,))
    repo = tmp_path / "plain"
    repo.mkdir()
    project = tmp_path / f"{_NAME}-proj"
    (project / ".git").mkdir(parents=True)
    (repo / ".git").write_text(pointer.format(project=project.as_posix()), encoding="utf-8")

    assert aa.repo_guard(world.safety)(repo) is True  # nothing proves the pointer is harmless


# --- the junction flag cannot be turned off quietly (#247 verifier: thin tests) ---------------


def test_guard_passes_the_junction_flag_to_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path, exclusions=(_NAME,))
    repo = make_repo(tmp_path, "proj")
    seen: list[bool] = []

    def spy(path: object, *, is_dir: bool, match_junction_targets: bool = False) -> None:
        seen.append(match_junction_targets)

    monkeypatch.setattr(world.safety, "exclusion_match", spy)

    assert aa.repo_guard(world.safety)(repo) is False
    assert seen == [True]


def test_env_passes_the_junction_flag_to_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path, exclusions=(_NAME,))
    seen: list[bool] = []

    def spy(path: object, *, is_dir: bool, match_junction_targets: bool = False) -> None:
        seen.append(match_junction_targets)

    monkeypatch.setattr(world.safety, "exclusion_match", spy)

    assert world.env.exclusion_match(tmp_path, True) is None
    assert seen == [True]
