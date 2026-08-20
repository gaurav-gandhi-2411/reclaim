from __future__ import annotations

import ast
from pathlib import Path

# R2 (per-category LLM explainer): a structural (AST-based) proof that the two modules R2 built
# to talk to Anthropic -- reclaim.ai.category_explainer (the API caller) and
# reclaim.anthropic_key_store (the DPAPI-backed key store the caller sources its key from) --
# NEVER read ANY environment variable, not just "no ANTHROPIC_API_KEY substring". Module
# docstring/comments in category_explainer.py already claim "this module never reads
# ANTHROPIC_API_KEY (or any other bare environment variable) as an implicit key source" and
# "raw httpx instead of the anthropic SDK specifically to eliminate any env-var key-fallback
# risk by construction" -- necessary but not sufficient on its own: raw httpx removes the SDK's
# OWN env-var fallback, but nothing stops a hand-written `os.environ.get(...)` (or an obfuscated
# `os.environ.get("ANTHROPIC" + "_API_KEY")`, which a naive substring-on-"ANTHROPIC_API_KEY"
# check would miss) from being added to either module later. This file is that check, mirroring
# evals/test_ai_safety_gate.py's and evals/test_mcp_safety_gate.py's "prove it structurally, not
# by convention" AST-scan pattern.

_AI_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim" / "ai"
_REPO_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim"

# The two R2 modules with any involvement in getting an Anthropic API key from storage to the
# outbound HTTP call: category_explainer.py never imports anthropic_key_store.py directly (the
# API layer -- api/service.py -- loads the key and passes it in as an explicit `api_key: str`
# argument, never as an env-var fallback inside the explainer itself), but the key-handling
# guarantee this test proves ("cannot read ANTHROPIC_API_KEY or any env var") is only meaningful
# if BOTH the module that calls the API and the module that stores/loads the key are covered --
# scanning only the former would leave the latter free to grow an `os.environ` fallback of its
# own with nothing here to catch it.
_SCANNED_FILES = (
    _AI_PACKAGE_ROOT / "category_explainer.py",
    _REPO_SRC_ROOT / "anthropic_key_store.py",
)

# Names that, once imported this way, make a bare `environ`/`getenv` reference in the source
# resolve to `os.environ`/`os.getenv` -- e.g. `from os import environ as environ` or
# `from os import getenv`.
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


def env_var_access_violations(source: str) -> list[str]:
    """Returns a human-readable violation string (with line number) for every construct in
    `source` that could read an environment variable, however it's spelled:

    - `os.environ` (or an aliased `import os as x` -> `x.environ`), any access shape: `.get(...)`,
      `[...]`, `.keys()`, iteration, etc. -- ANY attribute access on `os.environ` is flagged, not
      just the `.get(` call shape, since `os.environ["ANTHROPIC_API_KEY"]` is an equally real
      fallback and a substring/call-shape-only check would miss it.
    - `os.getenv(...)` (or aliased).
    - `environ`/`getenv` used bare after `from os import environ`/`from os import getenv`.

    Deliberately does NOT try to inspect what argument is passed to any of these (e.g. whether
    it's literally `"ANTHROPIC_API_KEY"`) -- the point is that NO environment-variable access of
    any kind is permitted in these modules, so an obfuscated argument
    (`os.environ.get("ANTHROPIC" + "_API_KEY")`) is caught by flagging the call itself, not by
    string-matching its argument.
    """
    tree = ast.parse(source)
    os_aliases = _os_aliases(tree)
    direct_env_names = _direct_env_names(tree)

    violations: list[str] = []
    for node in ast.walk(tree):
        # os.environ / <alias>.environ -- any attribute access at all
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        ):
            violations.append(f"line {node.lineno}: `{node.value.id}.environ` access")
        # os.getenv(...) / <alias>.getenv(...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
        ):
            violations.append(f"line {node.lineno}: `{node.func.value.id}.getenv(...)` call")
        # bare `environ`/`getenv` after `from os import environ`/`from os import getenv`
        if isinstance(node, ast.Name) and node.id in direct_env_names:
            violations.append(f"line {node.lineno}: bare `{node.id}` (imported from os) reference")

    return violations


