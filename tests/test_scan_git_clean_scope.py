"""The scan runs git's clean check only where a safety verdict can read the answer.

Build-12 scan slowdown (2026-10-31): three git processes per repo root, for every repo the walk
met, cost most of a profile scan, yet `git_repo_clean` is read by one rule only (the
dev-artifacts `node_modules` exemption). These tests pin both halves of that trade: the git
check is skipped elsewhere, and skipping it can never change a verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import reclaim.scanner as scanner_module
from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig
from reclaim.index import ScanIndex
from reclaim.models import FileRecord
from reclaim.safety import SafetyValidator
from reclaim.scanner import needs_git_clean_status, scan_tree

_GIT = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run([*_GIT, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603


def _committed_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True)
    _git(["init", "--quiet"], repo)
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "--quiet", "-m", "base"], repo)


def _scan(root: Path, db: Path) -> dict[Path, FileRecord]:
    with ScanIndex(db) as index:
        scan_tree(root, index, incremental=False)
        return {r.path: r for r in index.full_inventory(under=root)}


@pytest.mark.parametrize("listing", [True, False], ids=["listing", "scandir"])
def test_git_runs_only_for_repos_with_a_record_a_verdict_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: bool
) -> None:
    monkeypatch.setattr(scanner_module, "_USE_DIRECTORY_LISTING", listing)
    root = tmp_path / "root"
    for n in range(4):
        _committed_repo(root / f"plain{n}", {"a.txt": "a\n", "src/b.py": "b\n"})
    _committed_repo(root / "web", {"a.txt": "a\n", "node_modules/pkg/index.js": "x\n"})
    asked: list[Path] = []
    real = scanner_module._query_git_clean

    def counting(repo_root: Path, _depth: int = 0, excluded_patterns: object = ()) -> bool:
        asked.append(repo_root)
        return real(repo_root, _depth, excluded_patterns)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner_module, "_query_git_clean", counting)
    records = _scan(root, tmp_path / "idx.sqlite3")

    assert asked == [root / "web"]  # once, memoised; never for the four plain repos
    assert records[root / "web" / "node_modules"].git_repo_clean is True
    assert records[root / "web" / "a.txt"].git_repo_clean is False
    assert records[root / "plain0" / "src" / "b.py"].git_repo_root == root / "plain0"
    assert records[root / "plain0" / "src" / "b.py"].git_repo_clean is False


def test_node_modules_record_still_reads_a_dirty_repo_as_not_clean(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _committed_repo(root / "web", {"a.txt": "a\n", "node_modules/pkg/index.js": "x\n"})
    (root / "web" / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    records = _scan(root, tmp_path / "idx.sqlite3")
    assert records[root / "web" / "node_modules"].git_repo_clean is False


_PATHS = [
    "C:/R/src/main.py",
    "C:/R/node_modules",
    "C:/R/Node_Modules/pkg/index.js",
    "C:/R/NODE_MODULES/x",
    "C:/R/a/node_modules",  # a file or dir named exactly node_modules
    "C:/R/node_modules.bak/x.js",
    "C:/R/my_node_modules/x.js",
    "C:/R/node_module/x.js",
    "C:/R/nodemodules/x.js",
    "C:/R/node-modules/x.js",
    "C:/R/Résumé/node_modules/ü.js",
    "C:/R/.venv/Lib/site-packages/x.py",
    "C:/Users/u/AppData/Local/Temp/vignore-1/seed/app/x.ts",
]


@pytest.mark.parametrize("dev_artifacts", [True, False])
@pytest.mark.parametrize("is_dir", [False, True])
@pytest.mark.parametrize("path", _PATHS)
def test_skipping_the_git_check_never_changes_a_verdict(
    path: str, is_dir: bool, dev_artifacts: bool
) -> None:
    """Where `needs_git_clean_status` is False the scan stores `git_repo_clean=False` without
    asking git, so the verdict must be the same whatever git would have said."""
    validator = SafetyValidator(
        Config(categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=dev_artifacts)))
    )

    def verdict(clean: bool) -> tuple[object, object]:
        result = validator.evaluate(
            FileRecord(
                path=Path(path),
                is_dir=is_dir,
                size_bytes=10,
                attributes=0,
                ext="" if is_dir else Path(path).suffix.lower(),
                git_repo_root=Path("C:/R"),
                git_repo_clean=clean,
            )
        )
        return result.verdict, result.reason_code

    if not needs_git_clean_status(path):
        assert verdict(True) == verdict(False)
    elif dev_artifacts and "node_modules" in Path(path).as_posix().lower().split("/"):
        # Teeth: where the rule does read it, the value matters, so the check must run.
        assert verdict(True) != verdict(False)
