from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reclaim.models import FileRecord, Verdict

_MANIFEST_PATH = Path(__file__).with_name("golden_tree.json")
_GIT_USER_EMAIL = "fixture@reclaim.test"
_GIT_USER_NAME = "Reclaim Fixture Builder"

# The only Windows attribute bit settable through stdlib without ctypes/OneDrive. Cloud
# placeholder (0x400000) and other reparse-driven bits aren't real filesystem toggles here,
# so the manifest's `attributes` value is passed straight into FileRecord instead of being
# re-derived from a disk read (SafetyValidator never reads disk itself — see safety.py).
_FILE_ATTRIBUTE_READONLY = 0x1


@dataclass(frozen=True, slots=True)
class FixtureCase:
    """One materialized golden-tree entry, ready to convert into a FileRecord."""

    id: str
    path: Path
    kind: str
    size_bytes: int
    attributes: int
    category: str
    expected_verdict: Verdict
    expected_reason_contains: str
    git_repo_root: Path | None
    git_repo_clean: bool

    def to_file_record(self) -> FileRecord:
        return FileRecord(
            path=self.path,
            is_dir=self.kind == "dir",
            size_bytes=self.size_bytes,
            attributes=self.attributes,
            ext=self.path.suffix.lower(),
            git_repo_root=self.git_repo_root,
            git_repo_clean=self.git_repo_clean,
        )


def _load_manifest() -> list[dict[str, Any]]:
    with _MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)
    return entries


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    git_exe = shutil.which("git")
    if git_exe is None:
        raise RuntimeError("git executable not found on PATH; required to build the golden tree")
    return subprocess.run(  # noqa: S603 -- fixed fixture-builder args, not untrusted input
        [git_exe, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _materialize(entry: dict[str, Any], root: Path) -> None:
    target = root / entry["relative_path"]
    if entry["kind"] == "dir":
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"reclaim-fixture\n")
    os.truncate(target, entry["size_bytes"])
    if entry["attributes"] & _FILE_ATTRIBUTE_READONLY:
        target.chmod(0o444)


def _init_git_repo(repo_dir: Path, *, dirty: bool) -> None:
    _run_git(["init", "--quiet"], cwd=repo_dir)
    _run_git(["add", "-A"], cwd=repo_dir)
    # Identity scoped via -c to this single commit invocation only — no `git config` call at
    # all, local or global, ever writes a config file for this. See scripts/git_guard.py's
    # docstring for why this repo prefers -c over even repo-local `git config` where possible.
    _run_git(
        [
            "-c",
            f"user.email={_GIT_USER_EMAIL}",
            "-c",
            f"user.name={_GIT_USER_NAME}",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "chore: fixture baseline",
        ],
        cwd=repo_dir,
    )
    if dirty:
        marker = repo_dir / "_dirty_marker.txt"
        marker.write_text("uncommitted change for fixture\n", encoding="utf-8")


def _is_repo_clean(repo_dir: Path) -> bool:
    result = _run_git(["status", "--porcelain"], cwd=repo_dir)
    return result.stdout.strip() == ""


def _find_repo_root(entry_relative_path: str, repo_roots: list[str]) -> str | None:
    entry_parts = Path(entry_relative_path).parts
    best: str | None = None
    for candidate in repo_roots:
        candidate_parts = Path(candidate).parts
        is_prefix = entry_parts[: len(candidate_parts)] == candidate_parts
        if is_prefix and (best is None or len(candidate_parts) > len(Path(best).parts)):
            best = candidate
    return best


def build_golden_tree(root: Path) -> list[FixtureCase]:
    """Materialize the golden fixture tree under `root` and return parsed cases.

    Never touches anything outside `root`. Git repos are real (`git init` + a baseline
    commit, optionally left dirty) so `git_repo_clean` reflects actual `git status`
    output rather than just the manifest's stated intent.
    """
    entries = _load_manifest()
    repo_entries = [e for e in entries if e.get("is_git_repo_root")]
    repo_roots = [e["relative_path"] for e in repo_entries]

    for entry in entries:
        _materialize(entry, root)

    repo_clean: dict[str, bool] = {}
    for entry in repo_entries:
        repo_dir = root / entry["relative_path"]
        _init_git_repo(repo_dir, dirty=bool(entry.get("git_dirty", False)))
        repo_clean[entry["relative_path"]] = _is_repo_clean(repo_dir)

    cases: list[FixtureCase] = []
    for entry in entries:
        repo_rel = _find_repo_root(entry["relative_path"], repo_roots)
        git_repo_root = root / repo_rel if repo_rel is not None else None
        git_clean = repo_clean[repo_rel] if repo_rel is not None else False
        cases.append(
            FixtureCase(
                id=entry["id"],
                path=root / entry["relative_path"],
                kind=entry["kind"],
                size_bytes=entry["size_bytes"],
                attributes=entry["attributes"],
                category=entry["category"],
                expected_verdict=Verdict(entry["expected_verdict"]),
                expected_reason_contains=entry["expected_reason_contains"],
                git_repo_root=git_repo_root,
                git_repo_clean=git_clean,
            )
        )
    return cases
