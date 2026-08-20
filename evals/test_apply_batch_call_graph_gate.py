from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Structural (not behavioral) proof, promoted from a throwaway audit scratchpad script into this
# repo's established safety-gate convention (mirrors evals/test_ai_safety_gate.py's AST-scan
# structure). Proves two properties by parsing src/reclaim/executor.py and src/reclaim/purge.py
# with Python's `ast` module -- never string/regex matching, which a comment or a docstring
# mentioning "shutil.rmtree" could trivially fool either direction:
#
# 1. Every DIRECT call to a real filesystem-mutation primitive (`shutil.rmtree`,
#    `send2trash.send2trash`, `os.rename`, `os.unlink`) anywhere in these two files is lexically
#    confined to one of four functions: `_atomic_move` / `unlink_clear_readonly` (executor.py's
#    two crash-safe move/delete primitives, ADR-0004) or `apply_batch` (executor.py) /
#    `purge_expired` (purge.py) themselves (the two batch entry points that call those
#    primitives directly for their recycle-bin/direct-delete/permanent-purge branches).
# 2. Inside `apply_batch`'s per-candidate loop, Audit P0-1's pre-flight-check guard
#    (`_preflight_skip_reason` + its `if skip_reason is not None: continue`) structurally
#    precedes the first statement that can reach a mutation primitive -- sufficient to prove by
#    comparing statement-list positions within the loop body, since Python executes a statement
#    list top-to-bottom with no branch that can skip an earlier guard.
#
# Section 3 proves this gate has teeth (house rule 85a: a control that would pass vacuously is
# worth exactly nothing) by reproducing the same two adversarial cases the original audit used --
# against scratch copies only, written to a pytest `tmp_path` and parsed from there; neither
# negative test ever writes to the real tracked executor.py/purge.py.

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim"
_EXECUTOR_PATH = _SRC_ROOT / "executor.py"
_PURGE_PATH = _SRC_ROOT / "purge.py"

_FORBIDDEN_MUTATION_PRIMITIVES = {
    "shutil.rmtree",
    "send2trash.send2trash",
    "os.rename",
    "os.unlink",
}

_ALLOWED_HOMES_EXECUTOR = {"_atomic_move", "unlink_clear_readonly", "apply_batch"}
_ALLOWED_HOMES_PURGE = {"purge_expired"}

# apply_batch's own preflight guard call, and the two allowed-home wrapper functions a mutation
# can additionally reach through (rather than only ever via a bare primitive call) -- see
# `_MUTATION_REACHING_NAMES` below, used only for the loop-order proof in section 2.
_PREFLIGHT_GUARD_CALL_NAME = "_preflight_skip_reason"
_MUTATION_REACHING_NAMES = _FORBIDDEN_MUTATION_PRIMITIVES | {
    "_atomic_move",
    "unlink_clear_readonly",
}

# Audit-traced real call sites into `apply_batch` (section 4): `cli.py::_run_apply` (the CLI
# `apply` command) and `api/service.py::run_apply` (POST /api/apply's background task). Both
# import it via `from reclaim.executor import apply_batch` (confirmed by grep when this test was
# written), so both call it as the bare name `apply_batch(...)`, not a qualified attribute.
_EXPECTED_APPLY_BATCH_CALLER_FILES = frozenset({"cli.py", "api/service.py"})


# --- AST helpers ---------------------------------------------------------------------------------


