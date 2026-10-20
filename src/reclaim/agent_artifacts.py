"""Stale AI-agent leftovers: finished git worktrees, throwaway verify clones and idle agent
scratch (spec docs/specs/agent-artifacts.md; owner decisions 2026-10-20).

Everything here is a CONJUNCTION of rules, each proven by a read-only check; a check that cannot
complete (git fails, a process cannot be inspected, a walk errors) makes the item NOT a candidate
(fail closed). Nothing is deleted: an applied item is MOVED into the vault (same-volume rename,
never a copy) and can be moved back, with `git worktree prune` / `repair` bookkeeping for worktrees.

Rule ids (kept in the reasons shown to the user):
  worktree  W1 linked worktree  W2 clean  W3/W4 HEAD pushed to a real remote  W5 no live process
            W6 no live session transcript  W7 not excluded  W8 only regenerable ignored dirs, no
            reparse point  V  same volume as the vault
  clone     C1 standalone repo  C2 clean, no stash  C3/C4 every branch/tag/HEAD pushed
            C5 no live process  C6 no linked worktrees  C7 not excluded, W8, V
  scratch   S1 identity  S2 idle  S3 transcript idle  S4 no live process  S5 not excluded
            S7 every nested repository clean and pushed  S8 no reparse point  V

Every git command runs as `git --no-optional-locks -c core.fsmonitor=false ...` (a plain status
rewrites the index, and fsmonitor would run a program named in the repo's own config).
"""

# ruff: noqa: TRY301 -- `_Refusal` is deliberate control flow: each rule check raises, one handler reports
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from reclaim.app_paths import data_root
from reclaim.models import Candidate, FileRecord, Tier
from reclaim.models import Verdict as SafetyVerdict
from reclaim.procscan import ProcessScan, normalize_path, scan_processes
from reclaim.safety import first_matching_pattern
from reclaim.safety_env import assert_not_real_profile_under_pytest

if TYPE_CHECKING:
    from reclaim.index import ScanIndex
    from reclaim.safety import SafetyValidator

logger = structlog.get_logger(__name__)

GROUP = "agent_artifacts"
CAT_WORKTREE = "agent_worktree"
CAT_CLONE = "agent_clone"
CAT_SCRATCH = "agent_session_scratch"
CATEGORIES = (CAT_WORKTREE, CAT_CLONE, CAT_SCRATCH)
REASON_CODE = "AGENT_ARTIFACT_PROVEN_FINISHED"

ADMIN_BACKUP_DIR = ".reclaim-gitadmin"  # inside a vaulted worktree: its git admin dir + metadata

