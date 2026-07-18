from __future__ import annotations

# git_guard.py — a `git` wrapper that refuses any `--global`/`--system` config mutation unless
# the caller has explicitly sandboxed where that scope points.
#
# Why this exists: `.github/workflows/eval.yml`'s fixture-identity step used to run a bare
# `git config --global user.email ...` — harmless on a real, ephemeral GitHub Actions runner, but
# that exact command was once accidentally copy-pasted and run directly against a real developer
# machine's global git config during an agent session (see PLAN.md's 2026-07-18 checkpoint),
# overwriting the real identity for part of one turn before being caught and reverted. This
# script makes that class of mistake structurally hard to repeat: nothing in this repo's own
# tracked scripts, hooks, or CI-simulation tooling is allowed to call `git config
# --global`/`--system` unless the corresponding scope has been explicitly, unambiguously
# redirected away from the real user profile first.
#
# Usage — a drop-in prefix for any git invocation that might mutate config:
#
#     python scripts/git_guard.py config --global user.email "fixture@reclaim.test"
#
# Forwards to the real `git` binary with the same argv when sandboxed, refuses with a clear,
# actionable error otherwise (rather than failing silently or corrupting a real config file).
# Non-config-mutating invocations (`git status`, `git init`, `git config --local ...`, plain
# `git -c user.email=... commit ...`) always pass straight through untouched.
#
# A sandboxed environment is one where EITHER:
#   1. `GIT_CONFIG_GLOBAL` (for `--global`) / `GIT_CONFIG_SYSTEM` (for `--system`) is set — git's
#      own native override (2.32+) for where that scope reads/writes; the presence of the
#      variable is itself an unambiguous "I meant to redirect this" signal, or
#   2. `RECLAIM_GIT_SANDBOX_HOME` is set to a truthy value AND `HOME`/`USERPROFILE` is also set —
#      an explicit project-specific opt-in flag, checked instead of comparing HOME's *value*
#      against "the real home" (which is circular: once HOME is legitimately redirected for
#      sandboxing, there is no independent oracle left to tell "redirected" from "coincidentally
#      already this value" — the flag, not HOME's contents, is what makes the intent
#      unambiguous).
#
# Not a comprehensive shell-level interceptor: this only protects invocations actually routed
# through it. A raw `git config --global ...` typed directly into a shell, or run by a script
# that bypasses this wrapper, is not caught — no wrapper script can intercept commands that never
# call it. Its job is narrower and achievable: give every script/hook/CI-sim step this repo owns
# a shared, tested chokepoint to route through, and make the unsafe pattern impossible to copy
# from this repo's own tracked files (see the sandboxed `eval.yml` step and
# evals/fixtures/build_golden_tree.py's/tests/test_scanner.py's switch to `-c`-scoped identity
# instead of any `git config` call at all, local or global).
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

_GLOBAL_SCOPE_FLAG = "--global"
_SYSTEM_SCOPE_FLAG = "--system"
_SCOPE_ENV_VAR = {"global": "GIT_CONFIG_GLOBAL", "system": "GIT_CONFIG_SYSTEM"}
_SANDBOX_FLAG_VAR = "RECLAIM_GIT_SANDBOX_HOME"


def requested_scope(argv: Sequence[str]) -> str | None:
    """Returns "global", "system", or None for a `git <argv>` invocation — None means either
    this isn't a `config` subcommand at all, or it is but carries no explicit --global/--system
    scope flag (e.g. `git config --local ...`, `git -c k=v commit ...`, plain `git status`)."""
    if len(argv) == 0 or argv[0] != "config":
        return None
    args = set(argv[1:])
    if _GLOBAL_SCOPE_FLAG in args:
        return "global"
    if _SYSTEM_SCOPE_FLAG in args:
        return "system"
    return None


def sandbox_violation(scope: str, env: Mapping[str, str]) -> str | None:
    """Returns a human-readable rejection reason if `scope` ("global"/"system") is not safely
    sandboxed in `env`, or None if it's safe to proceed. See the module comment above for exactly
    what counts as sandboxed."""
    config_var = _SCOPE_ENV_VAR[scope]
    if env.get(config_var):
        return None

    sandbox_flag = env.get(_SANDBOX_FLAG_VAR, "").strip().lower()
    home_redirected = bool(env.get("HOME") or env.get("USERPROFILE"))
    if sandbox_flag in ("1", "true", "yes") and home_redirected:
        return None

    return (
        f"refusing 'git config --{scope} ...': this repo's own scripts/hooks/CI-sim must never "
        f"mutate a real user's --{scope} git config. Neither {config_var} nor "
        f"{_SANDBOX_FLAG_VAR} (+HOME/USERPROFILE) is set, so nothing here is confirmed "
        "sandboxed. Either:\n"
        "  1. Prefer 'git -c key=value <command>' (scoped to one invocation, no config file "
        f"touched at all) or 'git config --local ...' instead of --{scope}, or\n"
        f"  2. If you genuinely need --{scope} semantics (e.g. one identity shared across many "
        f"fixture repos), set {config_var} to a throwaway path first."
    )


def _resolve_git(env: Mapping[str, str]) -> str:
    git_exe = shutil.which("git", path=env.get("PATH"))
    if git_exe is None:
        raise RuntimeError("git executable not found on PATH")
    return git_exe


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    resolved_env = dict(env if env is not None else os.environ)

    scope = requested_scope(args)
    if scope is not None:
        violation = sandbox_violation(scope, resolved_env)
        if violation is not None:
            print(f"git_guard: {violation}", file=sys.stderr)
            return 1

    git_exe = _resolve_git(resolved_env)
    result = subprocess.run([git_exe, *args], env=resolved_env, check=False)  # noqa: S603
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
