from __future__ import annotations

# ruff: noqa: PTH100, PTH118, PTH120
# Deliberate str-based os.path handling: realpath of a not-yet-existing path, long-path prefixes
# and case-folded string comparison are the point of this module.
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# Hermetic-test guard (CLAUDE.md rule 19; ADR-0034 addendum "Hermetic tests"). A verifier probe
# once ran a `regenerable` apply under pytest with `local_appdata`/`home` still pointing at the
# REAL machine and deleted the owner's real browser caches. These functions make that
# structurally impossible a second time: under pytest, every destructive choke point refuses a
# target that resolves under the REAL user's profile roots. Outside pytest they are a no-op.

OPT_IN_ENV_VAR = "RECLAIM_TEST_ALLOW_REAL_PROFILE"

# Env vars whose value is a real-machine root. Captured ONCE (first call wins) so that a later
# redirect of these variables (the tests' autouse fixture) cannot change what "real" means.
_ROOT_ENV_VARS = (
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "HOME",
    "TEMP",
    "TMP",
    "PROGRAMDATA",
)

_real_roots: tuple[str, ...] | None = None
_sandbox_roots: list[str] = []


class RealProfileAccessError(BaseException):
    """Raised when a destructive operation is attempted against the real profile (or a real
    process is spawned) while running under pytest.

    Deliberately a `BaseException`: this code base has many `except Exception` isolation
    boundaries (`regenerable._guarded`, the executor's per-item handler) that would otherwise
    downgrade the refusal to a "failed item" and let the run carry on to the next target. This
    must stop the whole test, loudly. It is never raised outside pytest."""


def running_under_pytest() -> bool:
    """Fail-closed: either signal is enough. Tests replace this function to simulate production."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _opted_in() -> bool:
    return os.environ.get(OPT_IN_ENV_VAR) == "1"


def _norm(path: str | os.PathLike[str], *, link_itself: bool = False) -> str:
    text = os.fspath(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    if link_itself:
        # A junction/symlink entry: only the entry is removed, never its target, so resolve the
        # parent and keep the entry name unresolved.
        head, tail = os.path.split(os.path.abspath(text))
        text = os.path.join(os.path.realpath(head), tail)
    else:
        text = os.path.realpath(text)
    return os.path.normcase(text).rstrip("\\/")


def _within(target: str, root: str) -> bool:
    return target == root or target.startswith(root + os.sep)


def capture_real_profile_roots(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Records the real machine roots, once. Later calls return the first capture. Call it at
    conftest import, before any redirect; the guard also calls it lazily (best effort) when a
    run under pytest never imported the conftest."""
    global _real_roots  # process-wide, capture-once by design
    if _real_roots is not None:
        return _real_roots
    env = os.environ if environ is None else environ
    candidates: list[str] = [env[v] for v in _ROOT_ENV_VARS if env.get(v)]
    if env.get("HOMEDRIVE") and env.get("HOMEPATH"):
        candidates.append(env["HOMEDRIVE"] + env["HOMEPATH"])
    if env.get("LOCALAPPDATA"):
        candidates.append(os.path.join(env["LOCALAPPDATA"], "Temp"))
    if env.get("SYSTEMROOT"):
        candidates.append(os.path.join(env["SYSTEMROOT"], "Temp"))
    candidates.append(str(Path.home()))
    roots: list[str] = []
    for candidate in candidates:
        norm = _norm(candidate)
        # A drive root can never be a sensible "profile root"; including it would refuse everything.
        if os.path.dirname(norm) != norm and norm not in roots:
            roots.append(norm)
    _real_roots = tuple(roots)
    return _real_roots


def real_profile_roots() -> tuple[str, ...]:
    """The captured real roots (normalized, case-folded strings)."""
    return capture_real_profile_roots()


def register_sandbox_root(path: str | os.PathLike[str]) -> None:
    """Marks a directory (pytest's basetemp) as test-owned: it lives under the real TEMP, but
    destroying things inside it is exactly what tests are allowed to do."""
    norm = _norm(path)
    if norm not in _sandbox_roots:
        _sandbox_roots.append(norm)


def assert_not_real_profile_under_pytest(
    path: str | os.PathLike[str], *, operation: str = "delete", link_itself: bool = False
) -> None:
    """No-op in production. Under pytest, raises `RealProfileAccessError` when `path` resolves
    under a real profile root and not under a registered test sandbox, unless
    `RECLAIM_TEST_ALLOW_REAL_PROFILE=1` (deliberate manual use only). Resolution failure refuses."""
    if not running_under_pytest() or _opted_in():
        return
    try:
        target = _norm(path, link_itself=link_itself)
    except (OSError, ValueError, TypeError) as exc:
        raise RealProfileAccessError(
            f"refusing to {operation} {path!r} under pytest: cannot resolve it ({exc!r})"
        ) from exc
    if any(_within(target, s) for s in _sandbox_roots):
        return
    for root in real_profile_roots():
        if _within(target, root):
            raise RealProfileAccessError(
                f"refusing to {operation} {target!r} under pytest: it is inside the REAL profile "
                f"root {root!r}. Tests must redirect every root (LOCALAPPDATA, APPDATA, TEMP, "
                f"USERPROFILE, ...) to a tmp dir; set {OPT_IN_ENV_VAR}=1 only for a deliberate "
                f"manual run."
            )


def refuse_real_side_effect_under_pytest(what: str) -> None:
    """For operations that cannot be path-checked (spawning schtasks/powershell/uv/pip/npm, a
    real toast): refused under pytest unless opted in. Tests inject a fake runner instead."""
    if running_under_pytest() and not _opted_in():
        raise RealProfileAccessError(
            f"refusing to {what} under pytest: the real runner must never execute in a test "
            f"(inject a fake runner; {OPT_IN_ENV_VAR}=1 is for deliberate manual use only)"
        )
