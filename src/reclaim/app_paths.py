from __future__ import annotations

from pathlib import Path

# Shared anchor for every `data/`-relative default path in this app (scan index, quarantine
# vault, mode log, first-run marker, the app-wide log file -- see logging_config.DEFAULT_LOG_PATH,
# mode.DEFAULT_MODE_LOG_PATH, first_run.DEFAULT_FIRST_RUN_STATE_PATH, executor.DEFAULT_VAULT_DIR,
# api.app._DEFAULT_VAULT_DIR, mcp.server._DEFAULT_VAULT_DIR).
#
# P0 fix (2026-08-22, live-reproduced under a real frozen install, originally landed in
# logging_config.py alone as PR #51): a bare CWD-relative `Path("data/...")` breaks for any
# invocation path that has no way to set a working directory -- packaging/reclaim.iss's
# [Registry] `reclaim-notify:` URI protocol handler (the disk-space toast's Snooze button) is the
# one CONFIRMED-live case (a `shell\open\command` registry value has no working-directory
# concept, so the process's CWD at launch is whatever the invoking shell context happened to be
# -- observed: `C:\Windows\System32`), but "not reachable today" is a property of today's call
# sites, not of the code: the protocol handler itself was not reachable until R5 registered it.
# Every module building a `data/`-relative default now routes through `data_root()` below so the
# same class of bug cannot recur the next time a new CWD-less entry point is added -- enforced by
# `evals/test_data_path_cwd_independence_gate.py`, not just convention.
#
# Generalized from logging_config.py's original single-module fix to mode.py, first_run.py,
# executor.py, api/app.py, and mcp/server.py (AA1 finding, PR #46's task): all five independently
# constructed the identical bare-relative-Path pattern.


def compiled_exe_dir() -> Path | None:
    """The real compiled executable's own directory when running under Nuitka, or `None`
    otherwise (source/dev/test runs).

    `sys.executable` under Nuitka `--standalone` points at an internal Python shim Nuitka
    creates alongside the real exe, not the compiled exe itself -- it happens to share the exe's
    directory, but `__compiled__.original_argv0` is the field Nuitka actually documents for this
    purpose (confirmed via a real scoped `--standalone` compile, package/submodule layout
    matching this app's own entry_point.py -> cli.py -> logging_config.py chain: `sys.frozen` is
    NOT set by Nuitka -- despite being a common PyInstaller-compatibility assumption -- but the
    bare name `__compiled__` IS resolvable in every module of a compiled program).
    `__compiled__` does not exist as a name at all outside a Nuitka-compiled program, so
    `NameError` is the correct, unambiguous "not frozen" signal -- never a heuristic guess.

    Deliberately NOT `getattr(builtins, "__compiled__", None)`: also confirmed empirically (same
    real compile) that Nuitka does NOT populate this as a real attribute on the `builtins`
    module object -- `getattr` returns `None` even when compiled, silently defeating this whole
    function. The bare name below IS how Nuitka actually exposes it (verified working in that
    same compile) -- mypy has no stub for a dynamically injected builtin, hence the `type:
    ignore` rather than switching to the broken `getattr` form.
    """
    try:
        return Path(__compiled__.original_argv0).resolve().parent  # type: ignore[name-defined]
    except NameError:
        return None


def data_root() -> Path:
    """Anchor for every `data/`-relative default path in this app: the real running
    executable's own directory when compiled under Nuitka, or `Path(".")` otherwise -- see
    `compiled_exe_dir`'s docstring for why compiled-vs-not is detected the way it is.

    Deliberately `Path(".")`, NOT `Path.cwd()`, in the fallback case: `Path.cwd()` is captured
    ONCE, eagerly, at whatever moment this function happens to be called (for a module-level
    `DEFAULT_X_PATH = data_root() / "data" / "x"` constant, that's import time) -- a LATER CWD
    change (e.g. a test's `monkeypatch.chdir(tmp_path)`, the exact pattern several tests in this
    codebase use to prove a default resolves relative to "wherever we are now") would silently
    stop affecting the already-baked-in absolute value. `Path(".") / "data" / "x"` instead
    collapses to the plain relative `Path("data/x")` (verified: `str()` output is byte-identical
    to the original bare-literal form), preserving this codebase's original lazy-resolve-at-use
    behavior for dev/test exactly -- a regression caught the hard way (2026-08-22): `tests/
    test_mode.py::test_default_log_path_used_when_none_given` chdirs into an isolated `tmp_path`
    and expects `current_mode()`'s no-arg fallback to read THAT directory's own (absent) mode
    log, not whatever CWD was active when `reclaim.mode` was first imported -- an eager
    `Path.cwd()` broke that guarantee for all ten call sites at once, and did so SILENTLY on any
    clean checkout (no stray `data/mode_log.jsonl` at the importing CWD to expose it), only
    surfacing as a real assertion failure on a working tree that happened to have one from
    earlier manual testing. Only the frozen build's behavior changes from before this whole fix
    landed (no longer crashes when CWD isn't the install dir); dev/test runs are now BYTE-
    IDENTICAL to pre-fix behavior, not merely "equivalent in the common case."
    """
    compiled_dir = compiled_exe_dir()
    return compiled_dir if compiled_dir is not None else Path()
