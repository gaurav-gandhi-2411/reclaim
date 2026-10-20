"""Hermetic fixtures for the agent-artifacts tests: REAL git repositories in a tmp dir.

The remote is a local bare repository, but the repository's fetch URL is a fake `https://` one and
only its PUSH url points at the bare repo, so Reclaim's remote allow-list (which looks at the
fetch URL, like `git remote get-url`) sees a real remote while pushes land in the bare repo and
update the remote-tracking refs. No network is ever used.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from reclaim import agent_artifacts as aa
from reclaim.config import (
    AgentArtifactsConfig,
    CategoriesConfig,
    Config,
    ExclusionsConfig,
)
from reclaim.procscan import ProcessScan, fake_scan
from reclaim.safety import SafetyValidator

_IDENT = (
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "user.name=tester",
    "-c",
    "commit.gpgsign=false",
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, test helper
        ["git", *_IDENT, *args],  # noqa: S607 -- git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def commit_file(repo: Path, name: str, text: str, message: str = "change") -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def make_repo(root: Path, name: str = "proj") -> Path:
    """A repository whose `main` is pushed to a (local, bare) remote with a fake https URL."""
    bare = root / f"{name}-remote.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(bare))
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "README.md", "hello\n", "initial")
    git(repo, "remote", "add", "origin", f"https://example.invalid/{name}.git")
    git(repo, "remote", "set-url", "--push", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def add_worktree(repo: Path, name: str, branch: str | None = None) -> Path:
    """A linked worktree on a new branch (or detached when `branch` is None), at HEAD = pushed."""
    path = repo.parent / name
    if branch is None:
        git(repo, "worktree", "add", "-q", "--detach", str(path))
    else:
        git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return path


def push_branch(worktree: Path, branch: str) -> None:
    git(worktree, "push", "-q", "-u", "origin", branch)


def worktree_listed(repo: Path, path: Path) -> bool:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    wanted = os.path.normcase(os.path.realpath(path))
    return any(
        os.path.normcase(os.path.realpath(line[9:])) == wanted
        for line in out.splitlines()
        if line.startswith("worktree ")
    )


def make_junction(link: Path, target: Path) -> None:
    subprocess.run(  # noqa: S603 -- test helper creating a directory junction
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 -- cmd builtin mklink
        check=True,
        capture_output=True,
    )


@dataclass
class World:
    root: Path
    safety: SafetyValidator
    env: aa.AgentEnv

    @property
    def vault(self) -> Path:
        return self.root / "vault"

    @property
    def temp_claude(self) -> Path:
        return self.root / "temp" / "claude"

    @property
    def projects(self) -> Path:
        return self.root / "claude_home" / "projects"


def make_world(
    root: Path,
    *,
    procs: ProcessScan | None = None,
    exclusions: tuple[str, ...] = (),
    idle_days: float = 7.0,
    now: float | None = None,
    run: aa.GitRunner = aa.run_git,
) -> World:
    config = Config(
        exclusions=ExclusionsConfig(project_names=list(exclusions)),
        categories=CategoriesConfig(
            agent_artifacts=AgentArtifactsConfig(enabled=True, idle_days=idle_days)
        ),
    )
    safety = SafetyValidator(config)
    placeholder = World(root, safety, None)  # type: ignore[arg-type]
    env = aa.build_env(
        safety,
        vault_dir=placeholder.vault,
        procs=procs if procs is not None else fake_scan(),
        now=now if now is not None else time.time(),
        run=run,
        temp_claude_dir=placeholder.temp_claude,
        claude_projects_dir=placeholder.projects,
    )
    return World(root, safety, env)


def make_session(world: World, slug: str = "C--work-proj", *, age_days: float = 10.0) -> Path:
    """`%TEMP%\\claude\\<slug>\\<uuid>` with a scratchpad file whose mtime is `age_days` old."""
    session = world.temp_claude / slug / str(uuid.uuid4())
    (session / "scratchpad").mkdir(parents=True)
    f = session / "scratchpad" / "notes.txt"
    f.write_text("scratch", encoding="utf-8")
    stamp = world.env.now - age_days * 86400
    for p in (f, session / "scratchpad", session):
        os.utime(p, (stamp, stamp))
    return session


def real_tempdir_claude() -> Path:
    return Path(tempfile.gettempdir()) / "claude"
