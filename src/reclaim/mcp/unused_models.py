"""`find_unused_models`: Hugging Face models in the local cache that no project file names.

docs/specs/assistant-mcp.md section 4 / run spec Phase C. Read-only: lists the cache's model
folders, then reads small text files of the user's projects looking for each model's id
(`org/name`). Nothing is deleted or offered for deletion here; a model cache is not one of the
cleanup categories, so the assistant can only report this and the user decides.

Exclusions are decided first and end the walk: an excluded project's files are never opened.
That has a consequence the result states plainly: a model used only by an excluded project
looks unreferenced, so every result carries `excluded_projects_not_read`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

_TEXT_SUFFIXES = frozenset(
    {".py", ".ipynb", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".txt", ".md", ".sh"}
)
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        "site-packages",
        # Not projects: program data and caches (the model cache itself among them).
        "appdata",
        ".cache",
    }
)
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_FILES = 50_000  # a bound on the read: the report says when it was reached


@dataclass(frozen=True, slots=True)
class CachedModel:
    repo_id: str
    folder: str
    size_bytes: int
    referenced_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelReport:
    cache_dir: str
    models: tuple[CachedModel, ...]
    files_read: int
    stopped_at_limit: bool
    excluded_projects_not_read: bool


def _tree_bytes(root: str) -> int:
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def cached_models(hub: Path, excluded: Callable[[Path], bool]) -> list[tuple[str, str]]:
    """`(repo_id, folder)` for every `models--<org>--<name>` folder in the hub cache, except
    those whose own path matches an exclusion (a model named after an excluded project is not
    sized or reported)."""
    out = []
    try:
        with os.scandir(hub) as it:
            for entry in it:
                if (
                    entry.is_dir()
                    and entry.name.startswith("models--")
                    and not excluded(Path(entry.path))
                ):
                    parts = entry.name[len("models--") :].split("--")
                    out.append(("/".join(parts), entry.path))
    except OSError:
        return []
    return sorted(out)


def _project_files(roots: Iterable[Path], excluded: Callable[[Path], bool]) -> Iterable[Path]:
    for root in roots:
        if excluded(root):
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in _SKIP_DIRS and not excluded(path):
                            stack.append(path)
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and path.suffix.casefold() in _TEXT_SUFFIXES
                        and not excluded(path)
                        and entry.stat(follow_symlinks=False).st_size <= _MAX_FILE_BYTES
                    ):
                        yield path
                except OSError:
                    continue


def find_unused_models(
    hub: Path, project_roots: Iterable[Path], excluded: Callable[[Path], bool]
) -> ModelReport:
    """Every cached model with the project files that name it (empty: none found).

    `excluded(path)` is the user's exclusion check on a path's text; it is asked before any
    directory is listed or file opened, and a hit skips that subtree whole."""
    models = cached_models(hub, excluded)
    refs: dict[str, list[str]] = {repo: [] for repo, _ in models}
    needles = {repo: repo.casefold() for repo in refs}
    read = 0
    stopped = False
    for path in _project_files(project_roots, excluded):
        if read >= _MAX_FILES:
            stopped = True
            break
        read += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        except OSError:
            continue
        for repo, needle in needles.items():
            if needle in text and len(refs[repo]) < 5:
                refs[repo].append(str(path))
    return ModelReport(
        cache_dir=str(hub),
        models=tuple(
            CachedModel(repo, folder, _tree_bytes(folder), tuple(refs[repo]))
            for repo, folder in models
        ),
        files_read=read,
        stopped_at_limit=stopped,
        excluded_projects_not_read=True,
    )


def models_hub_dir(environ: Mapping[str, str]) -> Path:
    """Where huggingface_hub keeps its model cache: HF_HUB_CACHE, else HF_HOME/hub, else
    ~/.cache/huggingface/hub (the library's own order of precedence)."""
    if environ.get("HF_HUB_CACHE"):
        return Path(environ["HF_HUB_CACHE"])
    if environ.get("HF_HOME"):
        return Path(environ["HF_HOME"]) / "hub"
    home = Path(environ.get("USERPROFILE") or Path.home())
    return home / ".cache" / "huggingface" / "hub"
