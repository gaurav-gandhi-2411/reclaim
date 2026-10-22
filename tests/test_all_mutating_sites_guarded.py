from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "reclaim"

# The guard functions from reclaim.safety_env. A mutating site is "guarded" when one of these is
# called earlier in the same (or an enclosing) function.
GUARD_NAMES = frozenset(
    {"assert_not_real_profile_under_pytest", "refuse_real_side_effect_under_pytest"}
)

# Dotted callables that mutate the filesystem / registry / processes regardless of arguments.
_MUTATING_DOTTED = frozenset(
    {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.removedirs",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.makedirs",
        "os.mkdir",
        "os.truncate",
        "os.startfile",
        "os.system",
        "os.symlink",
        "os.link",
        "os.chmod",
        "os.utime",
        "os.write",
        "shutil.rmtree",
        "shutil.move",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "tempfile.mkstemp",
        "tempfile.mkdtemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "tempfile.TemporaryFile",
        "os.fdopen",
        "sqlite3.connect",
        "winreg.SetValue",
        "winreg.SetValueEx",
        "winreg.DeleteKey",
        "winreg.DeleteValue",
        "winreg.CreateKey",
        "winreg.CreateKeyEx",
        "winreg.DeleteKeyEx",
    }
)
_MUTATING_DOTTED_PREFIXES = ("subprocess.", "send2trash.")
# ctypes.windll.<dll>.<Function> file/process operations (read-only queries such as
# GetLogicalDrives or IsUserAnAdmin are deliberately not matched).
_CTYPES_MUTATING = re.compile(
    r"^ctypes\.windll\.\w+\.(DeleteFile|MoveFile|CopyFile|CreateFile|RemoveDirectory|"
    r"SetFileAttributes|CreateSymbolicLink|CreateDirectory|CreateHardLink|DeviceIoControl|"
    r"ShellExecute|SHFileOperation|WriteFile|SetEndOfFile|CreateProcess)"
)
# Method names that mutate on a Path-like receiver whatever the argument shape.
_MUTATING_METHODS = frozenset(
    {
        "unlink",
        "rmdir",
        "rename",
        "write_text",
        "write_bytes",
        "touch",
        "mkdir",
        "truncate",
        "symlink_to",
        "hardlink_to",
        "chmod",
        "backup",  # sqlite3.Connection.backup writes a whole database file
        "load_extension",  # loads and runs native code
    }
)
_SQL_MUTATING = ("DELETE", "DROP", "VACUUM", "UPDATE", "INSERT", "REPLACE", "ALTER", "CREATE")
_WRITE_MODE_CHARS = set("wax+")
# Statements beyond the _SQL_MUTATING verbs that change a database: ATTACH/DETACH open or close
# another file, REINDEX/ANALYZE rewrite the index/statistics tables.
_SQL_MUTATING_EXTRA = ("ATTACH", "DETACH", "REINDEX", "ANALYZE")
_CTE_WRITE = re.compile(r"\b(DELETE|UPDATE|INSERT|REPLACE\s+INTO)\b", re.IGNORECASE)
# PRAGMAs that write even without an `=` (a checkpoint truncates the WAL file on disk).
_WRITING_PRAGMAS = frozenset({"wal_checkpoint", "optimize", "incremental_vacuum", "shrink_memory"})
# `PRAGMA name(value)` is the call form of an assignment; these are the read-only pragmas that take
# an argument. Anything else with an argument is treated as a write.
_READ_ONLY_CALL_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "table_list",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "foreign_key_check",
        "integrity_check",
        "quick_check",
        "database_list",
        "collation_list",
        "function_list",
        "module_list",
        "pragma_list",
        "compile_options",
    }
)
# Stands for a part of a SQL string that is not statically known (a parameter, a call result).
_UNKNOWN = "\ufffc"
_EXECUTE_NAMES = ("execute", "executemany", "executescript")


