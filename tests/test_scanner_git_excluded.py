"""The scan never starts a git process in a project the user excluded.

A repo root that is, is inside, or CONTAINS an excluded project (or a checked-out gitlink that
is one) reads not-clean without running git: `git status` at an outer root lists untracked files
and `ls-files --recurse-submodules` enters submodules, so it would read inside the excluded
project. Every test is hermetic: `subprocess.run`/`shutil.which` are replaced in the scanner
module so a git invocation is counted, never executed; names are made up. The excluded name
(`zqexcl`) is deliberately absent from every test function name, because pytest's `tmp_path`
embeds the test name and the pattern matches the whole path.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.cli import main
from reclaim.config import Config, ExclusionsConfig, exclusion_patterns
from reclaim.index import ScanIndex
from reclaim.safety import subtree_exclusion_match
from reclaim.scanner import GitRepoCache

_NAME = "zqexcl"
_SRC = Path(__file__).resolve().parents[1] / "src" / "reclaim"


def _patterns() -> tuple[str, ...]:
    return tuple(exclusion_patterns(Config(exclusions=ExclusionsConfig(project_names=[_NAME]))))


class _FakeGit:
    """Stands in for `subprocess.run`: records the cwd of every git call, answers 'clean'."""

    def __init__(self, gitlinks: tuple[str, ...] = ()) -> None:
        self.cwds: list[Path] = []
        self._gitlinks = gitlinks

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.cwds.append(Path(str(kwargs["cwd"])))
        out = ""
        if "-s" in argv:
            out = "".join(f"160000 {'a' * 40} 0\t{rel}\0" for rel in self._gitlinks)
        elif "-v" in argv:
            out = "H a.txt\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> _FakeGit:
    fake = _FakeGit()
    monkeypatch.setattr(scanner_module.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(scanner_module.subprocess, "run", fake)
    return fake


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_repo_root_inside_a_protected_project_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    repo = _repo(tmp_path / f"{_NAME}-main" / "sub" / "repo")
    assert scanner_module._query_git_clean(repo, excluded_patterns=_patterns()) is False
    cache = GitRepoCache(_patterns())
    assert cache.is_clean(repo) is False
    assert cache.clean_for(repo, str(repo / "node_modules")) is False
    assert fake_git.cwds == []


def test_the_protected_project_as_the_repo_root_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    repo = _repo(tmp_path / _NAME)
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_repo_whose_work_tree_contains_a_protected_project_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    repo = _repo(tmp_path / "outer")
    (repo / "vendor" / f"{_NAME}-copy").mkdir(parents=True)
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_gitlink_that_is_protected_is_skipped_and_reads_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the nested path is excluded (the outer verdict is forced to 'not excluded', as a
    pattern matching nothing but the nested path would): the outer check runs, the gitlink's
    does not, and the whole answer is not-clean."""
    outer = _repo(tmp_path / "outer")
    nested = _repo(outer / "mod")
    fake = _FakeGit(gitlinks=("mod",))
    monkeypatch.setattr(scanner_module.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(scanner_module.subprocess, "run", fake)
    monkeypatch.setattr(scanner_module, "_git_target_excluded", lambda path, _p: path == nested)

    assert scanner_module._query_git_clean(outer, excluded_patterns=_patterns()) is False
    assert fake.cwds
    assert set(fake.cwds) == {outer}


def test_gitlink_inside_an_unprotected_repo_with_protected_content_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    outer = _repo(tmp_path / "outer")
    _repo(outer / f"{_NAME}-sub")
    assert scanner_module._query_git_clean(outer, excluded_patterns=_patterns()) is False
    assert fake_git.cwds == []


def test_unprotected_repo_still_gets_its_normal_check(tmp_path: Path, fake_git: _FakeGit) -> None:
    repo = _repo(tmp_path / "ordinary")
    assert GitRepoCache(_patterns()).is_clean(repo) is True
    assert len(fake_git.cwds) == 3 and set(fake_git.cwds) == {repo}
    # and with no exclusions configured at all
    other = _repo(tmp_path / "plain")
    assert GitRepoCache().is_clean(other) is True


def test_an_unresolvable_path_fails_closed(
    tmp_path: Path, fake_git: _FakeGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "ordinary")

    def boom(_path: object, _patterns: object, **_kw: object) -> str | None:
        raise OSError("unreadable")

    monkeypatch.setattr(scanner_module, "subtree_exclusion_match", boom)
    assert scanner_module._query_git_clean(repo, excluded_patterns=_patterns()) is False
    assert fake_git.cwds == []


def _scan_via_cli(tmp_path: Path, root: Path, names: list[str]) -> int:
    cfg = tmp_path / "config.toml"
    listed = ", ".join(f'"{n}"' for n in names)
    cfg.write_text(f"[exclusions]\nproject_names = [{listed}]\n", encoding="utf-8")
    db = tmp_path / "idx" / "index.sqlite3"
    return main(["scan", str(root), "--db", str(db), "--config", str(cfg)])


def _node_modules_repo(repo: Path) -> None:
    (repo / ".git").mkdir(parents=True)
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")


def test_cli_scan_passes_the_config_exclusions_to_the_git_cache(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    root = tmp_path / "root"
    _node_modules_repo(root / f"{_NAME}-app")
    _node_modules_repo(root / "ordinary")

    assert _scan_via_cli(tmp_path, root, [_NAME]) == 0

    # git ran for the ordinary repo (teeth: the node_modules row needs the answer) and never for
    # the protected one.
    assert fake_git.cwds
    assert all(cwd == root / "ordinary" for cwd in fake_git.cwds)
    with ScanIndex(tmp_path / "idx" / "index.sqlite3") as index:
        records = {r.path: r for r in index.full_inventory(under=root)}
    assert records[root / "ordinary" / "node_modules"].git_repo_clean is True
    assert records[root / f"{_NAME}-app" / "node_modules"].git_repo_clean is False


def test_cli_scan_without_exclusions_still_runs_git_everywhere(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    root = tmp_path / "root"
    _node_modules_repo(root / f"{_NAME}-app")
    assert _scan_via_cli(tmp_path, root, []) == 0
    assert {cwd for cwd in fake_git.cwds} == {root / f"{_NAME}-app"}


def test_cli_scan_refuses_when_the_config_cannot_be_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text("[exclusions\n", encoding="utf-8")
    assert main(["scan", str(root), "--db", str(tmp_path / "i.sqlite3"), "--config", str(cfg)]) == 1


def test_no_production_site_builds_a_git_cache_without_exclusions() -> None:
    """Wiring guard: a bare `GitRepoCache()` in src would silently skip the rule."""
    bare: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "GitRepoCache"
                and not node.args
                and not node.keywords
            ):
                bare.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert bare == []


# --- G2: the repo's own `.git` leads into an excluded project -------------------------------


def _protected_repo(tmp_path: Path) -> Path:
    """A made-up excluded repository with a linked-worktree admin dir."""
    main = _repo(tmp_path / f"{_NAME}-main")
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "objects").mkdir()
    return main


def test_git_file_pointing_into_a_protected_project_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    main = _protected_repo(tmp_path)
    wt = tmp_path / "wt-checkout"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8")
    assert GitRepoCache(_patterns()).is_clean(wt) is False
    assert fake_git.cwds == []


def test_nested_gitlink_whose_git_file_points_into_a_protected_project_runs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = _protected_repo(tmp_path)
    outer = _repo(tmp_path / "outer")
    (outer / "mod").mkdir()
    (outer / "mod" / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8"
    )
    fake = _FakeGit(gitlinks=("mod",))
    monkeypatch.setattr(scanner_module.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(scanner_module.subprocess, "run", fake)
    assert scanner_module._query_git_clean(outer, excluded_patterns=_patterns()) is False
    assert set(fake.cwds) == {outer}  # the outer repo's own check ran; the gitlink's did not


def test_commondir_naming_a_protected_repository_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    main = _protected_repo(tmp_path)
    repo = _repo(tmp_path / "borrower")
    (repo / ".git" / "commondir").write_text(f"{main / '.git'}\n", encoding="utf-8")
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_alternates_naming_a_protected_repository_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    main = _protected_repo(tmp_path)
    repo = _repo(tmp_path / "shared-clone")
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True)
    (info / "alternates").write_text(f"{main / '.git' / 'objects'}\n", encoding="utf-8")
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_unreadable_or_foreign_git_pointer_fails_closed_but_a_plain_one_is_checked(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("not a pointer\n", encoding="utf-8")
    assert GitRepoCache(_patterns()).is_clean(broken) is False
    assert fake_git.cwds == []
    # teeth: a pointer to an unrelated admin dir is allowed through to the normal check
    other = _repo(tmp_path / "other-main")
    (other / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "ordinary-wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {other / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8")
    assert GitRepoCache(_patterns()).is_clean(wt) is True
    assert len(fake_git.cwds) == 3


# --- D2: config.worktree is read by git too --------------------------------------------------


def _config_worktree_repo(tmp_path: Path, text: str) -> Path:
    repo = _repo(tmp_path / "sparse")
    (repo / ".git" / "config.worktree").write_text(text, encoding="utf-8")
    return repo


def test_core_worktree_in_config_worktree_into_a_protected_project_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    main = _protected_repo(tmp_path)
    repo = _config_worktree_repo(tmp_path, f"[core]\n\tworktree = {main.as_posix()}\n")
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_include_in_config_worktree_runs_no_git(tmp_path: Path, fake_git: _FakeGit) -> None:
    repo = _config_worktree_repo(tmp_path, "[include]\n\tpath = elsewhere.cfg\n")
    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_ordinary_config_worktree_still_gets_the_git_check(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    repo = _config_worktree_repo(tmp_path, "[core]\n\tsparseCheckout = true\n")
    assert GitRepoCache(_patterns()).is_clean(repo) is True
    assert len(fake_git.cwds) == 3


# --- G1: a junction inside the repo that points into an excluded project ---------------------

_Junction = Callable[[Path, Path], None]


@pytest.fixture
def junction() -> Iterator[_Junction]:
    """`make(link, target)`: an NTFS junction made with the stdlib (no subprocess, which the git
    fixture replaces), removed as a link first (`os.rmdir`) so the target is never touched."""
    winapi = pytest.importorskip("_winapi")
    made: list[Path] = []

    def make(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        winapi.CreateJunction(str(target), str(link))
        made.append(link)

    yield make
    for link in reversed(made):
        if link.is_junction():
            link.rmdir()


def test_junction_inside_the_repo_into_a_protected_project_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit, junction: _Junction
) -> None:
    # The target's contents are not named like the pattern: only its real path is.
    target = tmp_path / f"{_NAME}-real"
    (target / "data").mkdir(parents=True)
    (target / "data" / "f1.txt").write_text("x\n", encoding="utf-8")
    repo = _repo(tmp_path / "outer")
    junction(repo / "node_modules" / "lnk", target)

    assert GitRepoCache(_patterns()).is_clean(repo) is False
    assert fake_git.cwds == []


def test_junction_to_an_unprotected_place_still_gets_the_git_check(
    tmp_path: Path, fake_git: _FakeGit, junction: _Junction
) -> None:
    target = tmp_path / "somewhere-else"
    target.mkdir()
    repo = _repo(tmp_path / "outer")
    junction(repo / "node_modules" / "lnk", target)
    assert GitRepoCache(_patterns()).is_clean(repo) is True
    assert len(fake_git.cwds) == 3


def test_the_delete_path_walk_default_is_unchanged_by_the_junction_option(
    tmp_path: Path, junction: _Junction
) -> None:
    """`subtree_exclusion_match` keeps matching only text under the junction path unless asked."""
    target = tmp_path / f"{_NAME}-real"
    (target / "data").mkdir(parents=True)
    root = tmp_path / "outer"
    root.mkdir()
    junction(root / "lnk", target)
    patterns = _patterns()
    assert subtree_exclusion_match(root, patterns) is None
    assert subtree_exclusion_match(root, patterns, match_junction_targets=False) is None
    assert subtree_exclusion_match(root, patterns, match_junction_targets=True) is not None


# --- D1: path-shaped patterns against a junction target and its content ----------------------

_SHAPED = ["*/zqdir/*", "*/zqdir/data/*"]


@pytest.mark.parametrize("pattern", _SHAPED)
def test_junction_into_a_project_named_by_a_path_shaped_pattern_runs_no_git(
    tmp_path: Path, fake_git: _FakeGit, junction: _Junction, pattern: str
) -> None:
    target = tmp_path / "zqdir"
    (target / "data").mkdir(parents=True)
    (target / "data" / "f1.txt").write_text("x\n", encoding="utf-8")
    repo = _repo(tmp_path / "outer")
    junction(repo / "node_modules" / "lnk", target)

    assert GitRepoCache((pattern,)).is_clean(repo) is False
    assert fake_git.cwds == []


@pytest.mark.parametrize("pattern", _SHAPED)
def test_the_delete_path_default_ignores_path_shaped_junction_targets(
    tmp_path: Path, junction: _Junction, pattern: str
) -> None:
    target = tmp_path / "zqdir"
    (target / "data").mkdir(parents=True)
    (target / "data" / "f1.txt").write_text("x\n", encoding="utf-8")
    root = tmp_path / "outer"
    root.mkdir()
    junction(root / "lnk", target)
    assert subtree_exclusion_match(root, [pattern]) is None
    assert subtree_exclusion_match(root, [pattern], match_junction_targets=False) is None
    assert subtree_exclusion_match(root, [pattern], match_junction_targets=True) is not None
