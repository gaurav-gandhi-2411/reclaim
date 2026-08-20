from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

# R7 (docs/AUDIT-2026-08.md): the MCP control surface's own safety gate, mirroring evals/
# test_ai_safety_gate.py's structure and stakes for reclaim.ai. Two guarantees, proven
# structurally (not by convention):
#
# 1. No file under src/reclaim/mcp/ imports reclaim.executor or send2trash directly -- every
#    real mutation is reached only through reclaim.api.service's choke-point functions.
# 2. No MCP tool registered on the real server accepts a raw path/paths parameter -- checked
#    against the REAL, live tool input schema (JSON Schema `properties`), not source inspection,
#    with a negative test proving this check has teeth (it fails against a deliberately-broken
#    tool built in this file only, never against the real module).

_MCP_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "reclaim" / "mcp"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="scanner targets Windows/NTFS only")


# --- 1. Static: no file under reclaim.mcp imports the executor or send2trash -------------------


def _imported_module_names(source: str) -> set[str]:
    """Identical logic to evals/test_ai_safety_gate.py's own helper (including its `from
    reclaim import executor` coverage) -- duplicated rather than imported from that eval module
    on purpose: an eval file importing another eval file as library code would blur which file
    is actually asserting what, and this function is small enough that duplicating it once is
    cheaper than the coupling."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_mcp_package_never_imports_the_executor_or_send2trash() -> None:
    """The structural half of R7's no-bypass guarantee: re-checked against every .py file under
    src/reclaim/mcp/ on every CI run, so a future PR that adds so much as one `import
    reclaim.executor` (or `send2trash`) anywhere in this package fails immediately here."""
    py_files = sorted(_MCP_PACKAGE_ROOT.rglob("*.py"))
    assert py_files, f"expected to find .py files under {_MCP_PACKAGE_ROOT}, found none"

    violations: dict[str, set[str]] = {}
    forbidden = {"reclaim.executor", "send2trash"}
    for py_file in py_files:
        imported = _imported_module_names(py_file.read_text(encoding="utf-8"))
        hit = {name for name in imported if name in forbidden or name.startswith("send2trash.")}
        if hit:
            violations[str(py_file.relative_to(_MCP_PACKAGE_ROOT.parent.parent.parent))] = hit

    assert violations == {}, (
        f"reclaim.mcp must never import the auto-delete executor or send2trash: {violations}"
    )


def test_api_service_mcp_functions_are_the_ones_that_import_the_executor() -> None:
    """Positive counterpart to the test above (same shape as evals/test_ai_safety_gate.py's
    `test_api_service_now_imports_reclaim_ai_for_read_only_suggestions`): `reclaim.api.service`
    IS expected to import `reclaim.executor` (it already did, for the HTTP apply path; R7 adds
    `mcp_execute_delete` alongside it, not a new import) -- asserted here so a future refactor
    that accidentally severs that wiring is caught, not just silently passing the negative test
    above by coincidence."""
    import inspect

    from reclaim.api import service

    # The real, load-bearing assertion: mcp_execute_delete's own source calls apply_batch --
    # the one place any of this codebase actually deletes/quarantines a file.
    source = inspect.getsource(service.mcp_execute_delete)
    assert "apply_batch(" in source


# --- 2. Runtime/schema: no registered MCP tool accepts a raw path parameter --------------------

# Case-insensitive, and checked against every property name (not just top-level ones) -- a tool
# could in principle nest a path inside an object/array-typed parameter instead of a bare string
# one; this check still catches a property named `path`/`paths` at ANY nesting depth so a future
# tool can't route around the letter of the guard by wrapping a path in a container type.
_FORBIDDEN_PROPERTY_NAMES = frozenset({"path", "paths"})


def _forbidden_property_paths(schema: object, *, prefix: str = "") -> list[str]:
    """Recursively walks a JSON Schema `dict`, returning a `prefix.name`-style path for every
    property whose name is `path`/`paths` (case-insensitive) at any depth. Pure and
    protocol-agnostic -- takes a plain schema dict, not an `mcp.types.Tool`, so the negative
    test below can hand it a deliberately-broken schema with zero real MCP server involved."""
    hits: list[str] = []
    if not isinstance(schema, dict):
        return hits
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, subschema in properties.items():
            here = f"{prefix}.{name}" if prefix else name
            if name.lower() in _FORBIDDEN_PROPERTY_NAMES:
                hits.append(here)
            hits.extend(_forbidden_property_paths(subschema, prefix=here))
    for key in ("items", "additionalProperties"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            hits.extend(_forbidden_property_paths(nested, prefix=f"{prefix}[{key}]"))
    return hits


# Deliberately NOT every registered tool: `scan(path)` legitimately takes a filesystem path --
# it's "which directory to build a read-only index of," never a delete target, and this
# package's own guarantee (see its module docstring) is scoped to "never forwards [a path]
# toward deletion." `preview_apply`/`delete` are the two tools that select what gets deleted --
# selecting by path instead of by rule/category is exactly the R7 gap this whole package exists
# to close, so these are the two names this guard actually polices.
_SELECTION_TOOL_NAMES = frozenset({"preview_apply", "delete"})


def assert_no_tool_accepts_a_raw_path(tools: object) -> None:
    """The real guard, reused by both this file's own tests and `tests/test_mcp.py`'s live-
    server check -- `tools` is whatever `mcp.types.Tool` objects (or, for the negative test
    below, plain stand-ins with a `.name`/`.inputSchema`) the caller collected. Checks ONLY
    `_SELECTION_TOOL_NAMES` (see its own comment for why `scan` is deliberately excluded) --
    but fails loudly, not vacuously, if neither expected tool is even present in `tools` at all
    (a caller error -- e.g. handing this the wrong tool list -- must not silently "pass" by
    having nothing to check). Raises `AssertionError` naming every offending tool/property path;
    asserts nothing itself so a caller controls how the failure is reported."""
    tools = list(tools)  # type: ignore[arg-type]
    checked = [tool for tool in tools if tool.name in _SELECTION_TOOL_NAMES]
    assert checked, (
        f"expected at least one of {sorted(_SELECTION_TOOL_NAMES)} in the tool list handed to "
        f"this guard, found none among {[t.name for t in tools]} -- this would otherwise pass "
        "vacuously without checking anything"
    )
    violations: dict[str, list[str]] = {}
    for tool in checked:
        hits = _forbidden_property_paths(tool.inputSchema)
        if hits:
            violations[tool.name] = hits
    assert violations == {}, (
        f"MCP tool(s) accept a raw path/paths parameter -- R7's structural guarantee is broken: "
        f"{violations}"
    )


def test_real_mcp_server_has_no_tool_accepting_a_raw_path() -> None:
    """The live check: builds the REAL server (same construction `reclaim.mcp.server.
    build_mcp_server` uses in production) and inspects its ACTUAL registered tool schemas --
    proves the guarantee holds for what a real MCP client would actually see, not just for
    what the source code appears to say."""
    import asyncio

    from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig
    from reclaim.mcp.server import build_mcp_server, build_state

    async def _list_tools(tmp_path: Path) -> list[object]:
        state = build_state(
            db_path=tmp_path / "index.sqlite3",
            config=Config(
                categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True))
            ),
            vault_dir=tmp_path / "vault",
            manifest_path=tmp_path / "manifest.jsonl",
            mode_log_path=tmp_path / "mode_log.jsonl",
            first_run_state_path=tmp_path / "first_run_state.json",
            log_path=tmp_path / "reclaim.log",
        )
        server = build_mcp_server(state)
        return await server.list_tools()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tools = asyncio.run(_list_tools(Path(tmp_dir)))

    assert tools, "expected reclaim.mcp.server to register at least one tool"
    tool_names = {t.name for t in tools}
    assert {"scan", "scan_status", "list_candidates", "preview_apply", "delete"} <= tool_names
    assert_no_tool_accepts_a_raw_path(tools)


class _FakeTool:
    """Minimal stand-in for `mcp.types.Tool` -- only the two attributes
    `assert_no_tool_accepts_a_raw_path` actually reads."""

    def __init__(self, name: str, input_schema: dict[str, object]) -> None:
        self.name = name
        self.inputSchema = input_schema


def test_the_guard_itself_fails_against_a_deliberately_broken_tool_schema() -> None:
    """Negative test proving `assert_no_tool_accepts_a_raw_path` has teeth (rule 99: a guard
    with no failing case is unproven) -- reintroduces the EXACT shape R7 exists to forbid
    (today's real `POST /api/apply`'s free-form `paths: list[str]` field, `api/schemas.py`'s
    `ApplyRequest.paths`) as a fake tool schema, entirely within this test -- the real
    `reclaim.mcp` module is never touched or weakened."""
    offending_tools = [
        _FakeTool(
            name="delete",
            input_schema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "tier": {"type": "string"},
                },
                "required": ["paths"],
            },
        )
    ]
    with pytest.raises(AssertionError, match="delete"):
        assert_no_tool_accepts_a_raw_path(offending_tools)

    # And a nested case -- a `path` buried inside an object-typed parameter must be caught too.
    # Uses one of the two real policed tool names ("preview_apply") -- an unrecognized tool name
    # would be filtered out by the guard's own "only check selection tools" scoping (see
    # `_SELECTION_TOOL_NAMES`) and this negative test would then prove nothing.
    nested_offender = [
        _FakeTool(
            name="preview_apply",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    }
                },
            },
        )
    ]
    with pytest.raises(AssertionError, match="preview_apply"):
        assert_no_tool_accepts_a_raw_path(nested_offender)

    # Sanity: a genuinely clean schema (no path/paths anywhere) passes without raising.
    clean_tools = [
        _FakeTool(
            name="preview_apply",
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {"type": "string"},
                    "rule_id_or_category": {"type": "string"},
                    "tier": {"type": "string"},
                },
            },
        )
    ]
    assert_no_tool_accepts_a_raw_path(clean_tools)  # must not raise


def test_the_guard_refuses_to_pass_vacuously_when_handed_no_relevant_tools() -> None:
    """A tool list that never even includes `preview_apply`/`delete` (e.g. a caller error
    handing this the wrong list) must fail loudly, not silently 'pass' by having found nothing
    to check -- see `assert_no_tool_accepts_a_raw_path`'s own docstring for why (rule 98a: a
    guard's own data must fail closed, never open, on an ambiguous/empty input)."""
    unrelated_tools = [
        _FakeTool(name="scan", input_schema={"properties": {"path": {"type": "string"}}}),
        _FakeTool(name="scan_status", input_schema={"properties": {}}),
    ]
    with pytest.raises(AssertionError, match="expected at least one"):
        assert_no_tool_accepts_a_raw_path(unrelated_tools)