def _sql_skeleton(sql: str) -> str:
    """`sql` with comments removed and the contents of string literals / quoted identifiers
    emptied, so a `;`, `--` or a keyword inside a literal can neither split a statement, swallow
    the rest as a comment, nor look like a verb."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # doubled quote = escaped quote
                        j += 2
                        continue
                    break
                j += 1
            out.append(c + c)
            i = j + 1
        elif c == "[":
            end = sql.find("]", i)
            out.append("[]")
            i = n if end < 0 else end + 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            out.append(" ")
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            out.append(" ")
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _mutating_sql_verb(sql: str) -> str | None:
    """The verb that makes `sql` (static text, possibly several `;`-separated statements, possibly
    with comments) a write, or None when every statement is a read; "?" when a statement STARTS
    with unknown text (so its verb cannot be seen). A `WITH ... DELETE/UPDATE/INSERT` is a write
    whatever the CTE looks like; a PRAGMA is a write when it assigns a value, is a checkpoint-style
    pragma, or is called with an argument and is not a known read-only one."""
    for statement in _sql_skeleton(sql).split(";"):
        stripped = statement.lstrip(" \t\r\n\f\v(")
        if not stripped:
            continue
        if stripped.startswith(_UNKNOWN):
            return "?"
        word = re.match(r"[A-Za-z_]+", stripped)
        if word is None:
            continue
        first = word.group(0).upper()
        if first in _SQL_MUTATING or first in _SQL_MUTATING_EXTRA:
            return first
        if first == "WITH" and (cte := _CTE_WRITE.search(stripped)):
            return f"WITH {cte.group(1).split()[0].upper()}"
        if first == "PRAGMA":
            rest = stripped[word.end() :]
            name_match = re.match(r"\s*(?:\w+\.)?(\w+)", rest)
            name = name_match.group(1).lower() if name_match else ""
            if (
                "=" in rest
                or name in _WRITING_PRAGMAS
                or ("(" in rest and name not in _READ_ONLY_CALL_PRAGMAS)
            ):
                return "PRAGMA"
    return None


@dataclass(frozen=True)
class Site:
    file: str
    function: str
    line: int
    what: str
    guarded: bool

    @property
    def key(self) -> str:
        return f"{self.file}:{self.function}"


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _mode_is_write(node: ast.Call, mode_index: int) -> bool:
    mode: ast.AST | None = None
    if len(node.args) > mode_index:
        mode = node.args[mode_index]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(_WRITE_MODE_CHARS & set(mode.value))
    # A non-literal mode cannot be proven read-only: treat as mutating (fail closed).
    return mode is not None


def _method_open_is_write(node: ast.Call) -> bool:
    """`Path.open(mode)` with a literal write mode, or any `mode=` keyword that is not a read
    literal. A non-literal POSITIONAL first argument is a path (`PIL.Image.open(path)`), not a
    mode, so it is not flagged; a `Path.open(mode_variable)` would be missed (known limit)."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return _mode_is_write(node, 0)
    return any(kw.arg == "mode" for kw in node.keywords) and _mode_is_write(node, 99)


