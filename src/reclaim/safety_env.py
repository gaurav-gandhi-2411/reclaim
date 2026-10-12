from __future__ import annotations

# ruff: noqa: PTH100, PTH118, PTH120
# Deliberate str-based os.path handling: realpath of a not-yet-existing path, long-path prefixes
# and case-folded string comparison are the point of this module.
import ipaddress
import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path

# Hermetic-test guard (CLAUDE.md rule 7; ADR-0034 addendum "Hermetic tests"). A verifier probe
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

# A child process a test spawns (e.g. the crash-recovery harness) inherits PYTEST_CURRENT_TEST, so
# the guard is active there too -- but it never imported the conftest and its inherited
# environment is already the REDIRECTED one. The parent therefore publishes the real roots and the
# sandbox in these variables, and a child reads them instead of re-deriving "real" from a
# redirected environment.
_INHERIT_REAL_ENV_VAR = "RECLAIM_TEST_REAL_ROOTS"
_INHERIT_SANDBOX_ENV_VAR = "RECLAIM_TEST_SANDBOX_ROOTS"

_real_roots: tuple[str, ...] | None = None
_sandbox_roots: list[str] = []
_sandbox_inherited = False


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


_IPV6_LITERAL_SUFFIX = ".ipv6-literal.net"
_BS = "\\"