def _call_target_name(call: ast.Call) -> str | None:
    """The dotted or plain name a `Call` node's callee resolves to syntactically -- `"os.unlink"`
    for `os.unlink(...)`, `"apply_batch"` for a bare `apply_batch(...)`, `None` for anything more
    exotic (a call through a subscript, a chained attribute more than one level deep, ...), none
    of which any call site relevant to this file's two mutation-scoping proofs actually uses."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.FunctionDef)]


def _primitive_call_homes(source: str, names: set[str]) -> dict[str, list[str]]:
    """For each name in `names`, the list of module-level (outermost) function names whose body
    contains at least one direct `Call` to it -- one list entry per occurrence, so a name called
    twice in the same function appears twice. A name never called anywhere in `source` maps to an
    empty list, never a `KeyError` -- callers must not mistake "never called" for "not scanned".
    A nested `def` (there are none inside the loop bodies this file cares about) is still
    attributed to its OUTERMOST enclosing module-level function, since `ast.walk` over a
    top-level FunctionDef descends into everything lexically nested inside it.
    """
    tree = ast.parse(source)
    homes: dict[str, list[str]] = {name: [] for name in names}
    for top_fn in _top_level_functions(tree):
        for node in ast.walk(top_fn):
            if isinstance(node, ast.Call):
                target = _call_target_name(node)
                if target in homes:
                    homes[target].append(top_fn.name)
    return homes


def _mutation_primitive_violations(source: str, allowed_homes: set[str]) -> dict[str, list[str]]:
    """Section 1's core check: every mutation-primitive call whose home function is NOT in
    `allowed_homes`, keyed by primitive name. Empty dict == full confinement."""
    homes = _primitive_call_homes(source, _FORBIDDEN_MUTATION_PRIMITIVES)
    return {
        primitive: bad
        for primitive, found_in in homes.items()
        if (bad := [fn for fn in found_in if fn not in allowed_homes])
    }


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r} found")


def _find_first_for_loop(fn: ast.FunctionDef) -> ast.For:
    for node in ast.walk(fn):
        if isinstance(node, ast.For):
            return node
    raise AssertionError(f"no for-loop found inside {fn.name!r}")


def _stmt_calls_any(stmt: ast.stmt, names: set[str]) -> bool:
    """True if `stmt`'s own subtree (an `if`/`try` block's nested body included) contains a
    direct `Call` to any name in `names`."""
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call) and _call_target_name(node) in names:
            return True
    return False


def _assert_preflight_guard_precedes_mutation(loop_body: list[ast.stmt]) -> None:
    """Section 2's core check, reused by both the real-file positive test and the synthetic
    negative test: within one per-candidate loop body (a plain list of top-level statements, in
    source order), the pre-flight-check call and its `if ...: continue` guard must structurally
    precede the first statement that can reach a mutation primitive -- either directly or via
    `_atomic_move`/`unlink_clear_readonly`. Raises `AssertionError` (via a plain `assert`, so
    `pytest.raises(AssertionError)` catches it the same way a failed test would) the moment any
    part of that invariant doesn't hold, rather than returning a bool -- this is shared directly
    by a test function, not merely a predicate an outer test interprets.
    """
    guard_indices = [
        i for i, stmt in enumerate(loop_body) if _stmt_calls_any(stmt, {_PREFLIGHT_GUARD_CALL_NAME})
    ]
    assert guard_indices, (
        f"no statement in this loop body calls {_PREFLIGHT_GUARD_CALL_NAME!r} -- the pre-flight "
        "guard itself is missing"
    )
    guard_call_idx = guard_indices[0]

    guard_continue_idx = guard_call_idx + 1
    assert guard_continue_idx < len(loop_body), (
        "the preflight guard call is the loop body's last statement -- no if/continue follows it"
    )
    guard_if_stmt = loop_body[guard_continue_idx]
    assert isinstance(guard_if_stmt, ast.If), (
        f"expected the statement right after the {_PREFLIGHT_GUARD_CALL_NAME!r} call to be an "
        f"`if`/`continue` guard, found {type(guard_if_stmt).__name__} instead"
    )
    assert any(isinstance(n, ast.Continue) for n in ast.walk(guard_if_stmt)), (
        "the if-block immediately following the preflight guard call has no `continue` in it -- "
        "a skip reason would be computed but never actually skip the item"
    )

    mutation_indices = [
        i for i, stmt in enumerate(loop_body) if _stmt_calls_any(stmt, _MUTATION_REACHING_NAMES)
    ]
    assert mutation_indices, "no statement in this loop body can reach a mutation primitive"
    first_mutation_idx = mutation_indices[0]

    assert first_mutation_idx > guard_continue_idx, (
        f"a mutation-reaching statement (loop-body index {first_mutation_idx}) appears at or "
        f"before the preflight guard's if/continue (index {guard_continue_idx}) -- the guard "
        "does not structurally precede every mutation path"
    )


def _files_calling(root: Path, target_name: str) -> set[str]:
    """Every `.py` file under `root` (relative posix path) containing at least one direct `Call`
    node whose callee resolves to the bare name `target_name` -- AST-based so a comment/docstring
    merely mentioning the name (there are several, in both executor.py and purge.py) can never
    produce a false positive."""
    callers: set[str] = set()
    for py_file in sorted(root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_target_name(node) == target_name:
                callers.add(py_file.relative_to(root).as_posix())
    return callers


# --- 1. Static: every mutation primitive is confined to its expected allowed home -------------


def test_executor_mutation_primitives_confined_to_allowed_homes() -> None:
    """`restore_batch` calls ONLY `_atomic_move` (never a primitive directly) and so needs no
    entry of its own in `_ALLOWED_HOMES_EXECUTOR` -- proven by this same scan finding no
    restore_batch-homed primitive call (if one ever appeared there, this test would catch it)."""
    source = _EXECUTOR_PATH.read_text(encoding="utf-8")
    violations = _mutation_primitive_violations(source, _ALLOWED_HOMES_EXECUTOR)
    assert violations == {}, (
        f"executor.py has mutation-primitive call(s) outside the allowed homes "
        f"{sorted(_ALLOWED_HOMES_EXECUTOR)}: {violations}"
    )


def test_executor_scan_actually_found_every_real_mutation_primitive() -> None:
    """Guards the guard (house rule 85a): a scan that found zero calls anywhere would trivially
    "pass" the test above without proving anything. Confirms all four primitives are genuinely
    present in executor.py today (shutil.rmtree x3, os.rename x1, os.unlink x2, send2trash x1),
    each already confined to an allowed home."""
    source = _EXECUTOR_PATH.read_text(encoding="utf-8")
    homes = _primitive_call_homes(source, _FORBIDDEN_MUTATION_PRIMITIVES)
    empty = {name for name, found_in in homes.items() if not found_in}
    assert empty == set(), (
        f"scan found zero occurrences of {sorted(empty)} in executor.py -- can't prove "
        "confinement of a primitive that was never found"
    )


def test_purge_mutation_primitives_confined_to_allowed_homes() -> None:
    source = _PURGE_PATH.read_text(encoding="utf-8")
    violations = _mutation_primitive_violations(source, _ALLOWED_HOMES_PURGE)
    assert violations == {}, (
        f"purge.py has mutation-primitive call(s) outside the allowed homes "
        f"{sorted(_ALLOWED_HOMES_PURGE)}: {violations}"
    )


def test_purge_scan_actually_found_a_real_mutation_primitive() -> None:
    """purge.py directly calls only `shutil.rmtree` (the `entry.is_dir` branch of
    `purge_expired`); its `os.unlink` happens through the imported `unlink_clear_readonly`
    wrapper, not a direct call in THIS file -- so only `shutil.rmtree` is expected non-empty
    here, unlike executor.py's all-four (see the test above)."""
    source = _PURGE_PATH.read_text(encoding="utf-8")
    homes = _primitive_call_homes(source, _FORBIDDEN_MUTATION_PRIMITIVES)
    assert homes["shutil.rmtree"], "scan found zero shutil.rmtree calls in purge.py"