def _static_text(node: ast.AST, consts: dict[str, str]) -> str | None:
    """The statically known text of a SQL expression: a string literal, an f-string, an implicit
    or explicit `+` concatenation, or a name bound to one. Unknown pieces AFTER a known start are
    kept as `_UNKNOWN` so that a statement that begins with unknown text (`"SELECT 1; " + w`) is
    seen as such; None when even the start cannot be determined (a parameter, an attribute, a
    call result)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            elif isinstance(part, ast.FormattedValue):
                known = _static_text(part.value, consts)
                if known is None:
                    if not out:
                        return None
                    known = _UNKNOWN
                out += known
        return out or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left, consts)
        if left is None:
            return None
        right = _static_text(node.right, consts)
        return left + (right if right is not None else _UNKNOWN)
    return None


def _sql_text(node: ast.Call, consts: dict[str, str] | None = None) -> str | None:
    """The leading constant text of a SQL-ish first argument, None if it is not static."""
    if not node.args:
        return None
    return _static_text(node.args[0], consts or {})


def classify_call(node: ast.Call, consts: dict[str, str] | None = None) -> str | None:
    """Describes why `node` is a mutating call, or None when it is not."""
    func = node.func
    dotted = _dotted(func)
    if dotted is not None and (
        dotted in _MUTATING_DOTTED or dotted.startswith(_MUTATING_DOTTED_PREFIXES)
    ):
        return dotted
    if dotted is not None and _CTYPES_MUTATING.match(dotted):
        return dotted
    if isinstance(func, ast.Name) and func.id == "open" and _mode_is_write(node, 1):
        return "open(write)"
    if isinstance(func, ast.Attribute):
        name = func.attr
        if name in _MUTATING_METHODS:
            return f".{name}()"
        if name == "replace" and len(node.args) == 1 and not node.keywords:
            return ".replace(1 arg)"  # Path.replace; str.replace always takes >= 2 args
        if name == "open" and _method_open_is_write(node):
            return ".open(write)"
        if name.endswith("FileHandler"):
            return f".{name}()"  # logging file handlers create/append their file
        if name in _EXECUTE_NAMES:
            sql = _sql_text(node, consts)
            if sql is None:
                # Cannot be proven read-only (the SQL arrives as a parameter/attribute/call
                # result, e.g. ScanIndex._flush_hash_rows(sql, rows)): fail closed, like a
                # non-literal open() mode. A reader of such a call must allowlist it with a reason.
                return "sql <non-literal>"
            verb = _mutating_sql_verb(sql)
            if verb == "?":
                return "sql <non-literal>"  # a later statement starts with unknown text
            if verb is not None:
                return f"sql {verb}"
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
        and len(func.args) >= 2
        and isinstance(func.args[1], ast.Constant)
        and func.args[1].value in (*_MUTATING_METHODS, *_EXECUTE_NAMES)
    ):
        # `getattr(conn, "execute")(sql)`: the attribute is spelled out, so the call is a
        # mutation or an opaque execute, never a read. (`getattr(x, name_variable)(...)` stays a
        # known limit: nothing can be said about it statically.)
        return f"getattr {func.args[1].value}"
    return None


def _is_guard_call(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in GUARD_NAMES


def _target_names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
Bindings = dict[str, list[tuple[str, ast.AST | None]]]


def _scope_nodes(node: ast.AST, descend_defs: bool) -> list[ast.AST]:
    """Source-ordered nodes of a scope. With descend_defs=False a nested def/class/lambda is
    yielded (its own name is a binding) but not entered (its locals are another scope)."""
    out: list[ast.AST] = [node]
    for child in ast.iter_child_nodes(node):
        if not descend_defs and isinstance(child, _DEFS):
            out.append(child)
        else:
            out.extend(_scope_nodes(child, descend_defs))
    return out


def _collect_bindings(root: ast.AST, *, descend_defs: bool) -> Bindings:
    """Every binding of every name in `root`, by kind: ("assign", value) for `name = value`,
    ("aug", value) for `name += value`, ("other", None) for anything the resolver does not model
    (a parameter, a for/with/except/walrus/comprehension/match target, tuple unpacking, an import,
    a def/class name, global/nonlocal). A name with an "other" binding is treated as unknown, so
    shadowing a constant by any binding form fails closed."""
    out: Bindings = {}

    def add(name: str, kind: str, value: ast.AST | None = None) -> None:
        out.setdefault(name, []).append((kind, value))

    for n in _scope_nodes(root, descend_defs):
        if isinstance(n, ast.Assign):
            if len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                add(n.targets[0].id, "assign", n.value)
            else:
                for t in n.targets:
                    for name in _target_names(t):
                        add(name, "other")
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.value is not None:
                add(n.target.id, "assign", n.value)
            else:
                add(n.target.id, "other")
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.op, ast.Add):
                add(n.target.id, "aug", n.value)
            else:
                add(n.target.id, "other")
        elif isinstance(n, ast.NamedExpr):
            add(n.target.id, "other")
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in _target_names(n.target):
                add(name, "other")
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            for name in _target_names(n.optional_vars):
                add(name, "other")
        elif (isinstance(n, ast.ExceptHandler) and n.name) or (
            isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name
        ):
            add(n.name, "other")
        elif isinstance(n, ast.MatchMapping) and n.rest:
            add(n.rest, "other")
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                add((alias.asname or alias.name).split(".")[0], "other")
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            for name in n.names:
                add(name, "other")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(n.name, "other")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and (
            n is root or descend_defs
        ):
            a = n.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None:
                    add(arg.arg, "other")
    return out


def _first_word(text: str) -> str:
    match = re.match(r"[\s(]*([A-Za-z_]+)", text, re.ASCII)
    return match.group(1).upper() if match else ""


def _apply_bindings(consts: dict[str, str], binds: Bindings) -> None:
    """Overlay `binds` on `consts`. A name resolves only when ALL its bindings in the scope (not
    just earlier lines: a loop can run a later rebinding before the call) are plain assignments
    to static text with one common SQL verb; `+=` of static text is appended, `+=` of unknown text
    appends the unknown marker. Anything else makes the name unknown."""
    for name, entries in binds.items():
        consts.pop(name, None)
        if any(kind == "other" for kind, _ in entries):
            continue
        assigns = [_static_text(v, consts) for k, v in entries if k == "assign" and v]
        if not assigns or None in assigns:
            continue
        verbs = {_first_word(t or "") for t in assigns}
        if len(verbs) != 1 or "" in verbs:
            continue
        tails = []
        for kind, value in entries:
            if kind == "aug" and value is not None:
                text = _static_text(value, consts)
                tails.append(text if text is not None else _UNKNOWN)
        consts[name] = "; ".join(t for t in assigns if t) + "".join(tails)


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str, tree: ast.Module) -> None:
        self.file = file
        self.rebound_globals = {
            name for n in ast.walk(tree) if isinstance(n, ast.Global) for name in n.names
        }
        consts: dict[str, str] = {}
        _apply_bindings(consts, _collect_bindings(tree, descend_defs=False))
        self.consts = {k: v for k, v in consts.items() if k not in self.rebound_globals}
        self.stack: list[str] = []
        # per enclosing function: lines of guard calls found so far in its body
        self.guard_lines: list[list[int]] = []
        # per enclosing scope (function or class body): its bindings (_collect_bindings)
        self.local_binds: list[Bindings] = []
        self.call_targets: set[int] = set()
        self.raw: list[tuple[str, int, str, list[int]]] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        is_func = not isinstance(node, ast.ClassDef)
        if is_func:
            lines = [
                n.lineno for n in ast.walk(node) if isinstance(n, ast.Call) and _is_guard_call(n)
            ]
            self.guard_lines.append(lines)
            # nested functions are flattened in: an inner function reading an outer local is
            # affected by every rebinding of it
            self.local_binds.append(_collect_bindings(node, descend_defs=True))
        else:
            self.local_binds.append(_collect_bindings(node, descend_defs=False))
        self.generic_visit(node)
        self.local_binds.pop()
        if is_func:
            self.guard_lines.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def _constants_here(self) -> dict[str, str]:
        consts = dict(self.consts)
        for binds in self.local_binds:
            _apply_bindings(consts, binds)
        return consts

    def _record(self, node: ast.AST, what: str) -> None:
        function = ".".join(self.stack) or "<module>"
        all_guards = [ln for lines in self.guard_lines for ln in lines]
        self.raw.append((function, node.lineno, what, all_guards))  # type: ignore[attr-defined]

    def visit_Call(self, node: ast.Call) -> None:
        self.call_targets.add(id(node.func))
        what = classify_call(node, self._constants_here())
        if what is not None:
            self._record(node, what)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # `run = conn.execute; run("DELETE ...")`, `functools.partial(conn.execute, ...)`,
        # `map(conn.execute, ...)`: the method is passed around instead of called, so its SQL
        # cannot be seen.
        if node.attr in _EXECUTE_NAMES and id(node) not in self.call_targets:
            self._record(node, f"{node.attr} reference")
        self.generic_visit(node)


def scan_source(source: str, file: str) -> list[Site]:
    """Every mutating call in `source`; guarded = a guard call precedes it inside an enclosing
    function (lexically, by line)."""
    tree = ast.parse(source)
    visitor = _Visitor(file, tree)
    visitor.visit(tree)
    return [
        Site(file, function, line, what, any(g <= line for g in guards))
        for function, line, what, guards in visitor.raw
    ]


def scan_tree(root: Path = SRC) -> list[Site]:
    sites: list[Site] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == "safety_env.py":
            continue
        sites.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return sites


# `file:function` -> reason this unguarded site is safe under pytest. Categories (ADR-0034
# addendum "Hermetic tests"): (1) read-only subprocess; (2) SQL on a connection that can only be
# opened by a constructor which itself calls the guard on the database path.
_SQL_ON_GUARDED_CONNECTION = (
    "SQL on a connection opened only by ScanIndex.__init__, which guards db_path; the "
    "destructive methods (prune/delete/VACUUM/replace) additionally guard directly"
)
ALLOWLIST: dict[str, str] = {
    "ai/eval_harness.py:current_commit_sha": "read-only subprocess: `git rev-parse HEAD`",
    "scanner.py:_query_git_clean": (
        "read-only subprocesses: `git --no-optional-locks status --porcelain` and `ls-files -v` "
        "(without the flag git status rewrites .git/index; "
        "test_query_git_clean_never_writes_the_repo_index)"
    ),
    "notifications.py:ensure_toast_aumid": (
        "HKCU registry write (#158). Returns before touching winreg whenever "
        "PYTEST_CURRENT_TEST is set, so a test can never write the real profile; the "
        "registration test removes that variable only while injecting a fake winreg module"
    ),
    "index.py:ScanIndex._flush_hash_rows": (
        "executemany(sql, rows) where `sql` is a parameter, so the scanner cannot see its text. "
        "Its only callers are ScanIndex.store_partial_hashes/store_full_hashes, which pass an "
        "UPDATE of the hash columns of existing rows (both build the literal inline), on the "
        "connection opened only by ScanIndex.__init__, which guards db_path (#150/#151)"
    ),
    "ai/image_embeddings.py:ImageEmbeddingCache.put": (
        "SQL on a connection opened only by ImageEmbeddingCache.__init__, which guards db_path"
    ),
    **{
        f"index.py:ScanIndex.{name}": _SQL_ON_GUARDED_CONNECTION
        for name in (
            "_ensure_name_and_path_lower_columns",
            "_ensure_opaque_columns",
            "_backfill_name_and_path_lower",
            "upsert_rows",
            "begin_scan_tracking",
            "record_seen",
            "protect_under",
            "end_scan_tracking",
            # ANALYZE / WAL-checkpoint pragmas: they rewrite only the index's own statistics
            # tables and WAL file (visible once the scanner learned PRAGMA/ANALYZE are writes)
            "refresh_planner_stats",
            "checkpoint_wal",
            "close",
        )
    },
}


def _report(sites: list[Site], allowlist: dict[str, str]) -> str:
    lines = ["classification of every mutating site (G=guarded, A=allowlisted, !=UNGUARDED):"]
    for s in sorted(sites, key=lambda s: (s.file, s.line)):
        mark = "G" if s.guarded else ("A" if s.key in allowlist else "!")
        lines.append(f"  {mark} {s.file}:{s.line} {s.function} [{s.what}]")
    return "\n".join(lines)


def check_sites(sites: list[Site], allowlist: dict[str, str]) -> list[str]:
    """Problems: unguarded sites that are not allowlisted, and stale/redundant allowlist keys."""
    problems = [
        f"UNGUARDED mutating call {s.file}:{s.line} in {s.function} [{s.what}]: call "
        f"reclaim.safety_env.assert_not_real_profile_under_pytest before it, or add "
        f"'{s.key}' to ALLOWLIST with a reason"
        for s in sites
        if not s.guarded and s.key not in allowlist
    ]
    unguarded_keys = {s.key for s in sites if not s.guarded}
    problems += [
        f"STALE ALLOWLIST entry {key!r}: no unguarded mutating site with that key exists"
        for key in allowlist
        if key not in unguarded_keys
    ]
    return problems


def test_every_mutating_site_in_src_is_guarded_or_allowlisted() -> None:
    sites = scan_tree()
    assert len(sites) > 50, "scanner found suspiciously few sites; it is broken"
    problems = check_sites(sites, ALLOWLIST)
    assert not problems, "\n".join(problems) + "\n" + _report(sites, ALLOWLIST)


def test_allowlist_reasons_are_non_empty() -> None:
    assert all(reason.strip() for reason in ALLOWLIST.values())


# --- self-test of the scanner on synthetic modules -------------------------------------------

_UNGUARDED = """
import shutil

