from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest

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


@pytest.fixture
def block_ai_extra_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a "no `ai` extra installed" environment by making `cv2`/`imagehash`
    unimportable regardless of what's actually installed in THIS environment — makes the
    "core tool works without the AI extras" guarantee testable without needing two separate
    real venvs, and durable even after the ai extra is installed here for feature work.

    Patches `importlib.import_module` specifically (not `builtins.__import__`) — that's what
    `reclaim.ai._optional.require` actually calls, and `importlib.import_module` does NOT
    route through `builtins.__import__` internally (a first version of this fixture patched
    the wrong hook and silently never intercepted anything once the real packages were
    installed for feature-1a work — this is that fix)."""
    blocked = {"cv2", "imagehash"}
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


def test_ai_package_imports_fine_without_ai_extras(block_ai_extra_imports: None) -> None:
    """`import reclaim.ai` (and every current submodule) must never eagerly import cv2/
    imagehash — only a function that actually NEEDS one, called explicitly, may fail, and
    only with the actionable AIExtraNotInstalledError, never a raw ModuleNotFoundError."""
    importlib.import_module("reclaim.ai")
    importlib.import_module("reclaim.ai.models")
    importlib.import_module("reclaim.ai.review_queue")
    importlib.import_module("reclaim.ai.safety")
    importlib.import_module("reclaim.ai.eval_harness")


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
