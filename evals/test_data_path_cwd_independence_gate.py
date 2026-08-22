from __future__ import annotations

import ast
from pathlib import Path

# P0 fix (2026-08-22, live-reproduced under a real frozen install): a bare CWD-relative
# `Path("data/...")` literal breaks for any invocation path that has no working-directory
# concept -- packaging/reclaim.iss's [Registry] `reclaim-notify:` URI protocol handler (the
# disk-space toast's Snooze button) is the one CONFIRMED-live case (PR #51). Ten more modules
# (`mode.py`, `first_run.py`, `executor.py`, `api/app.py`, `mcp/server.py`, `cli.py`,
# `anthropic_key_store.py`, `ai/clutter_ranker.py`, `ai/category_explainer.py`,
# `notifications.py`) independently constructed the identical pattern, found by a plain grep
# sweep, not by this gate (this gate did not exist yet when they were found -- see rule 85b: a
# sweep is a control too, and this one was written only after the fix, specifically so the NEXT
# occurrence doesn't need a human to notice it by inspection).
#
# The fix in every case: route through `reclaim.app_paths.data_root()`, which anchors to the
# real running executable's directory when compiled under Nuitka, or `Path.cwd()` otherwise (see
# that module's docstring). This gate proves structurally that no OTHER module reintroduces the
# broken pattern -- a bare `Path("data/...")` (or `Path("data")`) literal anywhere under
# `src/reclaim/` -- rather than relying on convention or code review to catch it every time.
#
# SCOPE, disclosed rather than implied: this gate flags only the exact `Path("data/...")` literal
# shape that every real instance of this bug actually took. It does NOT catch a hypothetical
# `Path.cwd() / "data" / ...` construction that bypasses `data_root()` while still being
# CWD-relative in the frozen case -- no real instance of that shape exists in this codebase today,
# so a gate for it would be speculative rather than a regression proof. It also does NOT cover
# `cli.py`'s `_DEFAULT_CONFIG_PATH = Path("config.toml")` (deliberately out of scope: `config.toml`
# is not part of the `data/` convention this fix addresses, and is not currently reachable from
# any working-directory-less invocation -- both the protocol handler and the Task Scheduler
# action always pass `--config` as an absolute path or rely on an explicit `WorkingDirectory`
# rather than this default). See docs/AUDIT-2026-08.md's residual-gaps list.

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim"


def _path_call_names(tree: ast.Module) -> set[str]:
    """Every local name that refers to `pathlib.Path` itself (`from pathlib import Path`, `from
    pathlib import Path as P`, or `import pathlib` -- the last one makes `pathlib.Path(...)`
    reachable via the `pathlib` name, handled separately in `data_relative_path_violations`)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pathlib":
            for alias in node.names:
                if alias.name == "Path":
                    names.add(alias.asname or alias.name)
    return names


def _is_data_relative_string(value: object) -> bool:
    return isinstance(value, str) and (value == "data" or value.startswith(("data/", "data\\")))


def data_relative_path_violations(source: str) -> list[str]:
    """Returns a human-readable violation string (with line number) for every `Path(...)` call
    in `source` whose first positional argument is a string literal equal to `"data"` or
    starting with `"data/"`/`"data\\"` -- the exact bare-relative-path construction that broke
    under the protocol handler (PR #51) and eight other modules found by grep in the same
    session. Deliberately string-literal-only (not `.startswith("data")` on a computed value at
    runtime) -- the point is to catch the LITERAL AUTHORED PATTERN, the same way
    `test_r2_llm_env_var_gate.py` flags the call shape rather than trying to evaluate arguments.
    """
    tree = ast.parse(source)
    path_names = _path_call_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_bare_path_call = isinstance(func, ast.Name) and func.id in path_names
        is_qualified_path_call = (
            isinstance(func, ast.Attribute)
            and func.attr == "Path"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pathlib"
        )
        if not (is_bare_path_call or is_qualified_path_call):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and _is_data_relative_string(first_arg.value):
            violations.append(f"line {node.lineno}: Path({first_arg.value!r}) -- CWD-relative")
    return violations


def test_no_module_under_src_reclaim_builds_a_bare_cwd_relative_data_path() -> None:
    """The real, load-bearing check: re-run against every real `.py` file under `src/reclaim/`
    on every CI run. A future edit that adds so much as one bare `Path("data/...")` literal
    anywhere in this tree fails here immediately, regardless of which module it's in."""
    all_violations: dict[str, list[str]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        violations = data_relative_path_violations(path.read_text(encoding="utf-8"))
        if violations:
            all_violations[str(path.relative_to(_SRC_ROOT.parent.parent))] = violations
    assert all_violations == {}, (
        "every data/-relative default must route through reclaim.app_paths.data_root() instead "
        f"of a bare CWD-relative Path(...) literal (see PR #51): {all_violations}"
    )


def test_the_guard_catches_a_new_bare_cwd_relative_data_path() -> None:
    """Negative test (teeth-proof): the exact shape this guard exists to forbid -- a brand new
    module-level constant built as `Path("data/something.json")` -- injected as a source string.
    Never touches the real file."""
    poisoned = (
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "\n"
        'DEFAULT_SOMETHING_PATH = Path("data/something.json")\n'
    )
    violations = data_relative_path_violations(poisoned)
    assert violations, "expected the guard to flag the bare Path('data/...') literal"


def test_the_guard_catches_the_bare_data_directory_form_and_aliased_and_qualified_imports() -> None:
    """Negative test covering the other shapes the guard claims to catch: `Path("data")` (no
    trailing segment), `from pathlib import Path as P` + `P("data/x")`, and `import pathlib` +
    `pathlib.Path("data/x")`."""
    poisoned_bare_dir = (
        "from __future__ import annotations\nfrom pathlib import Path\nX = Path('data')\n"
    )
    assert data_relative_path_violations(poisoned_bare_dir), "bare Path('data') not caught"

    poisoned_aliased = (
        "from __future__ import annotations\nfrom pathlib import Path as P\nX = P('data/y.json')\n"
    )
    assert data_relative_path_violations(poisoned_aliased), "aliased Path import not caught"

    poisoned_qualified = (
        "from __future__ import annotations\nimport pathlib\nX = pathlib.Path('data/z.json')\n"
    )
    assert data_relative_path_violations(poisoned_qualified), "pathlib.Path(...) form not caught"


def test_the_guard_passes_the_real_fixed_pattern_with_no_false_positive() -> None:
    """Negative test proving no false positive: the ACTUAL fix pattern every real site in this
    codebase now uses (`data_root() / "data" / "something.json"`) must never be flagged -- the
    guard targets the literal `Path("data/...")` call shape specifically, not the string `"data"`
    appearing anywhere at all."""
    clean = (
        "from __future__ import annotations\n"
        "from pathlib import Path\n"
        "\n"
        "from reclaim.app_paths import data_root\n"
        "\n"
        'DEFAULT_SOMETHING_PATH = data_root() / "data" / "something.json"\n'
        "\n"
        "def _unrelated(p: Path) -> Path:\n"
        "    return Path(p) / 'not_data_prefixed'\n"
    )
    assert data_relative_path_violations(clean) == []
