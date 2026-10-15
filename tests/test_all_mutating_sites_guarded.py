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
    }
)
_SQL_MUTATING = ("DELETE", "DROP", "VACUUM", "UPDATE", "INSERT", "REPLACE", "ALTER", "CREATE")
_WRITE_MODE_CHARS = set("wax+")


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
    or explicit `+` concatenation, or a name bound to one at module level. Only the LEADING text
    decides the verb, so an f-string/concatenation is returned as far as it is known; None when
    even its start cannot be determined (a parameter, an attribute, a call result)."""
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
                    return out or None
                out += known
        return out or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left, consts)
        if left is None:
            return None
        right = _static_text(node.right, consts)
        return left + (right or "")
    return None


def _sql_text(node: ast.Call, consts: dict[str, str] | None = None) -> str | None:
    """The leading constant text of a SQL-ish first argument, None if it is not static."""
    if not node.args:
        return None
    return _static_text(node.args[0], consts or {})


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = <static string>` bindings (resolved in source order)."""
    consts: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name):
            text = _static_text(value, consts)
            if text is not None:
                consts[target.id] = text
    return consts


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
        if name in ("execute", "executemany", "executescript"):
            sql = _sql_text(node, consts)
            if sql is None:
                # Cannot be proven read-only (the SQL arrives as a parameter/attribute/call
                # result, e.g. ScanIndex._flush_hash_rows(sql, rows)): fail closed, like a
                # non-literal open() mode. A reader of such a call must allowlist it with a reason.
                return "sql <non-literal>"
            if sql.lstrip().upper().startswith(_SQL_MUTATING):
                return f"sql {sql.lstrip().split(None, 1)[0].upper()}"
    return None


def _is_guard_call(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in GUARD_NAMES


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str, consts: dict[str, str]) -> None:
        self.file = file
        self.consts = consts
        self.stack: list[str] = []
        # per enclosing function: lines of guard calls found so far in its body
        self.guard_lines: list[list[int]] = []
        # per enclosing function: (line, name, value-node) of its simple `name = <expr>` bindings
        self.local_binds: list[list[tuple[int, str, ast.AST]]] = []
        self.raw: list[tuple[str, int, str, list[int]]] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        is_func = not isinstance(node, ast.ClassDef)
        if is_func:
            lines = [
                n.lineno for n in ast.walk(node) if isinstance(n, ast.Call) and _is_guard_call(n)
            ]
            self.guard_lines.append(lines)
            self.local_binds.append(
                sorted(
                    (
                        (n.lineno, n.targets[0].id, n.value)
                        for n in ast.walk(node)
                        if isinstance(n, ast.Assign)
                        and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                    ),
                    key=lambda b: b[0],
                )
            )
        self.generic_visit(node)
        if is_func:
            self.guard_lines.pop()
            self.local_binds.pop()
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        consts = dict(self.consts)
        # A local binding shadows a module constant. Fail closed when a name is bound more than
        # once before the call (if/else branches): it resolves only if every earlier binding is
        # static AND they all start with the same SQL verb, otherwise it is treated as unknown.
        seen: dict[str, list[str | None]] = {}
        for binds in self.local_binds:
            for line, name, value in binds:
                if line >= node.lineno:
                    break
                seen.setdefault(name, []).append(_static_text(value, consts))
        for name, texts in seen.items():
            verbs = {t.split(None, 1)[0].upper() if t and t.split() else None for t in texts}
            if None in verbs:
                consts.pop(name, None)
            elif len(verbs) == 1:
                consts[name] = texts[-1] or ""
            else:
                consts.pop(name, None)
        what = classify_call(node, consts)
        if what is not None:
            function = ".".join(self.stack) or "<module>"
            all_guards = [ln for lines in self.guard_lines for ln in lines]
            self.raw.append((function, node.lineno, what, all_guards))
        self.generic_visit(node)


def scan_source(source: str, file: str) -> list[Site]:
    """Every mutating call in `source`; guarded = a guard call precedes it inside an enclosing
    function (lexically, by line)."""
    tree = ast.parse(source)
    visitor = _Visitor(file, _module_string_constants(tree))
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
    "scanner.py:_query_git_clean": "read-only subprocess: `git status --porcelain`",
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
            "_backfill_name_and_path_lower",
            "upsert_rows",
            "begin_scan_tracking",
            "record_seen",
            "protect_under",
            "end_scan_tracking",
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