def wipe(path):
    shutil.rmtree(path)
"""

_GUARDED = """
import shutil
from reclaim.safety_env import assert_not_real_profile_under_pytest

def wipe(path):
    assert_not_real_profile_under_pytest(path)
    shutil.rmtree(path)
"""

_GUARD_AFTER = """
import os
from reclaim.safety_env import assert_not_real_profile_under_pytest

def wipe(path):
    os.remove(path)
    assert_not_real_profile_under_pytest(path)
"""


def test_scanner_flags_an_unguarded_rmtree() -> None:
    sites = scan_source(_UNGUARDED, "synthetic.py")
    assert [(s.what, s.guarded) for s in sites] == [("shutil.rmtree", False)]
    assert check_sites(sites, {})  # fails: someone added an unguarded mutating call


def test_scanner_accepts_a_guarded_rmtree_and_rejects_guard_after_the_call() -> None:
    assert [s.guarded for s in scan_source(_GUARDED, "synthetic.py")] == [True]
    assert check_sites(scan_source(_GUARDED, "synthetic.py"), {}) == []
    assert [s.guarded for s in scan_source(_GUARD_AFTER, "synthetic.py")] == [False]


def test_scanner_allowlist_must_not_go_stale() -> None:
    sites = scan_source(_GUARDED, "synthetic.py")
    problems = check_sites(sites, {"synthetic.py:gone": "was removed"})
    assert len(problems) == 1 and "STALE" in problems[0]


def test_scanner_recognises_every_mutation_shape() -> None:
    source = """
