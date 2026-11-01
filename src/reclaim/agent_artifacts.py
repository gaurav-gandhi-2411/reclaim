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

Every git command runs as `git --no-optional-locks -c core.fsmonitor=false -c
log.showSignature=false ...` with no graft file and no replace objects (a plain status rewrites the
index; fsmonitor and gpg would run programs the config names; grafts and replace refs change
which commits look pushed). NOT prevented: a clean filter configured for a stat-dirty tracked
file (`git lfs` among them) runs during `status`, as in any `git status` (ADR-0043 addendum 5).
"""

# ruff: noqa: TRY301 -- `_Refusal` is deliberate control flow: each rule check raises, one handler reports
from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

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

# core.longpaths: without it git on Windows cannot open an untracked tree deeper than MAX_PATH,
# warns, and still reports the repository clean (A3 review, F3).
# log.showSignature: `log` would run `gpg.program`, a program the config names (A5 review, F3).
_GIT = (
    "git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.longpaths=true",
    "-c", "log.showSignature=false",
)  # fmt: skip
# Variables that point git at another repository, index, object store or ref view, or inject
# config: inherited from a hook or a shell, every proof would describe the wrong thing.
_GIT_ENV_SCRUB = frozenset(
    {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE", "GIT_QUARANTINE_PATH", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
        "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
    }
)  # fmt: skip
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
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in _GIT_ENV_SCRUB
        and not k.upper().startswith("GIT_CONFIG_KEY_")
        and not k.upper().startswith("GIT_CONFIG_VALUE_")
    }
    # Grafts and replace refs rewrite history as git sees it: a graft file made an only-copy
    # commit look reachable from origin, so C4 offered it (A5 review, F1). The proofs read the
    # real commit graph. The graft file must be MISSING: an existing one, even `nul`, makes git
    # print a deprecation hint on every command, and the silent-output rule would refuse all.
    no_grafts = Path(tempfile.gettempdir()) / "reclaim-no-graft-file-0f4c2b7e" / "grafts"
    if no_grafts.parent.exists():
        return GitResult(-1, "", f"{no_grafts.parent} exists; it must not (it disables grafts)")
    env.update(
        GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_GRAFT_FILE=str(no_grafts),
        GIT_NO_REPLACE_OBJECTS="1",
    )  # fmt: skip
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


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104 -- a deny-list entry
# A drive in the path, also percent-encoded (`/%44:/x`) or in the legacy `d|` form, after any
# number of leading slashes (`ssh://host//D:/x`).
_DRIVE_IN_PATH = re.compile(r"^[\\/]*[A-Za-z][:|]([\\/]|$)")


def _is_local_host(host: str) -> bool:
    """This machine, in any spelling: a loopback or unspecified address in any IPv4 notation
    (`127.1`, `2130706433`, `0x7f000001`), IPv6 (`[0:0:0:0:0:0:0:1]`, `::ffff:127.0.0.1`), a
    trailing-dot name (`localhost.`), or this computer's own name (A3 review)."""
    name = host.strip("[]").rstrip(".").lower()
    if not name or name in _LOOPBACK_HOSTS or name.endswith(".localhost"):
        return True
    # No getfqdn(): it can block on DNS. The local names need no lookup.
    if name in {socket.gethostname().lower(), os.environ.get("COMPUTERNAME", "").lower()} - {""}:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        try:  # the BSD notations getaddrinfo also accepts (`127.1`, decimal, hex)
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback or address.is_unspecified


def is_real_remote_url(url: str) -> bool:
    """Allow-list: https://, ssh://, git:// or scp-style user@host:path, with a real host. A
    drive-letter path, a UNC path, file://, any local path, a host that is this machine
    (`ssh://localhost/...`, `127.1`, its own name) or a path that names a drive
    (`ssh://host/D:/backup`) is NOT a backup (spec W3)."""
    text = url.strip()
    if text.lower().startswith(_REMOTE_SCHEMES):
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname or ""
        path = urllib.parse.unquote(parts.path)
        return bool(host) and not _is_local_host(host) and not _DRIVE_IN_PATH.match(path)
    if not _SCP_REMOTE.match(text):
        return False
    host = text.split("@", 1)[1].split(":", 1)[0]
    path = text.split(":", 1)[1]
    return not _is_local_host(host) and not _DRIVE_IN_PATH.match(path)


# --- environment --------------------------------------------------------------------------


class PatternView(Protocol):
    """The one thing the git-dir exclusion proofs (`_path_excluded`, `_gitdir_excluded`,
    `_nested_git_excluded`) need from the environment: a string-only exclusion match. `AgentEnv`
    satisfies it; the scanner passes a minimal object, so one implementation decides both."""

    @property
    def pattern_match(self) -> Callable[[Path], str | None]: ...


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
    # The raw exclusion patterns, for the slug check (Claude's project slug rewrites `_` and `.`).
    exclusion_patterns: tuple[str, ...] = ()
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
    def __init__(self, rule: str, detail: str, *, excluded: bool = False) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail
        # An exclusion hit: the verdict is hidden from every report and its detail is generic.
        self.excluded = excluded


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


