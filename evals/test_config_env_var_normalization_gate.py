from __future__ import annotations

import ast
from pathlib import Path

# P0 fix (2026-08 session, see src/reclaim/config.py's own comment above `_resolve_long_path`):
# `%TEMP%` (and, in principle, any other env-derived path config.py resolves) can come back
# already in Windows' 8.3 short-name (DOS alias) form. The scanner always indexes real long-form
# paths, so a detector pattern built from an unresolved short-form value can structurally never
# match anything in the index -- silently, with no error anywhere. The fix routes every
# detector-pattern-building env-var read through one chokepoint, `_win_path`, which calls
# `_resolve_long_path` before returning.
#
# That fix is only as durable as the chokepoint itself: a future detector-pattern helper that
# reads `os.environ` directly (however it's spelled) reintroduces the exact same silent breakage,
# and nothing about Python itself stops that from happening. This file is the structural (AST-
# based) proof that it can't happen unnoticed, mirroring evals/test_r2_llm_env_var_gate.py's
# "prove it structurally, not by convention" pattern:
#
#   1. No function in config.py OTHER THAN `_win_path` itself ever reads `os.environ`/`os.getenv`
#      (any access shape -- `.get(...)`, `[...]`, aliased imports, `from os import ...`, etc.),
#      via `env_var_bypass_violations` below. This is the "no bypass exists" property.
#   2. `_win_path`'s own body actually calls `_resolve_long_path(...)` before returning, via
#      `win_path_calls_resolve_long_path` below. This is the "the chokepoint itself hasn't been
#      silently weakened" property -- necessary because #1 alone would still pass if someone
#      reverted `_win_path` to `Path(value).as_posix()` directly while leaving it the sole
#      `os.environ` call site: no bypass would exist, but the actual normalization fix would be
#      gone.
#
# Both checks are pure `ast` structural analysis -- no substring/grep matching on source text,
# no argument inspection -- so an obfuscated or differently-shaped violation is caught the same
# way a literal one is.

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "src" / "reclaim" / "config.py"

_CHOKEPOINT_FUNCTION = "_win_path"
_LONG_PATH_RESOLVER = "_resolve_long_path"

# Names that, once imported this way, make a bare `environ`/`getenv` reference in the source
# resolve to `os.environ`/`os.getenv` -- e.g. `from os import environ as environ` or
# `from os import getenv`. Same convention as evals/test_r2_llm_env_var_gate.py.
_ENV_QUALIFIED_NAMES = {"environ", "getenv"}


def _os_aliases(tree: ast.Module) -> set[str]:
    """Every local name that refers to the `os` module itself (`import os`, `import os as _os`).
    `os.environ`/`os.getenv` access through any of these aliases is caught, not just literal
    `os.`."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _direct_env_names(tree: ast.Module) -> set[str]:
    """Every local name bound directly to `os.environ`/`os.getenv` via `from os import ...`
    (with or without `as`), e.g. `from os import environ`, `from os import getenv as _ge`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _ENV_QUALIFIED_NAMES:
                    names.add(alias.asname or alias.name)
    return names


def _env_var_access_nodes(tree: ast.Module) -> list[ast.AST]:
    """Every AST node representing an environment-variable access in `tree`, any shape:
    `os.environ`/`<alias>.environ` (any attribute access -- `.get(...)`, `[...]`, `.keys()`,
    iteration, etc.), `os.getenv(...)`/`<alias>.getenv(...)`, and bare `environ`/`getenv` names
    bound via `from os import ...`. Deliberately does NOT inspect call arguments -- the point is
    that no access of any kind is permitted outside `_win_path`, not that a specific env var
    name is forbidden.
    """
    os_aliases = _os_aliases(tree)
    direct_env_names = _direct_env_names(tree)
    nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        ):
            nodes.append(node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
        ):
            nodes.append(node)
        if isinstance(node, ast.Name) and node.id in direct_env_names:
            nodes.append(node)
    return nodes


def _enclosing_function_name(tree: ast.Module, lineno: int) -> str | None:
    """Returns the name of the innermost `FunctionDef`/`AsyncFunctionDef` in `tree` whose
    line range (`lineno`..`end_lineno`) contains `lineno`, or `None` if `lineno` sits at module
    level, outside any function.

    Module-level access is deliberately treated as "not `_win_path`" (i.e. still a violation)
    by the caller -- the invariant this gate proves is "the ONLY place config.py reads an
    environment variable is inside `_win_path`", not merely "no OTHER function does", so a
    stray module-level `os.environ.get(...)` must be caught too.
    """
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno if node.end_lineno is not None else node.lineno
            if start <= lineno <= end:
                if best is None:
                    best = node
                else:
                    best_end = best.end_lineno if best.end_lineno is not None else best.lineno
                    if (end - start) < (best_end - best.lineno):
                        best = node
    return best.name if best is not None else None


def env_var_bypass_violations(source: str) -> list[str]:
    """Returns a human-readable violation string (with line number and enclosing function) for
    every environment-variable access in `source` that happens OUTSIDE `_win_path`.

    This is gate #1: any FUTURE detector-pattern helper that reads an env var directly --
    bypassing `_win_path` and therefore skipping `_resolve_long_path`'s 8.3 short-name
    normalization -- fails here immediately, whether it's a brand new function, an existing one,
    or bare module-level code.
    """
    tree = ast.parse(source)
    violations: list[str] = []
    for node in _env_var_access_nodes(tree):
        enclosing = _enclosing_function_name(tree, node.lineno)
        if enclosing != _CHOKEPOINT_FUNCTION:
            where = f"function {enclosing!r}" if enclosing is not None else "module level"
            violations.append(f"line {node.lineno}: environment-variable access in {where}")
    return violations