def f(p, fh, conn):
    p.unlink()
    p.write_text("x")
    p.write_bytes(b"x")
    p.mkdir()
    p.replace("dst")
    p.open("w")
    open("f", "a")
    conn.execute("DELETE FROM files")
    conn.execute(f"VACUUM {p}")
    os.makedirs("d")
    subprocess.run(["x"])
    send2trash.send2trash("x")
    logging.handlers.RotatingFileHandler("x")
    ctypes.windll.kernel32.DeleteFileW("x")
"""
    kinds = {s.what for s in scan_source(source, "synthetic.py")}
    assert {
        ".unlink()",
        ".write_text()",
        ".write_bytes()",
        ".mkdir()",
        ".replace(1 arg)",
        ".open(write)",
        "open(write)",
        "sql DELETE",
        "sql VACUUM",
        "os.makedirs",
        "subprocess.run",
        "send2trash.send2trash",
        ".RotatingFileHandler()",
        "ctypes.windll.kernel32.DeleteFileW",
    } <= kinds


def test_scanner_flags_sql_it_cannot_see_and_resolves_what_it_can() -> None:
    source = """
_DELETE_ALL = "DELETE FROM files"
_BASE = "SELECT 1"

def opaque(conn, sql, rows):
    conn.executemany(sql, rows)

