"""Hermetic fixtures for the agent-artifacts tests: REAL git repositories in a tmp dir.

There is no real remote. A repository gets `origin` with a plausible `https://` URL for both fetch
and push, and "pushing" a branch is recorded exactly as git records a successful push: the
remote-tracking ref `refs/remotes/origin/<branch>` is set to the pushed commit (`mark_pushed`). No
network is used, and no local path ever stands in for the remote (a push URL that points at a local
bare repository must NOT count as a backup; the tests prove that separately).
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
    done = subprocess.run(  # noqa: S603 -- fixed argv, test helper
        ["git", *_IDENT, *args],  # noqa: S607 -- git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and done.returncode != 0:
        # CalledProcessError's message drops stderr, which hid why a push failed only on CI.
        raise AssertionError(
            f"git {' '.join(args)} failed ({done.returncode}) in {cwd}\n"
            f"stdout: {done.stdout}\nstderr: {done.stderr}"
        )
    return done


def commit_file(repo: Path, name: str, text: str, message: str = "change") -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def mark_pushed(repo: Path, branch: str, remote: str = "origin") -> None:
    """Record that `branch` (as it is NOW) was pushed to `remote`: what a successful `git push`
    leaves behind in the local repository."""
    git(repo, "update-ref", f"refs/remotes/{remote}/{branch}", "HEAD")


def make_repo(root: Path, name: str = "proj") -> Path:
    """A repository whose `main` is recorded as pushed to `origin` (a plausible https URL)."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit_file(repo, "README.md", "hello\n", "initial")
    git(repo, "remote", "add", "origin", f"https://example.invalid/{name}.git")
    mark_pushed(repo, "main")
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
    mark_pushed(worktree, branch)


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
