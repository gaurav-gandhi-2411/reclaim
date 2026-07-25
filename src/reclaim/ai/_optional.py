from __future__ import annotations

import importlib
from types import ModuleType


class AIExtraNotInstalledError(ImportError):
    """Raised in place of a raw ImportError/ModuleNotFoundError when an AI-layer function
    needs an optional dependency that isn't installed — carries an actionable message
    instead of a stack trace pointing at some third-party import line."""


# D15: Pillow's own default (`PIL.Image.MAX_IMAGE_PIXELS`, ~89.5M px) only WARNS past 1x that
# value and doesn't hard-fail until 2x (~179M px) — every image this app opens (phash.py's
# `compute_image_hashes`, image_embeddings.py's `compute_image_embedding`) comes from scanning
# an arbitrary user disk, so a crafted or corrupt file with an inflated header could still force
# a huge in-memory decode before that warning-only path is ever hit. Pinned lower here so
# `PIL.Image.DecompressionBombError` raises deterministically — well before pixel decode, inside
# `Image.open()` itself — for any image whose declared dimensions exceed this many pixels
# (~an 8000x8000 image; comfortably above any real photo/scan this app scans in practice).
_MAX_IMAGE_PIXELS = 64_000_000


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
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AIExtraNotInstalledError(
            f"{feature} needs the optional '{module_name}' package, which isn't installed. "
            "From a source checkout: `uv sync --extra ai`. There is no way to add this to an "
            "installed (setup.exe) copy of Reclaim yet -- see ADR-0029."
        ) from exc

    if module_name == "PIL.Image":
        # Centralized so every current AND future `require("PIL.Image", ...)` call site gets
        # the decompression-bomb cap for free — both existing call sites (phash.py,
        # image_embeddings.py) already wrap `Image.open()` in a broad `except Exception: return
        # None`, so this raises straight into that existing "unusable image, skip it" path with
        # no caller changes needed. Re-assigning on every call is idempotent (plain attribute
        # set on the already-imported module) and cheap enough not to special-case away.
        # `ModuleType` has no `MAX_IMAGE_PIXELS` attribute in its stub — same untyped-dynamic-
        # attribute shape as `image_embeddings.py`'s own `# type: ignore[attr-defined]` on
        # `model.encode_image(...)`, just on a module object instead of a model instance.
        module.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS  # type: ignore[attr-defined]

    return module