def test_category_explainer_and_key_store_never_access_any_environment_variable() -> None:
    """The real, load-bearing check: re-run against the actual R2 source on every CI run. A
    future edit that adds so much as one `os.environ`/`os.getenv` reference to either module
    (however it's spelled -- see `env_var_access_violations`'s docstring) fails here immediately."""
    for path in _SCANNED_FILES:
        assert path.exists(), f"expected {path} to exist"
        violations = env_var_access_violations(path.read_text(encoding="utf-8"))
        assert violations == [], (
            f"{path.name} must never read any environment variable (R2's key-isolation "
            f"guarantee): {violations}"
        )


def test_the_guard_catches_a_direct_os_environ_get_call() -> None:
    """Negative test: the exact shape this guard exists to forbid -- `os.environ.get(
    "ANTHROPIC_API_KEY")` -- injected into a source string shaped like category_explainer.py's
    own import block. Never touches the real module."""
    poisoned = (
        "from __future__ import annotations\n"
        "import os\n"
        "import httpx\n"
        "\n"
        "def _load_key() -> str | None:\n"
        '    return os.environ.get("ANTHROPIC_API_KEY")\n'
    )
    violations = env_var_access_violations(poisoned)
    assert violations, "expected the guard to flag os.environ.get(...)"
    assert any("environ" in v for v in violations)


def test_the_guard_catches_an_obfuscated_key_name_built_by_string_concatenation() -> None:
    """Negative test proving the guard does NOT rely on the literal string "ANTHROPIC_API_KEY"
    being present -- an obfuscated/constructed key name must still be caught, because the guard
    flags the `os.environ.get(...)` CALL itself, never its argument."""
    poisoned = (
        "from __future__ import annotations\n"
        "import os\n"
        "\n"
        "def _load_key() -> str | None:\n"
        '    return os.environ.get("ANTHROPIC" + "_API_KEY")\n'
    )
    violations = env_var_access_violations(poisoned)
    assert violations, "expected the guard to flag an obfuscated os.environ.get(...) call"


def test_the_guard_catches_os_getenv_and_bare_getenv_and_subscript_and_aliased_import() -> None:
    """Negative test covering the other access shapes the guard claims to catch: `os.getenv(...)`,
    `from os import getenv` + bare `getenv(...)`, `os.environ[...]` subscript access, and
    `import os as _os` aliasing."""
    poisoned_getenv = "import os\nos.getenv('ANTHROPIC_API_KEY')\n"
    assert env_var_access_violations(poisoned_getenv), "os.getenv(...) not caught"

    poisoned_bare_getenv = "from os import getenv\ngetenv('ANTHROPIC_API_KEY')\n"
    assert env_var_access_violations(poisoned_bare_getenv), "bare getenv(...) not caught"

    poisoned_subscript = "import os\nkey = os.environ['ANTHROPIC_API_KEY']\n"
    assert env_var_access_violations(poisoned_subscript), "os.environ[...] subscript not caught"

    poisoned_bare_environ = "from os import environ\nkey = environ['ANTHROPIC_API_KEY']\n"
    assert env_var_access_violations(poisoned_bare_environ), "bare environ[...] not caught"

    poisoned_aliased = "import os as _os\nkey = _os.environ.get('ANTHROPIC_API_KEY')\n"
    assert env_var_access_violations(poisoned_aliased), "aliased `import os as _os` not caught"


def test_the_guard_passes_clean_source_with_no_env_var_access() -> None:
    """Sanity check: genuinely clean source (using os for something unrelated to environ/getenv,
    e.g. os.path) must not be flagged -- the guard is scoped to environment-variable access only,
    not to `os` usage in general."""
    clean = (
        "from __future__ import annotations\n"
        "import os\n"
        "\n"
        "def _exists(p: str) -> bool:\n"
        "    return os.path.exists(p)\n"
    )
    assert env_var_access_violations(clean) == []