def _is_this_machine(host: str) -> bool:
    """True for every spelling of "this computer" a UNC path can use: localhost, any loopback IP
    (127.x, [::1], 0--1.ipv6-literal.net) and the machine's own host name."""
    host = host.strip().strip("[]").rstrip(".").lower()
    if not host:
        return False
    if host.endswith(_IPV6_LITERAL_SUFFIX):  # Windows' way to spell an IPv6 address in a UNC
        host = host[: -len(_IPV6_LITERAL_SUFFIX)].replace("-", ":").split("s")[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    own = {socket.gethostname().lower(), os.environ.get("COMPUTERNAME", "").lower()}
    own.discard("")
    return host in own


def _map_to_drive_path(text: str) -> str:
    r"""Rewrites the Win32 prefixes and loopback admin-share spellings that all name a LOCAL drive
    path (`\\?\C:\x`, `\\.\C:\x`, `\\?\UNC\localhost\C$\x`, `\\localhost\C$\x`,
    `\\<this host>\C$\x`, ...) to `C:\x`, so a real root cannot be bypassed through them.
    Anything it cannot map to a drive letter is returned still starting with `\\` (the caller
    treats that as unresolved and fails closed)."""
    text = text.replace("/", _BS)
    for _ in range(4):  # a prefix can wrap another one; stop when stable
        before = text
        if text[:8].upper() == _BS * 2 + "?" + _BS + "UNC" + _BS:
            text = _BS * 2 + text[8:]
        elif text[:4] in (_BS * 2 + "?" + _BS, _BS * 2 + "." + _BS) and text[5:6] == ":":
            text = text[4:]
        elif text.startswith(_BS * 2) and text[2:3] not in ("?", "."):
            parts = text[2:].split(_BS, 2)
            if (
                len(parts) >= 2
                and len(parts[1]) == 2
                and parts[1][0].isalpha()
                and parts[1][1] == "$"
                and _is_this_machine(parts[0])
            ):
                text = parts[1][0] + ":" + _BS + (parts[2] if len(parts) > 2 else "")
        if text == before:
            break
    return text


def _is_unresolved(norm_text: str) -> bool:
    """A normalized path still shaped like a device/UNC path (not mappable to a drive letter)."""
    return norm_text.startswith(_BS * 2)


def _norm(path: str | os.PathLike[str], *, link_itself: bool = False) -> str:
    text = _map_to_drive_path(os.fspath(path))
    if _is_unresolved(text):
        # No drive letter to anchor on (GLOBALROOT, Volume{...}, a foreign UNC share): do NOT
        # realpath it (could touch the network); the caller refuses it if it names a real root.
        return os.path.normcase(text).rstrip("\\/")
    if link_itself:
        # A junction/symlink entry: only the entry is removed, never its target, so resolve the
        # parent and keep the entry name unresolved.
        head, tail = os.path.split(os.path.abspath(text))
        text = os.path.join(os.path.realpath(head), tail)
    else:
        text = os.path.realpath(text)
    # realpath of an existing path can itself return a `\\?\` form; map that back too.
    return os.path.normcase(_map_to_drive_path(text)).rstrip("\\/")


def _names_real_root(norm_text: str, roots: tuple[str, ...]) -> str | None:
    r"""For an unresolved path: the real root whose drive-less part (`\users\me\...`) appears in
    its text, else None. Conservative on purpose: a false positive refuses a delete under pytest,
    a false negative deletes the owner's files."""
    haystack = norm_text + _BS
    for root in roots:
        tail = root[2:] if root[1:2] == ":" else root
        if tail and (tail + _BS) in haystack:
            return root
    return None


def _within(target: str, root: str) -> bool:
    return target == root or target.startswith(root + os.sep)


def capture_real_profile_roots(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Records the real machine roots, once. Later calls return the first capture. Call it at
    conftest import, before any redirect; the guard also calls it lazily (best effort) when a
    run under pytest never imported the conftest."""
    global _real_roots  # process-wide, capture-once by design
    if _real_roots is not None:
        return _real_roots
    inherited = os.environ.get(_INHERIT_REAL_ENV_VAR)
    if environ is None and inherited:
        _real_roots = tuple(r for r in inherited.split(os.pathsep) if r)
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
    if environ is None:
        os.environ[_INHERIT_REAL_ENV_VAR] = os.pathsep.join(_real_roots)
    return _real_roots


def real_profile_roots() -> tuple[str, ...]:
    """The captured real roots (normalized, case-folded strings)."""
    return capture_real_profile_roots()


def register_sandbox_root(path: str | os.PathLike[str]) -> None:
    """Marks a directory (pytest's basetemp) as test-owned: it lives under the real TEMP, but
    destroying things inside it is exactly what tests are allowed to do."""
    global _sandbox_inherited  # a registration supersedes anything inherited
    _sandbox_inherited = True
    norm = _norm(path)
    if norm not in _sandbox_roots:
        _sandbox_roots.append(norm)
    # Rewritten on EVERY call (not only the first): a test that swaps the list out must not leave
    # a stale value in the environment for the next test's children.
    os.environ[_INHERIT_SANDBOX_ENV_VAR] = os.pathsep.join(_sandbox_roots)


def _acceptable_sandboxes(raw: str) -> list[str]:
    r"""Normalizes the inherited sandbox list (split on os.pathsep only, empties ignored,
    case-folded like every other path here). An entry that is, contains or is a parent of a real
    root (`C:\`, `C:\Users`, the profile itself) is dropped: it would whitelist the very tree the
    guard protects, and this variable is plain environment text any process can set. A sandbox
    INSIDE a real root (pytest's basetemp under the real TEMP) is the normal case and is kept."""
    roots = real_profile_roots()
    kept: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry.strip():
            continue
        try:
            norm = _norm(entry)
        except (OSError, ValueError):
            continue  # unresolvable: not a sandbox (fail closed)
        if not norm or _is_unresolved(norm) or any(_within(root, norm) for root in roots):
            continue
        if norm not in kept:
            kept.append(norm)
    return kept


def _sandboxes() -> list[str]:
    global _sandbox_inherited  # load the parent's sandbox once, only in a fresh child process
    if not _sandbox_inherited:
        _sandbox_inherited = True
        inherited = os.environ.get(_INHERIT_SANDBOX_ENV_VAR, "")
        _sandbox_roots.extend(_acceptable_sandboxes(inherited))
    return _sandbox_roots


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
    roots = real_profile_roots()
    if _is_unresolved(target):
        # Never a sandbox, never resolvable: refuse when its text still names a real root.
        named = _names_real_root(target, roots)
        if named is not None:
            raise RealProfileAccessError(
                f"refusing to {operation} {target!r} under pytest: it is a device/UNC path that "
                f"cannot be mapped to a drive letter and its text names the REAL profile root "
                f"{named!r}. Use a plain drive-letter path inside the test's tmp dir."
            )
        return
    if any(_within(target, s) for s in _sandboxes()):
        return
    for root in roots:
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