# --- 2. Runtime shape: apply_batch's preflight guard precedes every mutation path --------------


def test_apply_batch_preflight_guard_precedes_every_mutation_reaching_statement() -> None:
    """Audit P0-1's structural guarantee: `apply_batch`'s per-candidate loop can never reach
    `_atomic_move` / `send2trash.send2trash` / `shutil.rmtree` (the direct-delete branch) without
    first passing through `_preflight_skip_reason` and its `if skip_reason is not None: continue`
    guard. Statement-list-position proof, not a behavioral one -- see
    `_assert_preflight_guard_precedes_mutation`'s docstring for why position alone suffices."""
    tree = ast.parse(_EXECUTOR_PATH.read_text(encoding="utf-8"))
    apply_batch_fn = _find_function(tree, "apply_batch")
    for_loop = _find_first_for_loop(apply_batch_fn)
    _assert_preflight_guard_precedes_mutation(for_loop.body)


# --- 3. Teeth: the same two adversarial cases the original audit used, against scratch copies --


def test_call_graph_gate_has_teeth_against_a_rogue_mutation_call_in_an_unrelated_function(
    tmp_path: Path,
) -> None:
    """Reproduces the original audit's first adversarial case: a `shutil.rmtree` call planted in
    a function that has no business making one. Built by appending to a COPY of the real source
    text (never mutating anything in place), written to a scratch file under `tmp_path`, parsed
    from there -- the real tracked executor.py is never touched."""
    real_source = _EXECUTOR_PATH.read_text(encoding="utf-8")
    rogue_source = real_source + (
        "\n\n"
        "def _rogue_unrelated_helper(path: str) -> None:\n"
        "    # planted only for this test, never present in the real tracked file -- proves the\n"
        "    # gate would catch a rogue call reaching a mutation primitive outside its allowed\n"
        "    # home, rather than merely asserting it in prose.\n"
        "    shutil.rmtree(path)\n"
    )
    scratch = tmp_path / "executor_scratch_rogue_call.py"
    scratch.write_text(rogue_source, encoding="utf-8")

    violations = _mutation_primitive_violations(
        scratch.read_text(encoding="utf-8"), _ALLOWED_HOMES_EXECUTOR
    )
    assert "_rogue_unrelated_helper" in violations.get("shutil.rmtree", []), (
        "the call-graph gate failed to flag a rogue shutil.rmtree call planted in an unrelated "
        "function -- it has no teeth"
    )