def attribute(self):
    self._conn.execute(self._sql)

def hidden_verb(conn, table):
    conn.execute(f"{table} SET x = 1")

def through_a_module_constant(conn):
    conn.execute(_DELETE_ALL)

def through_a_local(conn, where):
    sql = "UPDATE files SET size = 0"
    sql += where
    conn.execute(sql)

def concatenated(conn):
    conn.execute("DROP " + "TABLE files")

def branches_with_different_verbs(conn, flag):
    if flag:
        sql = "SELECT 1"
    else:
        sql = "DELETE FROM files"
    conn.execute(sql)

def reads_are_fine(conn, where):
    sql = f"{_BASE} FROM files"
    conn.execute(sql + where)
    q = "SELECT 2"
    conn.execute(q)
"""
    by_function = {s.function: s.what for s in scan_source(source, "synthetic.py")}
    assert by_function == {
        "opaque": "sql <non-literal>",
        "attribute": "sql <non-literal>",
        "hidden_verb": "sql <non-literal>",
        "through_a_module_constant": "sql DELETE",
        "through_a_local": "sql UPDATE",
        "concatenated": "sql DROP",
        "branches_with_different_verbs": "sql <non-literal>",
    }
    # the non-literal ones are failures until allowlisted with a reason
    assert any(
        "sql <non-literal>" in problem
        for problem in check_sites(scan_source(source, "synthetic.py"), {})
    )


def test_scanner_catches_the_sql_evasions_found_by_the_verifier() -> None:
    source = """
_S = "SELECT 1"
_SAFE = "SELECT 2"

def cte_write(conn):
    conn.execute("WITH x AS (SELECT 1) DELETE FROM files WHERE id IN (SELECT * FROM x)")

def block_comment(conn):
    conn.execute("/* tidy */ DELETE FROM files")

def line_comment(conn):
    conn.execute("-- tidy\\nUPDATE files SET size = 0")

def multi_statement(conn):
    conn.executescript("SELECT 1; DELETE FROM files")

def pragma_assign(conn):
    conn.execute("PRAGMA journal_mode=WAL")

def pragma_checkpoint(conn):
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

def attach(conn):
    conn.execute("ATTACH DATABASE 'x.db' AS x")

def analyze(conn):
    conn.execute("ANALYZE")

def getattr_dispatch(conn, sql):
    getattr(conn, "execute")(sql)

def loop_rebind_after_the_call(conn, rows):
    s = "SELECT 1"
    for row in rows:
        conn.execute(s)
        s = "DELETE FROM files"

