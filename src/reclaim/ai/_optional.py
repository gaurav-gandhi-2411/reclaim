from __future__ import annotations

import importlib
from types import ModuleType


class AIExtraNotInstalledError(ImportError):
    """Raised in place of a raw ImportError/ModuleNotFoundError when an AI-layer function
    needs an optional dependency that isn't installed — carries an actionable message
    instead of a stack trace pointing at some third-party import line."""


def require(module_name: str, *, feature: str) -> ModuleType:
    """Imports `module_name` lazily, inside the function that actually needs it — never at
    module load time, so `import reclaim.ai.<anything>` always succeeds regardless of
    whether the `ai` extra is installed. `feature` is a short human-readable name for what
    the caller was trying to do (used only in the error message).

    Deliberately re-raises as `AIExtraNotInstalledError` (an `ImportError` subclass, so
    existing `except ImportError` handling upstream still works) rather than letting the
    original `ModuleNotFoundError` propagate — a first-time user hitting a bare stack trace
    on `ModuleNotFoundError: No module named 'cv2'` has no idea this is an optional extra;
    this message tells them exactly what to run.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise AIExtraNotInstalledError(
            f"{feature} needs the optional '{module_name}' package, which isn't installed. "
            "Install the AI extras: `uv sync --extra ai` (or `pip install reclaim[ai]`)."
        ) from exc