def win_path_calls_resolve_long_path(source: str) -> bool:
    """True iff `_win_path`'s own body contains a `Call` node whose `func` resolves to the bare
    name `_resolve_long_path`.

    This is gate #2: proves the chokepoint itself hasn't been silently weakened (e.g. reverted
    to `Path(value).as_posix()` directly) even when gate #1 still passes because `_win_path`
    remains the sole `os.environ`/`os.getenv` call site in the module. Structural (AST `Call`
    node) check -- never a substring/grep match on the source text.
    """
    tree = ast.parse(source)
    win_path_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _CHOKEPOINT_FUNCTION
        ),
        None,
    )
    if win_path_fn is None:
        return False
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == _LONG_PATH_RESOLVER
        for call in ast.walk(win_path_fn)
    )


def test_only_win_path_reads_an_environment_variable_in_config() -> None:
    """The real, load-bearing check: re-run against the actual config.py source on every CI run.
    A future edit that adds so much as one `os.environ`/`os.getenv` reference anywhere in
    config.py OTHER than inside `_win_path` fails here immediately."""
    assert _CONFIG_PATH.exists(), f"expected {_CONFIG_PATH} to exist"
    source = _CONFIG_PATH.read_text(encoding="utf-8")
    violations = env_var_bypass_violations(source)
    assert violations == [], (
        "config.py must route every environment-variable read through _win_path (which resolves "
        f"8.3 short-name paths via _resolve_long_path before returning): {violations}"
    )


def test_win_path_itself_calls_resolve_long_path_before_returning() -> None:
    """The second real, load-bearing check: `_win_path` must not just be the SOLE env-var access
    point (proven above) but must actually still call `_resolve_long_path(...)` -- otherwise the
    long-path normalization fix could be silently reverted while gate #1 keeps passing."""
    source = _CONFIG_PATH.read_text(encoding="utf-8")
    assert win_path_calls_resolve_long_path(source), (
        "_win_path must call _resolve_long_path(...) before returning -- otherwise env-derived "
        "paths silently skip 8.3 short-name normalization even though _win_path remains the "
        "sole os.environ/os.getenv access point in config.py"
    )


def test_the_guard_catches_a_new_function_bypassing_win_path() -> None:
    """Negative test (teeth-proof a): a brand new function added to config.py that reads
    `os.environ.get("APPDATA")` directly, bypassing `_win_path` entirely. Injected as a source
    string shaped like the real module's own `_win_path`/`_resolve_long_path` pair -- never
    touches the real file."""
    poisoned = (
        "from __future__ import annotations\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def _resolve_long_path(path: str) -> str:\n"
        "    return path\n"
        "\n"
        "def _win_path(env_var: str, fallback: str) -> str:\n"
        "    value = os.environ.get(env_var)\n"
        "    return Path(_resolve_long_path(value)).as_posix() if value else fallback\n"
        "\n"
        "def _new_helper() -> str | None:\n"
        '    return os.environ.get("APPDATA")\n'
    )
    violations = env_var_bypass_violations(poisoned)
    assert violations, "expected the guard to flag the bypassing _new_helper access"
    assert any("_new_helper" in v for v in violations)


def test_the_guard_catches_win_path_dropping_the_resolve_long_path_call() -> None:
    """Negative test (teeth-proof b): `_win_path` rewritten to drop the `_resolve_long_path(...)`
    call -- reverted to `Path(value).as_posix()` directly -- while remaining the sole
    `os.environ` call site in the module. Gate #1 alone would NOT catch this (no bypass exists);
    gate #2 must catch it anyway."""
    poisoned = (
        "from __future__ import annotations\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def _resolve_long_path(path: str) -> str:\n"
        "    return path\n"
        "\n"
        "def _win_path(env_var: str, fallback: str) -> str:\n"
        "    value = os.environ.get(env_var)\n"
        "    return Path(value).as_posix() if value else fallback\n"
    )
    assert env_var_bypass_violations(poisoned) == [], (
        "gate #1 should NOT flag this snippet -- _win_path is still the sole os.environ access "
        "point, which is exactly why gate #2 is needed to catch the dropped call"
    )
    assert not win_path_calls_resolve_long_path(poisoned), (
        "gate #2 should have caught _win_path no longer calling _resolve_long_path(...)"
    )


def test_the_guard_passes_a_clean_source_mirroring_the_real_shape() -> None:
    """Negative test (teeth-proof c): a clean/passing source snippet mirroring config.py's real
    shape (`_resolve_long_path`, `_win_path` calling it, and a downstream helper calling
    `_win_path` rather than reading the env var itself) must NOT be flagged by either gate --
    proves no false positive."""
    clean = (
        "from __future__ import annotations\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def _resolve_long_path(path: str) -> str:\n"
        "    return path\n"
        "\n"
        "def _win_path(env_var: str, fallback: str) -> str:\n"
        "    value = os.environ.get(env_var)\n"
        "    return Path(_resolve_long_path(value)).as_posix() if value else fallback\n"
        "\n"
        "def _default_temp_roots() -> list[str]:\n"
        '    temp = _win_path("TEMP", "C:/Users/Default/AppData/Local/Temp")\n'
        "    return [temp, 'C:/Windows/Temp']\n"
    )
    assert env_var_bypass_violations(clean) == []
    assert win_path_calls_resolve_long_path(clean)
