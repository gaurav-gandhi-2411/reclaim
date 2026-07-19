from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys

import pytest

import reclaim.ai
from reclaim.ai._optional import AIExtraNotInstalledError, require

# Hard Gate 3: the core deterministic tool must install and run WITHOUT the `ai` extra's
# dependencies present. Tests here cover both install profiles: (1) `require()`'s own
# behavior when a dependency is genuinely missing/present, and (2) that importing the core
# CLI and every `reclaim.ai` submodule succeeds even when the heavy optional dependencies
# are unavailable — proven by simulating their absence via import-blocking, so this test is
# meaningful regardless of whether this environment actually has the `ai` extra installed.


def test_require_raises_actionable_error_when_module_missing() -> None:
    with pytest.raises(AIExtraNotInstalledError, match=r"nonexistent thing.*uv sync --extra ai"):
        require("this_module_definitely_does_not_exist_xyz123", feature="a nonexistent thing")


def test_require_error_is_an_import_error_subclass() -> None:
    """Existing `except ImportError` handling upstream (if any) keeps working — this is a
    more specific, more actionable error, not a different error family."""
    assert issubclass(AIExtraNotInstalledError, ImportError)


def test_require_returns_the_real_module_when_present() -> None:
    module = require("os", feature="a stdlib sanity check")
    assert module is __import__("os")


# Every module name any `reclaim.ai.*` call site has ever passed to `_optional.require()` —
# kept as one explicit set so a future feature adding a new optional dependency (and a new
# `require("whatever", ...)` call site) is forced to add it here too, rather than this
# fixture silently under-blocking and the "core tool degrades cleanly" guarantee quietly
# eroding. `test_every_require_call_site_module_is_covered_by_the_block_list` (below) is the
# structural backstop that catches a future call site NOT added here, so this can't drift
# silently either.
_ALL_GATED_MODULE_NAMES = {
    "cv2",
    "imagehash",
    "PIL",
    "PIL.Image",
    "docx",
    "pypdf",
    "datasketch",
    "sentence_transformers",
    "numpy",
    "rapidocr_onnxruntime",
    "open_clip",
    "torch",
    "faiss",
}


@pytest.fixture
def block_ai_extra_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a "no `ai` extra installed" environment by making EVERY optional AI
    dependency unimportable regardless of what's actually installed in THIS environment —
    makes the "core tool works without the AI extras" guarantee testable without needing two
    separate real venvs, and durable even after the ai extra is installed here for feature
    work.

    Patches `importlib.import_module` specifically (not `builtins.__import__`) — that's what
    `reclaim.ai._optional.require` actually calls, and `importlib.import_module` does NOT
    route through `builtins.__import__` internally (a first version of this fixture patched
    the wrong hook and silently never intercepted anything once the real packages were
    installed for feature-1a work — this is that fix)."""
    blocked = _ALL_GATED_MODULE_NAMES
    real_import_module = importlib.import_module

    def _fake_import_module(name: str, *args: object, **kwargs: object) -> object:
        if name in blocked or name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"simulated: {name!r} not installed")
        return real_import_module(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)
    for name in list(sys.modules):
        if name in blocked or name.split(".")[0] in blocked:
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_core_cli_imports_fine_without_ai_extras(block_ai_extra_imports: None) -> None:
    importlib.import_module("reclaim.cli")


def _discover_ai_submodules() -> list[str]:
    """Every `reclaim.ai.*` submodule, discovered via `pkgutil` rather than a hand-maintained
    list — the exact bug class this replaces: the original version of this test hardcoded 5
    submodule names and silently never grew to cover `screenshot_burst`/`screenshot_ocr`/
    `content_tagger`/`screenshot_review`/`feedback_store`/`cold_start_priority` (Feature 2/3),
    so a module that eagerly imported a heavy optional dependency at load time could have
    shipped without this test ever exercising it."""
    return [
        f"reclaim.ai.{info.name}"
        for info in pkgutil.iter_modules(reclaim.ai.__path__)
        if not info.name.startswith("_")
    ]


def test_ai_package_imports_fine_without_ai_extras(block_ai_extra_imports: None) -> None:
    """`import reclaim.ai` and EVERY current submodule (discovered, not hardcoded — see
    `_discover_ai_submodules`) must never eagerly import a heavy optional dependency at
    module load time — only a function that actually NEEDS one, called explicitly, may fail,
    and only with the actionable `AIExtraNotInstalledError`, never a raw `ModuleNotFoundError`
    or `ImportError` propagating from a top-level `import cv2`-style statement."""
    importlib.import_module("reclaim.ai")
    for module_name in _discover_ai_submodules():
        importlib.import_module(module_name)


def test_every_require_call_site_module_is_covered_by_the_block_list() -> None:
    """Structural backstop for `_ALL_GATED_MODULE_NAMES` itself: greps every `reclaim.ai.*`
    source file for `require("...", ...)` call sites and fails if any referenced module name
    is missing from the block list above — so a FUTURE feature adding a new optional
    dependency and forgetting to update this test's block list fails loudly here, rather than
    this whole test file silently under-blocking and passing anyway."""
    import ast
    from pathlib import Path

    ai_root = Path(reclaim.ai.__file__).resolve().parent
    referenced: set[str] = set()
    for py_file in ai_root.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "require"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                referenced.add(node.args[0].value)

    assert referenced, "expected to find at least one require(...) call site under reclaim.ai"
    missing = referenced - _ALL_GATED_MODULE_NAMES
    assert missing == set(), (
        f"require() is called with module name(s) {missing} that aren't in "
        "_ALL_GATED_MODULE_NAMES — add them so block_ai_extra_imports actually blocks them"
    )


def test_require_surfaces_the_actionable_error_not_a_raw_import_error(
    block_ai_extra_imports: None,
) -> None:
    with pytest.raises(AIExtraNotInstalledError, match="uv sync --extra ai"):
        require("cv2", feature="sharpness scoring")


@pytest.mark.skipif(
    importlib.util.find_spec("cv2") is None, reason="ai extras not installed in this env"
)
def test_require_returns_the_real_cv2_when_ai_extras_are_genuinely_installed() -> None:
    """The other half of the "with extras" profile — proven against whatever this
    environment actually has installed (skipped, not faked, when it doesn't), complementing
    the simulated-absence tests above."""
    cv2 = require("cv2", feature="sharpness scoring")
    assert hasattr(cv2, "Laplacian")