_GIT = ("git", "--no-optional-locks", "-c", "core.fsmonitor=false")
_GIT_TIMEOUT_SECONDS = 120.0
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SCP_REMOTE = re.compile(r"^[\w.-]+@[\w.-]+:[^\\/\s].*$")
_REMOTE_SCHEMES = ("https://", "ssh://", "git://")
# Ignored directories that are regenerable by definition (spec W8); anything else ignored
# (.env, *.db, build/, dist/, data and model dirs...) may hold non-regenerable work.
REGENERABLE_IGNORED_DIRS = frozenset(
    {
        "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
        ".mypy_cache", ".next",
    }
)  # fmt: skip
_STATE_FILES = ("MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "BISECT_LOG")
_STATE_DIRS = ("rebase-merge", "rebase-apply", "sequencer")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


# --- git ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitResult:
    rc: int
    out: str
    err: str = ""


GitRunner = Callable[[Sequence[str], Path], GitResult]


def run_git(args: Sequence[str], cwd: Path) -> GitResult:
    """Real git. Never raises: a missing git, a timeout or an OS error is rc -1 (fail closed).
    (Under pytest it refuses a working directory inside the real profile, like every mutating
    site: `worktree prune`/`repair` rewrite repository metadata.)"""
    assert_not_real_profile_under_pytest(cwd, operation="run git in")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [*_GIT, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(-1, "", f"{type(exc).__name__}: {exc}")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def is_real_remote_url(url: str) -> bool:
    """Allow-list: https://, ssh://, git:// or scp-style user@host:path. A drive-letter path, a
    UNC path, file:// or any local path is NOT a backup (spec W3)."""
    text = url.strip()
    return text.lower().startswith(_REMOTE_SCHEMES) or bool(_SCP_REMOTE.match(text))


# --- environment --------------------------------------------------------------------------


@dataclass
class AgentEnv:
    """Everything a rule check reads from the outside world, injectable for tests."""

    procs: ProcessScan
    now: float
    exclusion_match: Callable[[Path, bool], str | None]
    pattern_match: Callable[[Path], str | None]
    builtin_block: Callable[[Path], str | None]
    vault_dir: Path
    claude_projects_dir: Path
    temp_claude_dir: Path
    idle_seconds: float = 7 * 86400.0
    clone_name_patterns: tuple[str, ...] = ("*-verify*", "_verify*")
    run_git: GitRunner = run_git
    newest_mtime_hint: Callable[[Path], float | None] | None = None
    cache: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactVerdict:
    path: Path
    kind: str  # "worktree" | "clone" | "scratch"
    offered: bool
    held: tuple[str, ...]
    failed_rule: str = ""
    failed_detail: str = ""
    project: str = ""
    branch: str = ""
    last_commit_ts: float | None = None
    refs_fetched_ts: float | None = None
    repo_root: Path | None = None
    # Recognised as an excluded project from its PATH alone: never inspected (no git, no
    # process lookup, no walk) and never listed in a report (ADR-0039; the owner's hard rule).
    excluded: bool = False

    @property
    def category(self) -> str:
        return {"worktree": CAT_WORKTREE, "clone": CAT_CLONE, "scratch": CAT_SCRATCH}[self.kind]


class _Refusal(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


# --- small helpers ------------------------------------------------------------------------


def _posix_eq(a: Path | str, b: Path | str) -> bool:
    return normalize_path(a, expand_short=True) == normalize_path(b, expand_short=True)


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def same_volume(path: Path, vault_dir: Path) -> bool:
    try:
        return path.stat().st_dev == _nearest_existing(vault_dir).stat().st_dev
    except OSError:
        return False


def _slug(path: Path | str) -> str:
    """Claude Code's project slug for a path; every non-alphanumeric becomes `-` (how `_` and `.`
    are mapped is not documented, so BOTH sides are normalised this way before comparing)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path)).casefold()


def _git(env: AgentEnv, cwd: Path, *args: str) -> GitResult:
    return env.run_git(args, cwd)


def _must(env: AgentEnv, cwd: Path, rule: str, *args: str) -> str:
    result = _git(env, cwd, *args)
    if result.rc != 0:
        raise _Refusal(
            rule, f"git {' '.join(args[:3])} failed (rc {result.rc}): {result.err.strip()[:160]}"
        )
    return result.out


def _is_reparse(entry: os.DirEntry[str]) -> bool:
    try:
        if entry.is_symlink() or entry.is_junction():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True  # cannot tell: fail closed
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True, slots=True)
class TreeFacts:
    newest_mtime: float
    reparse: str | None
    git_entries: tuple[Path, ...]
    error: str | None


def walk_facts(root: Path, *, stop_at_reparse: bool = True) -> TreeFacts:
    """Live walk of `root` (never following links): newest mtime of ANY file or directory, the
    first reparse point (junction, symlink or any reparse attribute), every `.git` entry, and the
    first error. The root itself being a reparse point counts."""
    from reclaim.scanner import long_path  # local import: scanner is heavy and imports models

    newest = 0.0
    reparse: str | None = None
    git_entries: list[Path] = []
    error: str | None = None
    stack = [root]
    try:
        top = os.lstat(long_path(root))
        if getattr(top, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            return TreeFacts(newest, str(root), (), None)
        newest = top.st_mtime
    except OSError as exc:
        return TreeFacts(0.0, None, (), f"{root}: {exc}")
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                for entry in it:
                    path = current / entry.name
                    if _is_reparse(entry):
                        reparse = reparse or str(path)
                        if stop_at_reparse:
                            return TreeFacts(newest, reparse, tuple(git_entries), error)
                        continue
                    try:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError as exc:
                        error = error or f"{path}: {exc}"
                    if entry.name == ".git":
                        git_entries.append(path)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
        except OSError as exc:
            error = error or f"{current}: {exc}"
    return TreeFacts(newest, reparse, tuple(git_entries), error)


# --- shared repository proofs -------------------------------------------------------------


def _remotes(env: AgentEnv, repo: Path) -> list[str]:
    """Names of the remotes whose URL is a real remote. Raises on a git failure."""
    names = [n for n in _must(env, repo, "W3", "remote").split() if n]
    allowed = []
    for name in names:
        result = _git(env, repo, "remote", "get-url", name)
        if result.rc == 0 and is_real_remote_url(result.out):
            allowed.append(name)
    return allowed


def _prove_clean(env: AgentEnv, repo: Path, held: list[str], *, gitdir_state: bool = True) -> Path:
    """W2/C2: nothing uncommitted or hidden, no submodule, no operation in progress. Returns the
    absolute per-worktree git dir."""
    gitdir = Path(_must(env, repo, "W2", "rev-parse", "--absolute-git-dir").strip())
    toplevel = Path(_must(env, repo, "W2", "rev-parse", "--show-toplevel").strip())
    if not _posix_eq(toplevel, repo):
        raise _Refusal("W2", f"top level of the repository is {toplevel}, not {repo}")
    if gitdir_state:
        for name in _STATE_FILES:
            if (gitdir / name).exists():
                raise _Refusal("W2", f"{name} present: an operation is in progress")
        for name in _STATE_DIRS:
            if (gitdir / name).exists():
                raise _Refusal("W2", f"{name}/ present: an operation is in progress")
    if (repo / ".gitmodules").exists() or (gitdir / "modules").exists():
        raise _Refusal("W2", "submodules present (their git dir lives inside the removed area)")
    staged = _must(env, repo, "W2", "ls-files", "-s")
    if any(line.startswith("160000 ") for line in staged.splitlines()):
        raise _Refusal("W2", "a gitlink (submodule or nested repo) is tracked")
    status = _must(
        env, repo, "W2", "status", "--porcelain=v1", "--untracked-files=all",
        "--ignore-submodules=none",
    )  # fmt: skip
    if status.strip():
        raise _Refusal(
            "W2", f"uncommitted or untracked changes: {status.strip().splitlines()[0][:120]}"
        )
    for line in _must(env, repo, "W2", "ls-files", "-v").splitlines():
        if line and line[0] != "H":
            raise _Refusal("W2", f"hidden change flag '{line[0]}' on {line[2:][:100]}")
    held.append(
        "W2 no uncommitted, untracked or hidden changes, no submodule, no operation in progress"
    )
    return gitdir


def _prove_pushed(
    env: AgentEnv, repo: Path, held: list[str], *, everything: bool, rule: str
) -> list[str]:
    """W3/W4 (head only) or C3/C4 (all branches and tags): every commit is contained in a
    remote-tracking ref of a REAL remote. Returns the allowed remotes."""
    allowed = _remotes(env, repo)
    if not allowed:
        raise _Refusal(rule, "no remote with a real URL (https, ssh, git or scp-style)")
    if _git(env, repo, "rev-parse", "--verify", "-q", "HEAD").rc != 0:
        raise _Refusal(rule, "no commit at HEAD")
    tips = ["--branches", "--tags", "HEAD"] if everything else ["HEAD"]
    remotes = [f"--remotes={name}" for name in allowed]
    result = _git(env, repo, "rev-list", *tips, "--not", *remotes, "-n", "1")
    if result.rc != 0:
        raise _Refusal(
            rule, f"could not compare with the remote (rc {result.rc}): {result.err.strip()[:120]}"
        )
    if result.out.strip():
        raise _Refusal(
            rule, f"commit {result.out.strip()[:12]} is not on any remote ref of {allowed}"
        )
    held.append(
        f"{rule} every commit of {'all branches, tags and HEAD' if everything else 'HEAD'} is on a "
        f"remote ref of {', '.join(allowed)}"
    )
    return allowed


def _prove_no_stash(env: AgentEnv, repo: Path, held: list[str]) -> None:
    if _git(env, repo, "rev-parse", "--verify", "-q", "refs/stash").rc == 0:
        raise _Refusal("C2", "a stash exists (it would be lost with the clone)")
    held.append("C2 no stash")


def _ignored_entry_is_regenerable(rel: str) -> bool:
    """An ignored entry (`git status --ignored=matching`: a directory ends with `/`) is
    regenerable when it IS a directory named like a regenerable one, or lies inside such a
    directory (`.ruff_cache/.gitignore`). A file merely NAMED like one (`node_modules` the file) or
    a sibling that only shares a prefix (`node_modules_backup/`) is not."""
    parts = [p.lower() for p in rel.rstrip("/").split("/") if p]
    if not parts:
        return False
    is_dir = rel.endswith("/")
    inside = parts[:-1] if not is_dir else parts
    return any(part in REGENERABLE_IGNORED_DIRS for part in inside)


def _prove_ignored(env: AgentEnv, repo: Path, held: list[str]) -> None:
    """W8: the only ignored content is regenerable directories."""
    # `--ignored=matching` cannot be combined with `--untracked-files=no`: untracked entries are
    # listed too (`?? `), and any of them here (W2 already proved none) is a refusal as well.
    out = _must(env, repo, "W8", "status", "--ignored=matching", "--porcelain=v1", "-z")
    for item in out.split("\0"):
        if item.startswith("?? "):
            raise _Refusal("W8", f"untracked content appeared: {item[3:][:120]}")
        if not item.startswith("!! "):
            continue
        rel = item[3:]
        if not _ignored_entry_is_regenerable(rel):
            raise _Refusal("W8", f"ignored content that may not be regenerable: {rel[:120]}")
    held.append("W8 the only ignored content is regenerable directories")


def _prove_tree(env: AgentEnv, path: Path, held: list[str]) -> TreeFacts:
    facts = walk_facts(path)
    if facts.reparse is not None:
        raise _Refusal("W8", f"reparse point (junction or symlink) inside: {facts.reparse}")
    if facts.error is not None:
        raise _Refusal("W8", f"tree could not be fully read: {facts.error}")
    held.append("W8 no junction or symlink anywhere in the tree")
    return facts


def _prove_not_excluded(env: AgentEnv, path: Path, held: list[str], rule: str) -> None:
    hit = env.exclusion_match(path, True)
    if hit is not None:
        raise _Refusal(rule, f"matches the user exclusion '{hit}'")
    blocked = env.builtin_block(path)
    if blocked is not None:
        raise _Refusal(rule, f"the safety validator blocks it ({blocked})")
    held.append(f"{rule} not excluded and not blocked by the safety validator")


def _prove_unused(env: AgentEnv, path: Path, held: list[str], rule: str, *extra: str) -> None:
    reasons = env.procs.in_use(path)
    for token in extra:
        needle = token.casefold()
        for proc in env.procs.processes:
            if proc.cmdline and needle in proc.cmdline.casefold():
                reasons.append(f"{proc.name} (pid {proc.pid}) mentions {token}")
    if reasons:
        raise _Refusal(rule, "; ".join(reasons[:3]))
    held.append(f"{rule} no live process uses it")


def _prove_vault_volume(env: AgentEnv, path: Path, held: list[str]) -> None:
    if not same_volume(path, env.vault_dir):
        raise _Refusal(
            "V", f"not on the same volume as the vault {env.vault_dir} (a move would copy)"
        )
    held.append("V same volume as the vault (a rename, no copy)")


def _recent_transcript(env: AgentEnv, slug_source: Path | str, name: str | None) -> str | None:
    """A Claude Code transcript for this project (or this session) modified within the idle
    window, or None. Directory names are compared after the same normalisation as `_slug`."""
    wanted = _slug(slug_source)
    try:
        for entry in os.scandir(env.claude_projects_dir):
            if not entry.is_dir() or _slug(entry.name) != wanted:
                continue
            for child in os.scandir(entry.path):
                if not child.name.endswith(".jsonl"):
                    continue
                if name is not None and child.name.lower() != f"{name.lower()}.jsonl":
                    continue
                if env.now - child.stat().st_mtime < env.idle_seconds:
                    return child.path
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable ({exc})"  # fail closed: treat as a live session
    return None


def _repo_facts(env: AgentEnv, repo: Path) -> tuple[str, float | None, float | None]:
    branch = _git(env, repo, "rev-parse", "--abbrev-ref", "HEAD").out.strip()
    stamp = _git(env, repo, "log", "-1", "--format=%ct").out.strip()
    common = _git(env, repo, "rev-parse", "--git-common-dir").out.strip()
    fetched = None
    if common:
        fetch_head = Path(common) if Path(common).is_absolute() else repo / common
        try:
            fetched = (fetch_head / "FETCH_HEAD").stat().st_mtime
        except OSError:
            fetched = None
    return branch, float(stamp) if stamp.isdigit() else None, fetched


# --- worktrees ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkedWorktree:
    path: Path
    head: str
    branch: str
    locked: bool
    prunable: bool


def list_worktrees(env: AgentEnv, repo: Path) -> list[LinkedWorktree] | None:
    """Linked worktrees of `repo` (the main worktree excluded), or None when git failed."""
    return _list_worktrees(env.run_git, repo)


def _list_worktrees(run: GitRunner, repo: Path) -> list[LinkedWorktree] | None:
    result = run(("worktree", "list", "--porcelain", "-z"), repo)
    if result.rc != 0:
        return None
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in result.out.split("\0"):
        if not token:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = token.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return [
        LinkedWorktree(
            Path(e["worktree"]),
            e.get("HEAD", ""),
            e.get("branch", "detached"),
            "locked" in e,
            "prunable" in e,
        )
        for e in entries[1:]
        if "worktree" in e and "bare" not in e
    ]


def find_repo_of_worktree(path: Path) -> Path | None:
    """Main repository root of a linked worktree, from its `.git` pointer file."""
    pointer = path / ".git"
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.lower().startswith("gitdir:"):
        return None
    admin = Path(text[7:].strip())
    # <repo>/.git/worktrees/<name>
    if admin.parent.name.lower() != "worktrees" or admin.parent.parent.name.lower() != ".git":
        return None
    return admin.parent.parent.parent


def _excluded(kind: str, path: Path, project: str, repo: Path | None = None) -> ArtifactVerdict:
    rule = {"worktree": "W7", "clone": "C7", "scratch": "S5"}[kind]
    return ArtifactVerdict(
        path,
        kind,
        False,
        (),
        rule,
        "excluded project (not inspected)",
        project=project,
        repo_root=repo,
        excluded=True,
    )


def evaluate_worktree(env: AgentEnv, path: Path, repo: Path) -> ArtifactVerdict:
    held: list[str] = []
    project = repo.name
    # Exclusions first, from the path strings alone: an excluded project's worktree or repository
    # is not touched at all (no git runs in it), not merely refused later.
    if env.pattern_match(path) is not None or env.pattern_match(repo) is not None:
        return _excluded("worktree", path, project, repo)
    try:
        listed = list_worktrees(env, repo)
        if listed is None:
            raise _Refusal("W1", "git worktree list failed")
        entry = next((w for w in listed if _posix_eq(w.path, path)), None)
        if entry is None:
            raise _Refusal("W1", f"not a linked worktree of {repo}")
        if entry.locked or entry.prunable:
            raise _Refusal("W1", "locked or prunable worktree")
        pointer = path / ".git"
        if not pointer.is_file():
            raise _Refusal("W1", ".git is not a pointer file")
        held.append(f"W1 linked worktree of {repo.name}")
        _prove_clean(env, path, held)
        _prove_pushed(env, path, held, everything=False, rule="W4")
        _prove_unused(env, path, held, "W5")
        recent = _recent_transcript(env, path, None)
        if recent is not None:
            raise _Refusal(
                "W6", f"a session transcript was written within the idle window: {recent}"
            )
        held.append("W6 no recent session transcript for this worktree")
        _prove_not_excluded(env, path, held, "W7")
        _prove_ignored(env, path, held)
        _prove_tree(env, path, held)
        _prove_vault_volume(env, path, held)
        branch, commit_ts, fetched = _repo_facts(env, path)
        return ArtifactVerdict(
            path, "worktree", True, tuple(held), project=project, branch=branch,
            last_commit_ts=commit_ts, refs_fetched_ts=fetched, repo_root=repo,
        )  # fmt: skip
    except _Refusal as refusal:
        return ArtifactVerdict(path, "worktree", False, tuple(held), refusal.rule, refusal.detail,
                       project=project, repo_root=repo)  # fmt: skip


# --- throwaway clones ---------------------------------------------------------------------


def matches_clone_name(env: AgentEnv, path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in env.clone_name_patterns)


def evaluate_clone(env: AgentEnv, path: Path) -> ArtifactVerdict:
    held: list[str] = []
    project = path.name
    if env.pattern_match(path) is not None:
        return _excluded("clone", path, project, path)
    try:
        if not matches_clone_name(env, path):
            raise _Refusal("C1", "name does not match a throwaway-clone pattern")
        if not (path / ".git").is_dir():
            raise _Refusal("C1", ".git is not a directory (not a standalone clone)")
        held.append(
            f"C1 standalone clone with a throwaway name ({'/'.join(env.clone_name_patterns)})"
        )
        _prove_clean(env, path, held)
        _prove_no_stash(env, path, held)
        _prove_pushed(env, path, held, everything=True, rule="C4")
        listed = list_worktrees(env, path)
        if listed is None:
            raise _Refusal("C6", "git worktree list failed")
        if listed:
            raise _Refusal("C6", f"{len(listed)} linked worktree(s) depend on this clone")
        held.append("C6 no linked worktrees depend on it")
        _prove_unused(env, path, held, "C5")
        _prove_not_excluded(env, path, held, "C7")
        _prove_ignored(env, path, held)
        _prove_tree(env, path, held)
        _prove_vault_volume(env, path, held)
        branch, commit_ts, fetched = _repo_facts(env, path)
        return ArtifactVerdict(
            path, "clone", True, tuple(held), project=project, branch=branch,
            last_commit_ts=commit_ts, refs_fetched_ts=fetched, repo_root=path,
        )  # fmt: skip
    except _Refusal as refusal:
        return ArtifactVerdict(path, "clone", False, tuple(held), refusal.rule, refusal.detail,
                       project=project, repo_root=path)  # fmt: skip


# --- session scratch ----------------------------------------------------------------------


def is_session_dir(env: AgentEnv, path: Path) -> bool:
    return (
        _UUID.match(path.name) is not None
        and path.parent.parent.name.lower() == "claude"
        and _posix_eq(path.parent.parent, env.temp_claude_dir)
    )


def evaluate_scratch(env: AgentEnv, path: Path) -> ArtifactVerdict:
    held: list[str] = []
    slug = path.parent.name
    if env.pattern_match(path) is not None:
        return _excluded("scratch", path, slug)
    try:
        if not is_session_dir(env, path):
            raise _Refusal("S1", "not a %TEMP%\\claude\\<project>\\<session-uuid> directory")
        held.append("S1 a session directory (uuid-named, under %TEMP%\\claude\\<project>)")
        if env.newest_mtime_hint is not None:
            # Cheap early refusal from the scan index (aggregate rows included): a tree the
            # index already shows as recently written is not worth a full live walk.
            hinted = env.newest_mtime_hint(path)
            if hinted is not None and env.now - hinted < env.idle_seconds:
                raise _Refusal(
                    "S2", f"the index shows content only {(env.now - hinted) / 86400:.1f} days old"
                )
        facts = walk_facts(path)
        if facts.reparse is not None:
            raise _Refusal("S8", f"reparse point (junction or symlink) inside: {facts.reparse}")
        if facts.error is not None:
            raise _Refusal("S2", f"tree could not be fully read: {facts.error}")
        newest = facts.newest_mtime
        age = env.now - newest
        if age < env.idle_seconds:
            raise _Refusal("S2", f"newest content is only {age / 86400:.1f} days old")
        held.append(
            f"S2 newest content is {age / 86400:.1f} days old "
            f"(idle window {env.idle_seconds / 86400:.0f} days)"
        )
        held.append("S8 no junction or symlink anywhere in the tree")
        recent = _recent_transcript(env, path.parent.name, path.name)
        if recent is not None:
            raise _Refusal(
                "S3", f"the session transcript was written within the idle window: {recent}"
            )
        held.append("S3 the session transcript is idle or absent")
        _prove_unused(env, path, held, "S4", path.name)
        _prove_not_excluded(env, path, held, "S5")
        for git_entry in facts.git_entries:
            repo = git_entry.parent
            nested: list[str] = []
            try:
                _prove_clean(env, repo, nested)
                _prove_no_stash(env, repo, nested)
                _prove_pushed(env, repo, nested, everything=True, rule="S7")
            except _Refusal as inner:
                raise _Refusal(
                    "S7", f"nested repository {repo}: {inner.rule} {inner.detail}"
                ) from inner
        held.append(
            f"S7 {len(facts.git_entries)} nested repositories (0 is fine), "
            "each clean and fully pushed"
        )
        _prove_vault_volume(env, path, held)
        return ArtifactVerdict(
            path, "scratch", True, tuple(held), project=slug, last_commit_ts=newest
        )
    except _Refusal as refusal:
        return ArtifactVerdict(
            path, "scratch", False, tuple(held), refusal.rule, refusal.detail, project=slug
        )


# --- dispatch (detection and apply time share the same code) --------------------------------


def evaluate_path(env: AgentEnv, path: Path, category: str) -> ArtifactVerdict:
    """Re-prove ONE artifact from live state. This is what runs at detection and again, from
    scratch, right before an apply moves anything (the scan-time verdict is never trusted)."""
    if category == CAT_WORKTREE:
        if env.pattern_match(path) is not None:
            return _excluded("worktree", path, path.name)
        repo = find_repo_of_worktree(path)
        if repo is None:
            return ArtifactVerdict(
                path, "worktree", False, (), "W1", "no readable .git pointer to a repository"
            )
        return evaluate_worktree(env, path, repo)
    if category == CAT_CLONE:
        return evaluate_clone(env, path)
    if category == CAT_SCRATCH:
        return evaluate_scratch(env, path)
    return ArtifactVerdict(path, "worktree", False, (), "?", f"unknown category {category}")


# --- vault bookkeeping --------------------------------------------------------------------


def after_vault_move(original: Path, vault_path: Path, run: GitRunner = run_git) -> str | None:
    """A worktree was just moved into the vault: keep a copy of its git admin dir inside the
    vaulted directory, then `git worktree prune` so the repository stops listing a missing
    directory. Returns None on success or a message (never raises: the item IS vaulted)."""
    assert_not_real_profile_under_pytest(vault_path, operation="write a git admin backup into")
    pointer = vault_path / ".git"
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
        admin = Path(text[7:].strip())
        repo = admin.parent.parent.parent
        backup = vault_path / ADMIN_BACKUP_DIR
        backup.mkdir()
        shutil.copytree(admin, backup / "admin")
        (backup / "meta.json").write_text(
            json.dumps(
                {"original_path": str(original), "repo_root": str(repo), "admin": str(admin)}
            ),
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        return f"could not keep the git admin dir ({exc}); the worktree entry was NOT pruned"
    result = run(("worktree", "prune"), repo)
    if result.rc != 0:
        return f"git worktree prune failed (rc {result.rc}): {result.err.strip()[:160]}"
    return None


def after_restore(restored: Path, run: GitRunner = run_git) -> str | None:
    """A vaulted worktree is back at its original path: put its git admin dir back if `prune`
    removed it, drop our backup folder and run `git worktree repair`. None on success."""
    assert_not_real_profile_under_pytest(restored, operation="restore git admin data into")
    backup = restored / ADMIN_BACKUP_DIR
    meta_file = backup / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        repo = Path(meta["repo_root"])
        admin = Path(meta["admin"])
        if not admin.exists():
            shutil.copytree(backup / "admin", admin)
        shutil.rmtree(backup)
    except (OSError, ValueError, KeyError) as exc:
        return f"restored, but the git admin dir could not be put back ({exc})"
    result = run(("worktree", "repair", str(restored)), repo)
    if result.rc != 0:
        return (
            f"restored, but git worktree repair failed (rc {result.rc}): {result.err.strip()[:160]}"
        )
    listed = _list_worktrees(run, repo)
    if listed is None or not any(_posix_eq(w.path, restored) for w in listed):
        return "restored, but git does not list the worktree after repair"
    return None


def describe(verdict: ArtifactVerdict, size_bytes: int | None = None) -> str:
    """One-paragraph rationale shown in the review queue: project, branch, activity, and the
    exact rules that held."""
    parts = [f"Agent leftover ({verdict.kind}) of '{verdict.project}'"]
    if verdict.branch:
        parts.append(f"branch {verdict.branch}")
    if verdict.last_commit_ts:
        parts.append(
            "last commit " + time.strftime("%Y-%m-%d", time.localtime(verdict.last_commit_ts))
        )
    if verdict.refs_fetched_ts:
        parts.append(
            "remote refs last fetched "
            + time.strftime("%Y-%m-%d", time.localtime(verdict.refs_fetched_ts))
        )
    if size_bytes is not None:
        parts.append(f"{size_bytes / 2**30:.2f} GiB")
    return "; ".join(parts) + ". Proven finished: " + " | ".join(verdict.held)


# --- environment builders, discovery, detection ----------------------------------------------


def default_vault_dir() -> Path:
    """The vault `executor.DEFAULT_VAULT_DIR` names (not imported: executor imports this module)."""
    return data_root() / "data" / "quarantine"


def _builtin_blocker(safety: SafetyValidator) -> Callable[[Path], str | None]:
    def check(path: Path) -> str | None:
        # The validator's own rules (protected roots, UNC, docker/WSL, deny lists...) on a record
        # whose git fields are cleared: its "inside a git repository" deny would otherwise
        # reject every worktree and clone, which is exactly what this module proves safe itself.
        record = FileRecord(
            path=path, is_dir=True, size_bytes=0, attributes=0, ext="",
            git_repo_root=None, git_repo_clean=False,
        )  # fmt: skip
        result = safety.evaluate(record)
        return result.reason_code if result.verdict == SafetyVerdict.BLOCKED else None

    return check


def build_env(
    safety: SafetyValidator,
    *,
    vault_dir: Path | None = None,
    procs: ProcessScan | None = None,
    now: float | None = None,
    run: GitRunner = run_git,
    newest_mtime_hint: Callable[[Path], float | None] | None = None,
    temp_claude_dir: Path | None = None,
    claude_projects_dir: Path | None = None,
) -> AgentEnv:
    settings = safety.agent_artifacts_settings
    return AgentEnv(
        procs=procs if procs is not None else scan_processes(),
        now=now if now is not None else time.time(),
        exclusion_match=lambda p, is_dir: safety.exclusion_match(p, is_dir=is_dir),
        pattern_match=lambda p: first_matching_pattern(p, safety.exclusion_patterns),
        builtin_block=_builtin_blocker(safety),
        vault_dir=vault_dir if vault_dir is not None else default_vault_dir(),
        claude_projects_dir=(
            claude_projects_dir
            if claude_projects_dir is not None
            else Path.home() / ".claude" / "projects"
        ),
        temp_claude_dir=(
            temp_claude_dir
            if temp_claude_dir is not None
            else Path(tempfile.gettempdir()) / "claude"
        ),
        idle_seconds=settings.idle_days * 86400.0,
        clone_name_patterns=tuple(settings.clone_name_patterns),
        run_git=run,
        newest_mtime_hint=newest_mtime_hint,
    )


def _inside(path: Path, root: Path) -> bool:
    a, b = normalize_path(path, expand_short=False), normalize_path(root, expand_short=False)
    return a == b or a.startswith(b + "\\")


def discover(index: ScanIndex, env: AgentEnv) -> list[tuple[str, Path]]:
    """Possible artifacts from the scan index: `(category, path)`. Cheap name lookups only; every
    one is then proven from live state by `evaluate_path`."""
    found: dict[str, tuple[str, Path]] = {}
    for record in index.files_by_name([".git"]):
        owner = record.path.parent
        if record.is_dir:
            if matches_clone_name(env, owner) and not _inside(owner, env.temp_claude_dir):
                found.setdefault(normalize_path(owner), (CAT_CLONE, owner))
        elif env.pattern_match(owner) is not None or find_repo_of_worktree(owner) is not None:
            found.setdefault(normalize_path(owner), (CAT_WORKTREE, owner))
    for slug in index.direct_children(env.temp_claude_dir):
        if not slug.is_dir:
            continue
        for session in index.direct_children(slug.path):
            if session.is_dir and _UUID.match(session.path.name):
                found.setdefault(normalize_path(session.path), (CAT_SCRATCH, session.path))
    return sorted(found.values(), key=lambda item: str(item[1]).casefold())


def detect_agent_artifacts(
    index: ScanIndex,
    safety: SafetyValidator,
    *,
    retention_days: int,
    now: float | None = None,
    env: AgentEnv | None = None,
    vault_dir: Path | None = None,
) -> tuple[list[Candidate], list[ArtifactVerdict]]:
    """Every provably-finished artifact as a Tier B candidate, plus the verdict of EVERY artifact
    examined (offered or refused, with the first rule that failed) for the dry-run report."""
    now_ts = now if now is not None else time.time()
    if env is None:
        env = build_env(
            safety,
            vault_dir=vault_dir,
            now=now_ts,
            newest_mtime_hint=index.subtree_newest_mtime,
        )
    verdicts = [evaluate_path(env, path, category) for category, path in discover(index, env)]
    candidates: list[Candidate] = []
    for verdict in verdicts:
        if not verdict.offered:
            continue
        record = index.get_record(verdict.path)
        if record is None:
            continue  # no scan-time identity baseline: not offered
        size = index.subtree_size_bytes(verdict.path)
        rebuild = {
            CAT_WORKTREE: "Restore it from the vault (reclaim undo), or recreate it with "
            "`git worktree add` from its branch.",
            CAT_CLONE: "Restore it from the vault (reclaim undo), or `git clone` it again.",
            CAT_SCRATCH: "Restore it from the vault (reclaim undo) within the retention window.",
        }[verdict.category]
        candidates.append(
            Candidate(
                path=verdict.path,
                is_dir=True,
                category=verdict.category,
                category_group=GROUP,
                size_bytes=size,
                tier=Tier.B,
                rationale=describe(verdict, size),
                rebuild_instruction=rebuild,
                safety_verdict=SafetyVerdict.ELIGIBLE,
                safety_reason_code=REASON_CODE,
                retention_days=retention_days,
                recovery_cost_note=None,
                dev=record.dev,
                ino=record.ino,
                mtime=record.mtime,
            )
        )
    return _drop_nested(candidates), verdicts


def _drop_nested(candidates: list[Candidate]) -> list[Candidate]:
    """A scratch directory that holds a worktree or clone is offered once, as the outer item."""
    outer = [c.path for c in candidates]
    return [c for c in candidates if not any(o != c.path and _inside(c.path, o) for o in outer)]


def reverify(
    candidate: Candidate,
    *,
    safety: SafetyValidator,
    vault_dir: Path,
    env: AgentEnv | None = None,
) -> str | None:
    """Apply-time re-proof from live state (never the scan-time verdict). None = still provably
    finished; otherwise the first rule that no longer holds."""
    live = env if env is not None else build_env(safety, vault_dir=vault_dir)
    verdict = evaluate_path(live, candidate.path, candidate.category)
    if verdict.offered:
        return None
    return f"{verdict.failed_rule}: {verdict.failed_detail}"


def report_lines(
    verdicts: Sequence[ArtifactVerdict], sizes: dict[Path, int] | None = None
) -> list[str]:
    """Human-readable dry-run report grouped by project."""
    lines: list[str] = []
    by_project: dict[str, list[ArtifactVerdict]] = {}
    for verdict in verdicts:
        if verdict.excluded:
            continue  # an excluded project is never surfaced
        by_project.setdefault(verdict.project or "(unknown)", []).append(verdict)
    for project in sorted(by_project, key=str.casefold):
        lines.append(project)
        for verdict in sorted(by_project[project], key=lambda v: str(v.path).casefold()):
            have = sizes is not None and verdict.path in sizes
            size = f"{sizes[verdict.path] / 2**30:.2f} GiB " if have and sizes else ""
            if verdict.offered:
                lines.append(f"  PROPOSE  {verdict.category:22} {size}{verdict.path}")
                lines.append("           " + " | ".join(verdict.held))
            else:
                lines.append(f"  KEEP     {verdict.category:22} {size}{verdict.path}")
                lines.append(
                    f"           first failed rule {verdict.failed_rule}: "
                    f"{verdict.failed_detail[:200]}"
                )
    return lines
