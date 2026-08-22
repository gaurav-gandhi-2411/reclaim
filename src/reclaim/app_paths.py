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
    executable's own directory when compiled under Nuitka, or the current working directory
    otherwise -- see `compiled_exe_dir`'s docstring for why compiled-vs-not is detected the way
    it is. Dev/test runs (always launched from the repo root, and free to override any default
    via each call site's own `path: Path | None` parameter) are unaffected; only the frozen
    build's behavior changes, and only in the direction of "no longer crashes when CWD isn't the
    install dir."""
    compiled_dir = compiled_exe_dir()
    return compiled_dir if compiled_dir is not None else Path.cwd()