def plus_equals_second_statement(conn):
    s = "SELECT 1"
    s += "; DELETE FROM files"
    conn.execute(s)

def shadowed_by_annotated_local(conn):
    _S: str = "DELETE FROM files"
    conn.execute(_S)

def shadowed_by_walrus(conn):
    if (_S := conn.fetch_sql()):
        conn.execute(_S)

def shadowed_by_loop_variable(conn, statements):
    for _S in statements:
        conn.execute(_S)

def shadowed_by_parameter(conn, _S):
    conn.execute(_S)

def shadowed_by_with(conn, factory):
    with factory() as _S:
        conn.execute(_S)

def shadowed_by_unpacking(conn, pair):
    _S, other = pair
    conn.execute(_S)

def global_rebind_elsewhere(conn):
    conn.execute(_SAFE)

def rebinds_the_global():
    global _SAFE
    _SAFE = "DELETE FROM files"

def nested_reads_outer_local_rebound_later(conn):
    s = "SELECT 1"

    def inner():
        conn.execute(s)

    s = "DELETE FROM files"
    inner()

def readonly_pragma_and_selects(conn):
    conn.execute("PRAGMA table_info(files)")
    conn.execute("PRAGMA integrity_check")
    conn.execute("WITH x AS (SELECT 1) SELECT * FROM x")
    conn.execute("SELECT REPLACE(name, 'a', 'b') FROM files")
    conn.execute("-- just a note\\nSELECT 1; SELECT 2")
"""
    by_function = {s.function: s.what for s in scan_source(source, "synthetic.py")}
    assert by_function == {
        "cte_write": "sql WITH DELETE",
        "block_comment": "sql DELETE",
        "line_comment": "sql UPDATE",
        "multi_statement": "sql DELETE",
        "pragma_assign": "sql PRAGMA",
        "pragma_checkpoint": "sql PRAGMA",
        "attach": "sql ATTACH",
        "analyze": "sql ANALYZE",
        "getattr_dispatch": "getattr execute",
        "loop_rebind_after_the_call": "sql <non-literal>",
        "plus_equals_second_statement": "sql DELETE",
        "shadowed_by_annotated_local": "sql DELETE",
        "shadowed_by_walrus": "sql <non-literal>",
        "shadowed_by_loop_variable": "sql <non-literal>",
        "shadowed_by_parameter": "sql <non-literal>",
        "shadowed_by_with": "sql <non-literal>",
        "shadowed_by_unpacking": "sql <non-literal>",
        "global_rebind_elsewhere": "sql <non-literal>",
        "nested_reads_outer_local_rebound_later.inner": "sql <non-literal>",
    }


def _whats(source: str) -> dict[str, str]:
    return {site.function: site.what for site in scan_source(source, "synthetic.py")}


def test_scanner_is_quote_aware_and_reads_pragma_call_forms() -> None:
    source = """
def comment_marker_in_a_string(conn):
    conn.executescript("SELECT '--'; DELETE FROM t")

def block_comment_markers_in_strings(conn):
    conn.executescript("SELECT '/*'; DELETE FROM t; SELECT '*/'")

def pragma_call_form(conn):
    conn.execute("PRAGMA user_version(5)")

def pragma_journal_call_form(conn):
    conn.execute("PRAGMA journal_mode(WAL)")

def verb_inside_a_literal_is_not_a_verb(conn):
    conn.execute("WITH x AS (SELECT 'update' AS a) SELECT * FROM x")
    conn.execute("SELECT ';DELETE FROM t'")
    conn.execute("SELECT 'it''s; DELETE'")

def replace_function_is_a_read(conn):
    conn.execute("WITH x AS (SELECT 1) SELECT REPLACE(a, 'b', 'c') FROM x")

def read_only_pragma_with_an_argument(conn):
    conn.execute("PRAGMA table_info(optimize)")
    conn.execute("PRAGMA main.index_list(files)")
"""
    assert _whats(source) == {
        "comment_marker_in_a_string": "sql DELETE",
        "block_comment_markers_in_strings": "sql DELETE",
        "pragma_call_form": "sql PRAGMA",
        "pragma_journal_call_form": "sql PRAGMA",
    }


def test_scanner_sees_a_statement_that_starts_with_unknown_text() -> None:
    source = """
def concatenated_tail(conn, w):
    conn.execute("SELECT 1; " + w)

def fstring_tail(conn, w):
    conn.execute(f"SELECT 1; {w}")