def _must_silent(env: AgentEnv, cwd: Path, rule: str, *args: str) -> str:
    """`_must` for a listing whose EMPTY output proves something (status, ls-files): git reports
    a directory it could not open only as a warning on stderr and still exits 0 with nothing
    listed (A3 review, F3), so any stderr at all is a refusal."""
    result = _git(env, cwd, *args)
    if result.rc != 0 or result.err.strip():
        raise _Refusal(
            rule,
            f"git {' '.join(args[:3])} (rc {result.rc}) reported: "
            f"{result.err.strip().splitlines()[0][:160] if result.err.strip() else 'no output'}",
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
    # Directories that look like a BARE repository (HEAD + objects + refs) outside any `.git`: a
    # bare repo can hold the only copy of a commit and has no working tree to inspect.
    bare_repos: tuple[Path, ...] = ()
    # Entries whose name ends with a dot or a space: invisible to git (and to most Win32 tools).
    odd_names: tuple[Path, ...] = ()


def walk_facts(root: Path, *, stop_at_reparse: bool = True) -> TreeFacts:
    """Live walk of `root` (never following links): newest mtime of ANY file or directory, the
    first reparse point (junction, symlink or any reparse attribute), every `.git` entry, and the
    first error. The root itself being a reparse point counts."""
    from reclaim.scanner import long_path  # local import: scanner is heavy and imports models

    newest = 0.0
    reparse: str | None = None
    git_entries: list[Path] = []
    bare_repos: list[Path] = []
    odd_names: list[Path] = []
    error: str | None = None
    stack = [root]
    try:
        top = os.lstat(long_path(root))
        if getattr(top, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            return TreeFacts(newest, str(root), (), None, ())
        newest = top.st_mtime
    except OSError as exc:
        return TreeFacts(0.0, None, (), f"{root}: {exc}", ())
    while stack:
        current = stack.pop()
        names: set[str] = set()
        try:
            with os.scandir(long_path(current)) as it:
                for entry in it:
                    path = current / entry.name
                    names.add(entry.name.lower())
                    if _is_reparse(entry):
                        reparse = reparse or str(path)
                        if stop_at_reparse:
                            return TreeFacts(
                                newest, reparse, tuple(git_entries), error, tuple(bare_repos)
                            )
                        continue
                    try:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError as exc:
                        error = error or f"{path}: {exc}"
                    # Git on Windows treats `.GIT`/`.Git` as `.git` (A3 review, F4).
                    if entry.name.casefold() == ".git":
                        git_entries.append(path)
                    if entry.name.endswith((".", " ")):
                        # Win32 path parsing strips these, so git cannot open such an entry and
                        # leaves it out of its listings without failing (A3 review, F3).
                        odd_names.append(path)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
        except OSError as exc:
            error = error or f"{current}: {exc}"
        # Only a repository's OWN git dir (named `.git`) is exempt; a bare repository anywhere
        # below it (`.git/backup.git`) can hold the only copy of a commit too (A3b review, N2).
        # `.git/modules/<name>` is a full git dir as well and is flagged: submodules are refused.
        if {"head", "objects", "refs"} <= names and current.name.casefold() != ".git":
            bare_repos.append(current)
    return TreeFacts(
        newest, reparse, tuple(git_entries), error, tuple(bare_repos), tuple(odd_names)
    )


# --- shared repository proofs -------------------------------------------------------------


def _remotes(env: AgentEnv, repo: Path) -> list[str]:
    """Names of the remotes whose URL is a real remote. Raises on a git failure."""
    # Git still honours the legacy `$GIT_COMMON_DIR/remotes/<name>` and `branches/<name>` files:
    # such a remote is missing from `git remote` and from `remote.*.fetch`, yet its `Pull:` line
    # can write into refs/remotes/origin/* (A3b review, N1). Not parsed: refused.
    common_text = _must(env, repo, "W3", "rev-parse", "--git-common-dir").strip()
    common = Path(common_text) if Path(common_text).is_absolute() else repo / common_text
    for legacy in ("remotes", "branches"):
        try:
            entries = [p.name for p in (common / legacy).iterdir()]
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _Refusal("W3", f"cannot read {common / legacy}: {exc}") from exc
        if entries:
            raise _Refusal("W3", f"a legacy remote definition is present: {legacy}/{entries[0]}")
    names = [n for n in _must(env, repo, "W3", "remote").split() if n]
    allowed = []
    for name in names:
        if any(ch in name for ch in "*?[]\\"):
            continue  # `--remotes=<name>` is a glob: such a name cannot be matched exactly
        # BOTH directions must be a real remote. A commit that only ever reached a local path
        # through `pushurl`/`pushInsteadOf` still updates `refs/remotes/<name>/*`: not a backup.
        fetch = _git(env, repo, "remote", "get-url", "--all", name)
        push = _git(env, repo, "remote", "get-url", "--push", "--all", name)
        urls = [*fetch.out.split("\n"), *push.out.split("\n")]
        urls = [u.strip() for u in urls if u.strip()]
        if fetch.rc == 0 and push.rc == 0 and urls and all(is_real_remote_url(u) for u in urls):
            allowed.append(name)
    if not allowed:
        return allowed
    # `--remotes=<name>` reads `refs/remotes/<name>/*`. Another remote can write there too: a
    # local `bak` whose fetch refspec maps into `refs/remotes/origin/*` (a push updates that
    # tracking ref), or a remote named `origin/bak` (A3 review, F6). Such a namespace proves
    # nothing about where a commit really is.
    # Loose refs live in files, so on Windows `Origin` and `origin` share one namespace.
    writers = _fetch_destinations(env, repo)
    trusted = []
    for name in allowed:
        ns = f"refs/remotes/{name}/".casefold()
        folded = name.casefold()
        if "/" in name:
            continue
        if any(
            other != name
            and (other.casefold() == folded or other.casefold().startswith(f"{folded}/"))
            for other in names
        ):
            continue  # `origin/bak` (or `ORIGIN`) writes into origin's namespace by its name
        if any(
            other != name and _ns_overlap(dst.casefold(), ns)
            for other, dsts in writers.items()
            for dst in dsts
        ):
            continue
        trusted.append(name)
    return trusted


def _fetch_destinations(env: AgentEnv, repo: Path) -> dict[str, list[str]]:
    """remote name -> the destination side of each of its fetch refspecs. Raises on a git
    failure other than "no such key" (rc 1)."""
    result = _git(env, repo, "config", "--get-regexp", r"^remote\..*\.fetch$")
    if result.rc not in (0, 1):
        raise _Refusal("W3", f"git config failed (rc {result.rc}): {result.err.strip()[:120]}")
    out: dict[str, list[str]] = {}
    for line in result.out.splitlines():
        key, _, spec = line.partition(" ")
        name = key[len("remote.") : -len(".fetch")]
        spec = spec.strip().lstrip("+")
        if not spec or spec.startswith("^"):
            continue  # a negative refspec writes nothing
        dst = spec.partition(":")[2]
        if dst:  # no destination: the fetch updates no tracking ref
            out.setdefault(name, []).append(dst)
    return out


def _ns_overlap(dst: str, namespace: str) -> bool:
    """A refspec destination (possibly a `*` glob) can name a ref inside `namespace`."""
    static = dst.split("*", 1)[0]
    return static.startswith(namespace) or namespace.startswith(static)


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
    for lock in gitdir.glob("*.lock"):
        raise _Refusal("W2", f"{lock.name} present: a git process is working in it")
    if (repo / ".gitmodules").exists() or (gitdir / "modules").exists():
        raise _Refusal("W2", "submodules present (their git dir lives inside the removed area)")
    staged = _must_silent(env, repo, "W2", "ls-files", "-s")
    if any(line.startswith("160000 ") for line in staged.splitlines()):
        raise _Refusal("W2", "a gitlink (submodule or nested repo) is tracked")
    status = _must_silent(
        env, repo, "W2", "status", "--porcelain=v1", "--untracked-files=all",
        "--ignore-submodules=none",
    )  # fmt: skip
    if status.strip():
        lines = status.strip().splitlines()
        # A path in the listing can name an excluded project (`?? zzexclproj/notes.txt`): the
        # refusal is then hidden like any other exclusion hit, not reported (A5 review, F4).
        named = [p.strip('"') for line in lines for p in line[3:].split(" -> ")]
        if _path_excluded(env, *(repo / p for p in named if p)):
            raise _Refusal("W2", "excluded project (not inspected further)", excluded=True)
        raise _Refusal("W2", f"uncommitted or untracked changes: {lines[0][:120]}")
    for line in _must_silent(env, repo, "W2", "ls-files", "-v").splitlines():
        if line and line[0] != "H":
            if _path_excluded(env, repo / line[2:]):  # a sparse-checkout path is not on disk
                raise _Refusal("W2", "excluded project (not inspected further)", excluded=True)
            raise _Refusal("W2", f"hidden change flag '{line[0]}' on {line[2:][:100]}")
    _prove_no_hiding_filter(env, repo)
    held.append(
        "W2 no uncommitted, untracked or hidden changes, no submodule, no operation in progress"
    )
    return gitdir


_FILTER_KEY = re.compile(r"^filter\.(.+)\.(clean|process)$", re.IGNORECASE)
_CHECK_ATTR_CHUNK = 100  # paths per `check-attr` call: well inside the Windows command-line limit


def _prove_no_hiding_filter(env: AgentEnv, repo: Path) -> None:
    """W2: no clean filter other than Git LFS applies to a tracked file. A clean filter can drop
    content on its way into the index (`nbstripout` drops notebook outputs): once that commit is
    pushed and the index stat matches, `status` is empty while the only copy of the dropped bytes
    is the working file (A6 review, F1). Git LFS keeps what it drops in its store, which W2/C4
    check. Nothing here runs a filter: `config` and `check-attr` only read."""
    result = _git(env, repo, "config", "--get-regexp", r"^filter\..*\.(clean|process)$")
    if result.rc not in (0, 1):
        raise _Refusal("W2", f"git config failed (rc {result.rc}): {result.err.strip()[:120]}")
    drivers = set()
    for line in result.out.splitlines():
        key, _, value = line.partition(" ")
        match = _FILTER_KEY.match(key)
        if match is not None and not value.strip().startswith("git-lfs "):
            drivers.add(match.group(1))
    if not drivers:
        return
    tracked = [p for p in _must_silent(env, repo, "W2", "ls-files", "-z").split("\0") if p]
    for start in range(0, len(tracked), _CHECK_ATTR_CHUNK):
        chunk = tracked[start : start + _CHECK_ATTR_CHUNK]
        out = _must_silent(env, repo, "W2", "check-attr", "-z", "filter", "--", *chunk)
        fields = out.split("\0")
        for path, value in zip(fields[0::3], fields[2::3], strict=False):
            if value in drivers:
                raise _Refusal(
                    "W2",
                    f"the clean filter '{value}' applies to {path[:100]}: what is committed may "
                    "not be what is on disk",
                )


def _hidden_if_excluded(env: AgentEnv, path: Path, refusal: _Refusal) -> _Refusal:
    """A refusal's detail can name a path inside the artifact (W2, W8, S2, S8...), and those
    rules run before the subtree exclusion walk: an artifact holding an excluded project's name
    is then hidden like any exclusion hit, never reported (A6 review, F2/F3). A walk that fails
    counts as a hit (fail closed)."""
    if refusal.excluded:
        return refusal
    try:
        hit = env.exclusion_match(path, True)
    except OSError:
        hit = "unreadable"
    if hit is None:
        return refusal
    return _Refusal(refusal.rule, "excluded project (not inspected further)", excluded=True)


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
    # `--all` = every ref (branches, tags, notes, stash, refs/original, refs/jj, custom namespaces)
    # plus HEAD. Commits that exist only in a reflog are NOT counted (accepted residual risk).
    # A linked worktree's shared refs stay in its repository, but its PER-WORKTREE refs live in
    # the admin dir that is removed after the move: once that is gone their commits are
    # unreachable and `git gc` drops them (A3 review, F1), so they must be on a remote too.
    tips = ["--all"] if everything else ["HEAD", *_per_worktree_refs(env, repo, rule)]
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
        f"{rule} every commit of "
        f"{'every ref and HEAD' if everything else 'HEAD and its per-worktree refs'} is on a "
        f"remote ref of {', '.join(allowed)}"
    )
    return allowed


_PER_WORKTREE_REF_PREFIXES = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")


def _per_worktree_refs(env: AgentEnv, repo: Path, rule: str) -> list[str]:
    """The refs that belong to this worktree alone (git: `refs/worktree/`, `refs/bisect/`,
    `refs/rewritten/`), read in the worktree itself."""
    out = _must(env, repo, rule, "for-each-ref", "--format=%(refname)", *_PER_WORKTREE_REF_PREFIXES)
    return [line.strip() for line in out.splitlines() if line.strip()]


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


def _regenerable_root(rel: str) -> str:
    """The shortest leading part of an ignored entry's path that ends at a regenerable name."""
    parts = [p for p in rel.rstrip("/").split("/") if p]
    for index, part in enumerate(parts):
        if part.lower() in REGENERABLE_IGNORED_DIRS:
            return "/".join(parts[: index + 1])
    return ""


def _prove_ignored(env: AgentEnv, repo: Path, held: list[str]) -> None:
    """W8: the only ignored content is regenerable directories."""
    # `--ignored=matching` cannot be combined with `--untracked-files=no`: untracked entries are
    # listed too (`?? `), and any of them here (W2 already proved none) is a refusal as well.
    # `--untracked-files=normal` is the default, spelled out: a configured
    # `status.showUntrackedFiles=no` made every status here fail (A6 review, O1).
    out = _must_silent(
        env, repo, "W8", "status", "--ignored=matching", "--untracked-files=normal",
        "--porcelain=v1", "-z",
    )  # fmt: skip
    for item in out.split("\0"):
        if item.startswith("?? "):
            raise _Refusal("W8", f"untracked content appeared: {item[3:][:120]}")
        if not item.startswith("!! "):
            continue
        rel = item[3:]
        if not _ignored_entry_is_regenerable(rel):
            raise _Refusal("W8", f"ignored content that may not be regenerable: {rel[:120]}")
        parts = rel.rstrip("/").split("/")
        if rel.endswith("/") and parts[-1].lower() in REGENERABLE_IGNORED_DIRS:
            continue  # an entirely ignored directory: git lists it only if nothing in it is tracked
        # An ignored FILE inside a directory that merely bears a regenerable NAME (`venv/`,
        # `node_modules/`): regenerable only if that directory holds no tracked source. A tracked
        # `venv/tool.py` next to an ignored `venv/keys.secretlocal` is a project, not a cache.
        root = _regenerable_root(rel)
        tracked = _must_silent(env, repo, "W8", "ls-files", "-z", "--", f":(literal){root}")
        if tracked.strip("\0"):
            raise _Refusal(
                "W8", f"tracked source lives under '{root}', so ignored {rel[:100]} is kept"
            )
    held.append("W8 the only ignored content is regenerable directories")


def _prove_nested(
    env: AgentEnv, root: Path, facts: TreeFacts, held: list[str], rule: str, *, include_root: bool
) -> None:
    """Every repository INSIDE the artifact (a clone in `.venv/src`, one in scratch...) must itself
    be clean and fully pushed, and a bare repository inside is refused outright (it can hold
    the only copy of a commit). `include_root`: a `.git` directly in `root` is the artifact's
    own (worktree, clone) unless the artifact is a scratch directory, where it is a nested repo."""
    if facts.bare_repos:
        raise _Refusal(rule, f"bare repository inside: {facts.bare_repos[0]}")
    entries = [e for e in facts.git_entries if include_root or e.parent != root]
    # Exclusions first, from paths alone, before ANY git command runs in a nested repository: a
    # neutral-looking folder can be a linked worktree of an excluded repository, which only its
    # `.git` pointer names (A3 review, F5).
    for entry in entries:
        if _nested_git_excluded(env, entry):
            raise _Refusal(rule, "excluded project (not inspected further)", excluded=True)
    for entry in entries:
        if not entry.is_dir():
            continue
        # A linked worktree OUTSIDE the artifact keeps its index, HEAD and objects in this git
        # dir: moving it breaks that worktree and purging loses its staged work (A4, F1).
        if _has_linked_worktrees(entry):
            raise _Refusal(rule, f"nested repository {entry.parent} has linked worktrees")
        if _has_lfs_objects(entry):
            raise _Refusal(
                rule, f"nested repository {entry.parent} holds Git LFS or git-annex objects"
            )
    repos = sorted({e.parent for e in entries})
    for repo in repos:
        hidden = next((p for p in facts.odd_names if _inside(p, repo)), None)
        if hidden is not None:
            raise _Refusal(rule, f"nested repository {repo}: git cannot see {hidden}")
        nested: list[str] = []
        try:
            _prove_clean(env, repo, nested)
            _prove_no_stash(env, repo, nested)
            _prove_pushed(env, repo, nested, everything=True, rule=rule)
        except _Refusal as inner:
            raise _Refusal(
                rule, f"nested repository {repo}: {inner.rule} {inner.detail}"
            ) from inner
    held.append(
        f"{rule} {len(repos)} nested repositories (0 is fine), none bare, each clean and pushed"
    )


def _nested_git_excluded(env: PatternView, entry: Path) -> bool:
    """A `.git` entry inside an artifact belongs to an excluded project: by its own path, or by
    what its git dir (the directory itself, or the admin dir a pointer FILE names) refers to
    (`_gitdir_excluded`). A pointer that cannot be read or parsed counts as excluded: nothing
    proves it is not (fail closed)."""
    if _path_excluded(env, entry.parent):
        return True
    if entry.is_dir():
        return _gitdir_excluded(env, entry)
    try:
        admin = _resolve_gitdir(
            entry.read_text(encoding="utf-8", errors="replace").strip(), entry.parent
        )
    except OSError:
        return True
    if admin is None:
        return True
    return _gitdir_excluded(env, admin)


def _has_linked_worktrees(gitdir: Path) -> bool:
    """`gitdir/worktrees` has any entry (an unreadable one counts: fail closed). Read from the
    filesystem, so it also sees a worktree git itself would list as prunable."""
    try:
        return any((gitdir / "worktrees").iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True


# Large-file stores whose content lives outside the commits: C4/W4 prove commits are on a remote,
# not the content their pointers name (A4 review, F2: Git LFS; A5 review, F2: git-annex).
_LARGE_FILE_STORES = (("lfs", "objects"), ("annex", "objects"))


def _has_lfs_objects(gitdir: Path) -> bool:
    """Any file in a large-file store (Git LFS or git-annex) of `gitdir`. A skipped or failed
    `git lfs push`/`git annex copy`, or the tool missing when pushing, leaves the only copy here;
    without a network call nothing proves otherwise."""
    for parts in _LARGE_FILE_STORES:
        store = gitdir.joinpath(*parts)
        if not store.exists():
            continue
        try:
            for _dirpath, _dirs, files in os.walk(store, onerror=_raise):
                if files:
                    return True
        except OSError:
            return True
    return False


def _raise(exc: OSError) -> None:
    raise exc


# Config keys read without git: a key may also sit on its section's header line (`[core]
# worktree = x`), and git joins quoted pieces (`"zz"exclproj` is `zzexclproj`), so every value
# has its quotes removed (A8 review, F2).
# `[ \t]`, never `\s`: `\s` also matches newlines, and on a config of blank lines the scan went
# quadratic (50k blank lines took 213 s; A9 review, F3). A UTF-8 BOM may open the file.
# Any number of headers may precede the key (`[core][core] worktree = x`), and a `]` inside a
# quoted subsection does not end one (A10 review, 1). The value is captured greedily to its
# last non-blank character: a lazy capture was quadratic on a long run of blanks (A10, 2).
_KEY_PREFIX = r'^\ufeff?[ \t]*(?:\[(?:"[^"\n]*"|[^\]"\n])*\][ \t]*)*'
_KEY_VALUE = r"[ \t]*=[ \t]*([^\r\n]*[^ \t\r\n])[ \t]*\r?$"
_CORE_WORKTREE = re.compile(_KEY_PREFIX + r"worktree" + _KEY_VALUE, re.IGNORECASE | re.MULTILINE)
_REMOTE_URL = re.compile(_KEY_PREFIX + r"(?:push)?url" + _KEY_VALUE, re.IGNORECASE | re.MULTILINE)
# Files git READS on every command when the config names them: one inside an excluded project
# was read by every proof (A7 review, F1).
_CONFIG_FILE_REF = re.compile(
    _KEY_PREFIX + r"(?:excludesfile|attributesfile)" + _KEY_VALUE, re.IGNORECASE | re.MULTILINE
)
# An include (nested, quoted, conditional...) or a continued line cannot be followed reliably
# without git, which would read the file itself: such a config counts as excluded (A8, F2).
# `[include` ANYWHERE: git honours one after another header on the same line, or after a BOM
# (A9, F2). A continued line ends in an ODD run of backslashes: `\\` at the end is git's own
# escape of one backslash, as in `url = D:\\repos\\x\\` (A9 review, F1: that hid such clones).
_UNFOLLOWABLE_CONFIG = re.compile(
    r"\[[ \t]*include|(?<!\\)(?:\\\\)*\\\r?$", re.IGNORECASE | re.MULTILINE
)
_LOCAL_URL = re.compile(r"^(?:file://[^/\\]*)?(?:[A-Za-z]:[\\/]|[\\/])", re.IGNORECASE)
_FILE_URL_PREFIX = re.compile(r"^file://[^/\\]*", re.IGNORECASE)  # `file://host` included


def _config_value(raw: str) -> str:
    return raw.replace('"', "")


def _url_repo_name(url: str) -> str:
    """The repository name a remote URL ends in: `https://h/u/zzexclproj.git` -> `zzexclproj`,
    and `D:\\r\\zzexclproj\\.git` (a clone of a working copy's git dir) -> `zzexclproj`."""
    parts = [p for p in re.split(r"[\\/:]", url.strip().strip('"')) if p]
    while parts and parts[-1].lower() == ".git":
        parts.pop()
    tail = parts[-1] if parts else ""
    return tail[:-4] if tail.lower().endswith(".git") else tail


def _gitdir_excluded(env: PatternView, gitdir: Path, depth: int = 0) -> bool:
    """Git run in `gitdir` would read an excluded project: the git dir itself, its common dir (a
    linked worktree's main repository), any object store it borrows through
    `objects/info/alternates` (`clone --shared`), or a `core.worktree` it names (A3b review, N4:
    these held for `.git` FILES only, so a `.git` DIRECTORY with a commondir read the excluded
    repository). A file that exists but cannot be read counts as excluded (fail closed)."""
    if depth > 4:
        return True  # an alternates chain this long is not a normal repository
    targets = [gitdir, gitdir.parent]
    deeper: list[Path] = []  # git dirs whose own settings apply too

    def read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None

    try:
        common = read(gitdir / "commondir")
        if common is not None and common.strip():
            resolved = Path(os.path.normpath(gitdir / common.strip()))
            targets += [resolved, resolved.parent]
            deeper.append(resolved)
        # `config.worktree` is read by git too when extensions.worktreeConfig is set (sparse
        # checkout and submodule absorb turn it on), and can carry core.worktree or an include.
        for config_name in ("config", "config.worktree"):
            config = read(gitdir / config_name)
            if config is None:
                continue
            if _UNFOLLOWABLE_CONFIG.search(config):
                return True
            for match in _CORE_WORKTREE.finditer(config):
                value = _config_value(match.group(1))
                targets.append(Path(os.path.normpath(gitdir / value)))
            # A clone of an excluded project is that project, wherever it sits: its remote
            # names it (`.../zzexclproj.git`) even when the folder does not (A5 review, F5).
            for match in _REMOTE_URL.finditer(config):
                url = _config_value(match.group(1))
                name = _url_repo_name(url)
                if name:
                    targets.append(gitdir.parent / name / ".git")
                if _LOCAL_URL.match(url):  # a local remote is a path: match it as one (A7, F2)
                    targets.append(Path(os.path.normpath(_FILE_URL_PREFIX.sub("", url))))
            for match in _CONFIG_FILE_REF.finditer(config):  # (A7 review, F1)
                try:
                    ref = Path(_config_value(match.group(1))).expanduser()
                except (RuntimeError, ValueError):
                    # `~otheruser/x`, or no home directory: pathlib cannot say where it points
                    # (it raised, which aborted the whole detection run; A8 review, F1).
                    return True
                targets.append(Path(os.path.normpath(gitdir / ref)))
        objects = gitdir / "objects"
        alternates = read(objects / "info" / "alternates")
        # A linked worktree of this repository lives wherever its admin `gitdir` file says; one
        # inside an excluded project depends on these objects and this admin data (A4, F1).
        try:
            admins = list((gitdir / "worktrees").iterdir())
        except FileNotFoundError:
            admins = []
        for admin in admins:
            owner = read(admin / "gitdir")
            if owner is not None and owner.strip():
                pointer = Path(os.path.normpath(admin / owner.strip()))  # relative: to the admin
                targets += [pointer, pointer.parent]
    except OSError:
        return True
    for line in (alternates or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        store = Path(os.path.normpath(objects / line))  # relative to the objects dir
        targets += [store, store.parent]
        deeper.append(store.parent)
    if _path_excluded(env, *targets):
        return True
    return any(_gitdir_excluded(env, d, depth + 1) for d in deeper if d != gitdir)


def _prove_tree(env: AgentEnv, path: Path, held: list[str]) -> TreeFacts:
    facts = walk_facts(path)
    if facts.reparse is not None:
        raise _Refusal("W8", f"reparse point (junction or symlink) inside: {facts.reparse}")
    if facts.error is not None:
        raise _Refusal("W8", f"tree could not be fully read: {facts.error}")
    if facts.odd_names:
        raise _Refusal(
            "W8", f"git cannot see a name ending in a dot or space: {facts.odd_names[0]}"
        )
    held.append("W8 no junction or symlink anywhere in the tree")
    return facts


def _prove_not_excluded(env: AgentEnv, path: Path, held: list[str], rule: str) -> None:
    hit = env.exclusion_match(path, True)
    if hit is not None:
        raise _Refusal(rule, "excluded project (not inspected further)", excluded=True)
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


def _resolve_gitdir(text: str, base: Path) -> Path | None:
    """The admin dir a `.git` pointer file names. `gitdir: <path>`; a relative path (git's
    `--relative-paths`) is relative to the directory holding the pointer, not to our cwd."""
    if not text.lower().startswith("gitdir:"):
        return None
    admin = Path(text[7:].strip())
    if not admin.is_absolute():
        admin = Path(os.path.normpath(base / admin))
    return admin


def find_repo_of_worktree(path: Path) -> Path | None:
    """Main repository root of a linked worktree, from its `.git` pointer file."""
    pointer = path / ".git"
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    admin = _resolve_gitdir(text, path)
    if admin is None:
        return None
    # <repo>/.git/worktrees/<name>
    if admin.parent.name.lower() != "worktrees" or admin.parent.parent.name.lower() != ".git":
        return None
    return admin.parent.parent.parent


def _path_excluded(env: PatternView, *paths: Path) -> bool:
    """An excluded project, decided from path strings only: each path AND its real location (a
    junction or symlink alias of an excluded repository is the same repository)."""
    for path in paths:
        if env.pattern_match(path) is not None:
            return True
        try:
            real = Path(os.path.realpath(path))
        except (OSError, ValueError):
            continue
        if env.pattern_match(real) is not None:
            return True
    return False


def _subtree_excluded(env: AgentEnv, path: Path) -> bool:
    """`path` is, or holds anywhere beneath it, an excluded project (the name-only subtree walk the
    W7/C7 proofs use). Run BEFORE any git command in `path`: `git status` lists every untracked
    file, so it would read inside an excluded project the path itself does not name. A walk that
    fails counts as a hit (fail closed)."""
    try:
        return env.exclusion_match(path, True) is not None
    except OSError:
        return True


def _slug_excluded(env: AgentEnv, name: str) -> bool:
    """The exclusion globs against a Claude project slug (every non-alphanumeric is `-` there, so
    an excluded `my_proj` appears as `my-proj`); the patterns get the same rewrite."""
    patterns = [
        re.sub(r"[^a-z0-9*?\[\]]", "-", pattern.lower())
        for pattern in env.exclusion_patterns
        if not pattern.startswith("re:")
    ]
    return first_matching_pattern(_slug(name), patterns) is not None


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
    pointer_file = (path / ".git").is_file()  # anything else is refused visibly by W1 below
    if (
        _path_excluded(env, path, repo)
        or (pointer_file and _nested_git_excluded(env, path / ".git"))
        or _subtree_excluded(env, path)
    ):
        # The pointer's admin dir can name an excluded common dir (A3b review, N4/F5h).
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
        facts = _prove_tree(env, path, held)
        _prove_nested(env, path, facts, held, "W9", include_root=False)
        _prove_vault_volume(env, path, held)
        branch, commit_ts, fetched = _repo_facts(env, path)
        return ArtifactVerdict(
            path, "worktree", True, tuple(held), project=project, branch=branch,
            last_commit_ts=commit_ts, refs_fetched_ts=fetched, repo_root=repo,
        )  # fmt: skip
    except _Refusal as refusal:
        refusal = _hidden_if_excluded(env, path, refusal)
        return ArtifactVerdict(
            path, "worktree", False, tuple(held), refusal.rule, refusal.detail,
            project=project, repo_root=repo, excluded=refusal.excluded,
        )  # fmt: skip


# --- throwaway clones ---------------------------------------------------------------------


def matches_clone_name(env: AgentEnv, path: Path) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in env.clone_name_patterns)


def evaluate_clone(env: AgentEnv, path: Path) -> ArtifactVerdict:
    held: list[str] = []
    project = path.name
    if (
        _path_excluded(env, path)
        or _gitdir_excluded(env, path / ".git")
        or _subtree_excluded(env, path)
    ):
        # e.g. `clone --shared` of an excluded project reads its objects (A3b review, N4/F5g), or
        # an excluded project sits INSIDE it: git status would list its files
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
        if _has_lfs_objects(path / ".git"):
            raise _Refusal(
                "C4", "Git LFS or git-annex objects are present; nothing proves they were pushed"
            )
        listed = list_worktrees(env, path)
        if listed is None:
            raise _Refusal("C6", "git worktree list failed")
        if listed:
            raise _Refusal("C6", f"{len(listed)} linked worktree(s) depend on this clone")
        held.append("C6 no linked worktrees depend on it")
        _prove_unused(env, path, held, "C5")
        _prove_not_excluded(env, path, held, "C7")
        _prove_ignored(env, path, held)
        facts = _prove_tree(env, path, held)
        _prove_nested(env, path, facts, held, "C8", include_root=False)
        _prove_vault_volume(env, path, held)
        branch, commit_ts, fetched = _repo_facts(env, path)
        return ArtifactVerdict(
            path, "clone", True, tuple(held), project=project, branch=branch,
            last_commit_ts=commit_ts, refs_fetched_ts=fetched, repo_root=path,
        )  # fmt: skip
    except _Refusal as refusal:
        refusal = _hidden_if_excluded(env, path, refusal)
        return ArtifactVerdict(
            path, "clone", False, tuple(held), refusal.rule, refusal.detail,
            project=project, repo_root=path, excluded=refusal.excluded,
        )  # fmt: skip


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
    if _path_excluded(env, path) or _slug_excluded(env, slug):
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
        _prove_nested(env, path, facts, held, "S7", include_root=True)
        _prove_vault_volume(env, path, held)
        return ArtifactVerdict(
            path, "scratch", True, tuple(held), project=slug, last_commit_ts=newest
        )
    except _Refusal as refusal:
        refusal = _hidden_if_excluded(env, path, refusal)
        return ArtifactVerdict(
            path, "scratch", False, tuple(held), refusal.rule, refusal.detail,
            project=slug, excluded=refusal.excluded,
        )  # fmt: skip


# --- dispatch (detection and apply time share the same code) --------------------------------


def evaluate_path(env: AgentEnv, path: Path, category: str) -> ArtifactVerdict:
    """Re-prove ONE artifact from live state. This is what runs at detection and again, from
    scratch, right before an apply moves anything (the scan-time verdict is never trusted)."""
    if category == CAT_WORKTREE:
        if _path_excluded(env, path):
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


def _root_commits(run: GitRunner, repo: Path) -> list[str]:
    result = run(("rev-list", "--max-parents=0", "--all"), repo)
    return [line.strip() for line in result.out.splitlines() if line.strip()][:50]


def _admin_owned_by(admin: Path, worktree: Path) -> bool:
    """The admin dir's `gitdir` file names `worktree` (its `.git` pointer). Unreadable: False."""
    try:
        owner = Path((admin / "gitdir").read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False
    return _posix_eq(owner.parent if owner.name.lower() == ".git" else owner, worktree)


def _admin_tips_not_on_a_remote(admin: Path, repo: Path, run: GitRunner) -> str | None:
    """None when the admin entry's detached HEAD and every per-worktree ref it holds are on a
    remote-tracking ref, else what is not (or why it could not be checked). A HEAD that names a
    branch is fine: the branch lives in the repository, not here. Commits reachable only from
    this worktree's reflog or ORIG_HEAD are the accepted reflog class (ADR-0043)."""
    tips: list[str] = []
    try:
        head = (admin / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            tips.append(head)
        refs_dir = admin / "refs"
        if refs_dir.is_dir():
            for ref in refs_dir.rglob("*"):
                if ref.is_file():
                    tips.append(ref.read_text(encoding="utf-8", errors="replace").strip())
    except OSError as exc:
        return f"the admin entry could not be read ({exc})"
    if not tips:
        return None
    if any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tip) for tip in tips):
        return "the admin entry holds a ref that is not a plain commit id"
    result = run(("rev-list", "-n", "1", *tips, "--not", "--remotes"), repo)
    if result.rc != 0:
        return f"could not check the worktree's own commits (rc {result.rc})"
    if result.out.strip():
        return f"commit {result.out.strip()[:12]} of this worktree is not on any remote ref"
    return None


def after_vault_move(original: Path, vault_path: Path, run: GitRunner = run_git) -> str | None:
    """A worktree was just moved into the vault. Keep a copy of its git admin dir inside the
    vaulted directory, then remove EXACTLY that one admin entry so the repository stops listing a
    missing directory. (`git worktree prune` is deliberately NOT used: it drops the entry of every
    prunable worktree, including ones the user never asked to touch and whose directory is merely
    on an offline drive; an independent review reproduced losing a staged index and a detached
    unpushed commit that way.) Returns None on success or a message; never raises: the item IS
    vaulted."""
    assert_not_real_profile_under_pytest(vault_path, operation="write a git admin backup into")
    try:
        text = (vault_path / ".git").read_text(encoding="utf-8", errors="replace").strip()
        admin = _resolve_gitdir(text, original)  # relative pointers: relative to where it WAS
        if (
            admin is None
            or admin.parent.name.lower() != "worktrees"
            or admin.parent.parent.name.lower() != ".git"
        ):
            return "the .git pointer does not name a worktree admin dir; nothing was removed"
        repo = admin.parent.parent.parent
        if not _admin_owned_by(admin, original):
            return "that admin dir belongs to another path; nothing was removed"
        if (admin / "locked").exists():
            return "the worktree is locked; its admin entry was kept"
        unbacked = _admin_tips_not_on_a_remote(admin, repo, run)
        if unbacked is not None:
            # The proof ran before the move; a commit made since (A3b review, N5) is reachable
            # only through this admin entry, and `git gc` would drop it once the entry is gone.
            return f"{unbacked}; the admin entry was kept so git keeps that commit"
        backup = vault_path / ADMIN_BACKUP_DIR
        backup.mkdir()
        shutil.copytree(admin, backup / "admin")
        (backup / "meta.json").write_text(
            json.dumps(
                {
                    "original_path": str(original),
                    "repo_root": str(repo),
                    "admin": str(admin),
                    "root_commits": _root_commits(run, repo),
                }
            ),
            encoding="utf-8",
        )
        assert_not_real_profile_under_pytest(
            admin, operation="remove one worktree admin entry from"
        )
        shutil.rmtree(admin)
    except (OSError, ValueError) as exc:
        return f"could not keep or remove the git admin dir ({exc}); the entry was NOT removed"
    listed = _list_worktrees(run, repo)
    if listed is None or any(_posix_eq(w.path, original) for w in listed):
        return "git still lists the worktree after its admin entry was removed"
    return None


DETACHED_POINTER = ".git.reclaim-detached"


def _same_head(admin: Path, saved: Path) -> bool:
    """The live admin dir's HEAD is the one we saved (both readable and byte-identical)."""
    try:
        return (admin / "HEAD").read_bytes() == (saved / "HEAD").read_bytes()
    except OSError:
        return False


def _detach_pointer(restored: Path) -> str:
    """Rename the restored folder's `.git` pointer so git no longer follows it into an admin dir
    that is not ours; returns a sentence for the note."""
    assert_not_real_profile_under_pytest(restored, operation="detach the git pointer of")
    try:
        (restored / ".git").rename(restored / DETACHED_POINTER)
    except OSError as exc:
        return f". Its .git pointer could NOT be renamed ({exc}): do not run git in it"
    return f". Its .git pointer was renamed to {DETACHED_POINTER} so git does not use it"


def after_restore(restored: Path, run: GitRunner = run_git) -> str | None:
    """A vaulted worktree is back at its original path: put its git admin dir back, drop our
    backup folder and run `git worktree repair`. None on success. If the repository is no longer
    where it was, or a DIFFERENT repository now sits at that path, nothing is written into it and
    the saved admin data stays in the restored directory for a manual `git worktree repair`."""
    assert_not_real_profile_under_pytest(restored, operation="restore git admin data into")
    backup = restored / ADMIN_BACKUP_DIR
    meta_file = backup / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        repo = Path(meta["repo_root"])
        admin = Path(meta["admin"])
        saved_roots = set(meta.get("root_commits", []))
        if not (repo / ".git").is_dir():
            return (
                f"restored, but the repository is no longer at {repo}; the saved admin data stays "
                f"in {ADMIN_BACKUP_DIR}\\ for a manual `git worktree repair`"
            )
        if saved_roots and not saved_roots & set(_root_commits(run, repo)):
            return (
                f"restored, but a different repository is now at {repo}; nothing was written "
                f"into it (the saved admin data stays in {ADMIN_BACKUP_DIR}\\)"
                + _detach_pointer(restored)
            )
        if not _posix_eq(admin.parent.parent, repo / ".git"):
            return "restored, but the saved admin path is not inside that repository"
        if admin.exists() and not (
            _admin_owned_by(admin, restored) and _same_head(admin, backup / "admin")
        ):
            # Not the entry we removed: `git worktree add` reused the freed admin name for
            # ANOTHER worktree (A3 review, F2), or another worktree was added at this very path
            # and later deleted, leaving its own HEAD and index here (A3b review, N6). Repairing
            # would mix the two. Write nothing, keep our backup, and detach our pointer so git
            # run in this folder cannot read or write that other worktree's index and HEAD.
            return (
                f"restored, but {admin} now belongs to another worktree; nothing was written "
                f"(the saved admin data stays in {ADMIN_BACKUP_DIR}\\ for a manual repair)"
                + _detach_pointer(restored)
            )
        if not admin.exists():
            admin.parent.mkdir(exist_ok=True)  # `.git/worktrees`, inside a repository we checked
            assert_not_real_profile_under_pytest(
                admin, operation="restore a worktree admin dir into"
            )
            shutil.copytree(backup / "admin", admin)
        shutil.rmtree(backup)
    except (OSError, ValueError, KeyError) as exc:
        return f"restored, but the git admin dir could not be put back ({exc})"
    # `repair` rewrites EVERY linked worktree of the repository into the configured path style:
    # with `worktree.useRelativePaths=true` it rewrote another worktree's `.git` file and admin
    # `gitdir` (A4 review, F3). With the setting forced off it only fixes links that are broken.
    result = run(
        ("-c", "worktree.useRelativePaths=false", "worktree", "repair", str(restored)), repo
    )
    if result.rc != 0:
        return (
            f"restored, but git worktree repair failed (rc {result.rc}): {result.err.strip()[:160]}"
        )
    listed = _list_worktrees(run, repo)
    if listed is None or not any(_posix_eq(w.path, restored) for w in listed):
        return "restored, but git does not list the worktree after repair"
    return None


def is_agent_candidate(category_group: str, category: str) -> bool:
    """A candidate counts as an agent artifact by its group OR its category: a hand-built
    candidate that lies about one of them must not slip past the apply-time re-proof."""
    return category_group == GROUP or category in CATEGORIES


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
        exclusion_patterns=tuple(safety.exclusion_patterns),
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
        elif _path_excluded(env, owner) or find_repo_of_worktree(owner) is not None:
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