_BROKEN_ORDER_SCRATCH_SOURCE = """
def apply_batch(candidates):
    for candidate in candidates:
        vault_path = _compute_vault_path(candidate)
        try:
            _atomic_move(candidate.path, vault_path, is_dir=candidate.is_dir)
        except Exception as exc:
            pass
        skip_reason = _preflight_skip_reason(candidate)
        if skip_reason is not None:
            continue
"""


def test_call_graph_gate_has_teeth_against_a_reordered_guard_running_after_the_mutation(
    tmp_path: Path,
) -> None:
    """Reproduces the original audit's second adversarial case: the preflight guard moved to run
    AFTER the mutation call it's supposed to gate -- the exact bug shape this proof exists to
    catch. Uses a scratch, minimal-but-structurally-faithful stand-in for `apply_batch`'s
    per-candidate loop (a `for` loop; a mutation call inside a `try`; the guard) rather than a
    text-surgery copy of the real 1,500+-line executor.py: splicing exact substrings out of the
    real file would make this test break on any unrelated comment/formatting change to that file
    -- fragility unrelated to the safety property under test. The position-comparison ALGORITHM
    under test (`_assert_preflight_guard_precedes_mutation`) is identical either way, and is
    already proven against the real file by the positive test above.
    """
    scratch = tmp_path / "apply_batch_scratch_reordered_guard.py"
    scratch.write_text(_BROKEN_ORDER_SCRATCH_SOURCE, encoding="utf-8")

    tree = ast.parse(scratch.read_text(encoding="utf-8"))
    broken_apply_batch = _find_function(tree, "apply_batch")
    for_loop = _find_first_for_loop(broken_apply_batch)
    with pytest.raises(AssertionError, match="does not structurally precede"):
        _assert_preflight_guard_precedes_mutation(for_loop.body)


# --- 4. Closed set: apply_batch's real call sites can't grow silently ---------------------------


def test_apply_batch_real_callers_are_exactly_the_known_closed_set() -> None:
    """Audit-traced real call sites into `apply_batch`: `cli.py::_run_apply` (the CLI `apply`
    command) and `api/service.py::run_apply` (POST /api/apply's background task). A future PR
    adding a new caller elsewhere in `src/reclaim/` -- a bypass around the pre-flight/safety-
    re-check machinery this whole gate file exists to protect -- must touch this allowlist
    consciously rather than silently growing the real call-graph with nobody noticing (house rule
    85a: a control's surface should be named explicitly, not left to shrink or grow unnoticed).

    Deliberately scoped to `src/reclaim/` only (evals/tests/ legitimately call `apply_batch`
    directly in fixtures) and to calls literally spelled `apply_batch(...)` -- both known call
    sites import it via `from reclaim.executor import apply_batch` (confirmed by grep when this
    test was written), so a future `import reclaim.executor as x; x.apply_batch(...)` call style
    would need this scan taught the attribute form too, and would otherwise simply be invisible
    to `_files_calling` rather than falsely passing -- see `test_executor_scan_actually_found_
    every_real_mutation_primitive` above for the same "a scan finding nothing proves nothing"
    concern applied here: if this ever silently scanned zero files, `_EXPECTED_APPLY_BATCH_
    CALLER_FILES` (non-empty) would still make the equality assertion below fail loudly, not
    pass vacuously.
    """
    callers = _files_calling(_SRC_ROOT, "apply_batch")
    assert callers == set(_EXPECTED_APPLY_BATCH_CALLER_FILES), (
        f"apply_batch's real call sites changed: expected exactly "
        f"{sorted(_EXPECTED_APPLY_BATCH_CALLER_FILES)}, found {sorted(callers)}"
    )