def augmented_tail(conn, w):
    s = "SELECT 1;"
    s += w
    conn.execute(s)

def unknown_tail_inside_one_statement_is_fine(conn, w):
    conn.execute("SELECT 1 FROM t WHERE x = " + w)
    conn.execute(f"SELECT 1 FROM t WHERE x IN ({w})")
    s = "SELECT 1 FROM t"
    s += w
    conn.execute(s)
"""
    assert _whats(source) == {
        "concatenated_tail": "sql <non-literal>",
        "fstring_tail": "sql <non-literal>",
        "augmented_tail": "sql <non-literal>",
    }


def test_scanner_resolves_module_constants_with_the_same_rules_as_locals() -> None:
    cases = {
        "branch": 'Q = "SELECT 1"\nif flag:\n    Q = "DELETE FROM t"\n',
        "augmented": 'Q = "SELECT 1"\nQ += "; DELETE FROM t"\n',
        "loop_target": 'Q = "SELECT 1"\nfor Q in statements:\n    pass\n',
        "import_alias": 'Q = "SELECT 1"\nimport os as Q\n',
        "late_import": 'Q = "SELECT 1"\nfrom elsewhere import Q\n',
        "walrus": 'Q = "SELECT 1"\nif (Q := compute()):\n    pass\n',
        "def_name": 'Q = "SELECT 1"\ndef Q():\n    pass\n',
        "class_name": 'Q = "SELECT 1"\nclass Q:\n    pass\n',
    }
    expected = {
        "branch": "sql <non-literal>",
        "augmented": "sql DELETE",
        "loop_target": "sql <non-literal>",
        "import_alias": "sql <non-literal>",
        "late_import": "sql <non-literal>",
        "walrus": "sql <non-literal>",
        "def_name": "sql <non-literal>",
        "class_name": "sql <non-literal>",
    }
    for name, header in cases.items():
        got = _whats(header + "\ndef use(conn):\n    conn.execute(Q)\n")
        assert got == {"use": expected[name]}, (name, got)
    # the untouched constant still resolves as a read
    assert _whats('Q = "SELECT 1"\n\ndef use(conn):\n    conn.execute(Q)\n') == {}


def test_scanner_handles_bracket_identifiers_and_except_shadowing() -> None:
    source = """
Q = "SELECT 1"

def bracket_identifier_with_comment_marker(conn):
    conn.executescript("SELECT [a--b]; DELETE FROM t")

def except_as_shadows_a_constant(conn):
    try:
        pass
    except ValueError as Q:
        conn.execute(Q)
"""
    assert _whats(source) == {
        "bracket_identifier_with_comment_marker": "sql DELETE",
        "except_as_shadows_a_constant": "sql <non-literal>",
    }


def test_scanner_models_match_captures_and_class_scope() -> None:
    source = """
Q = "SELECT 1"

def match_capture(conn, d):
    match d:
        case str() as Q:
            conn.execute(Q)

def mapping_rest(conn, d):
    match d:
        case {"k": 1, **Q}:
            conn.execute(Q)

class Holder:
    Q = "DELETE FROM t"
    conn.execute(Q)
"""
    assert _whats(source) == {
        "match_capture": "sql <non-literal>",
        "mapping_rest": "sql <non-literal>",
        "Holder": "sql DELETE",
    }


def test_scanner_flags_the_execute_method_passed_around_and_other_db_writers() -> None:
    source = """
import functools

def alias(conn):
    run = conn.execute
    run("DELETE FROM t")

def partial(conn):
    functools.partial(conn.execute, "DELETE FROM t")()

def mapped(conn, statements):
    list(map(conn.executemany, statements))

def backup(conn, dest):
    conn.backup(dest)

def extension(conn):
    conn.load_extension("x")

def plain_call_is_not_a_reference(conn):
    conn.execute("SELECT 1")
"""
    assert _whats(source) == {
        "alias": "execute reference",
        "partial": "execute reference",
        "mapped": "executemany reference",
        "backup": ".backup()",
        "extension": ".load_extension()",
    }


def test_scanner_ignores_reads_and_string_replace() -> None:
    source = """
def f(p, img, conn):
    p.read_text()
    p.open("r")
    open("f")
    img.open(p)
    "a".replace("b", "c")
    conn.execute("SELECT 1")
    ctypes.windll.kernel32.GetLogicalDrives()
"""
    assert scan_source(source, "synthetic.py") == []
