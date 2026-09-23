from __future__ import annotations

# check_nofollow_allowlist.py -- build gate for packaging/nofollow_allowlist.txt.
#
# Why this exists: the Nuitka build excludes numpy/scipy test suites to save hours of C
# compile, but the last attempt to do that with a `*.tests`/`*.testing` glob also excluded
# three packages that runtime code imports unconditionally, and the packaged app crashed on
# every invocation. This script is the gate that makes the named allow-list safe to use: it
# fails (exit 1) if anything that is not test code imports an excluded name, and fails closed
# on anything it cannot verify (a glob entry, an entry that doesn't exist, an unparsable file).
#
# What counts as an import of excluded name E, from any .py file under the scanned roots:
#   - `import E` / `import E.sub` / `from E import x` / `from E.sub import x`
#   - `from P import T` where P.T == E or starts with E. (covers `from . import tests`)
#   - relative imports, resolved against the importing module's own package
#   - a string literal equal to E or starting with `E.` (importlib.import_module / __import__)
#   - an f-string whose static leading text is a prefix of E (e.g. f"scipy.{name}" could build
#     "scipy.linalg.tests"). These can't be resolved statically, so each one must be reviewed by
#     hand and declared in the allow-list as `@reviewed-dynamic <module> <prefix>`. An
#     undeclared site fails, and so does a declared one that no longer exists, so a dependency
#     upgrade that adds or moves one forces a fresh review.
# What counts as test code (allowed to import excluded names): a file inside an excluded
# package itself, `conftest.py`, and `test_*.py`.
#
# Scope limit, stated so the green result isn't over-read (house rule 85a): this sees Python
# source only. An import made from inside a compiled extension (.pyd) is invisible here, the
# same gap that hid winrt.windows.foundation. That surface is covered by the frozen smoke test
# (packaging/test_packaged_serve.ps1), not by this script. Names built by concatenation or
# `.format()` are also not followed; the frozen smoke test is the backstop for those too.
#
# Usage: python scripts/check_nofollow_allowlist.py --allowlist packaging/nofollow_allowlist.txt
#            --root <build-venv>/Lib/site-packages --root src
import argparse
import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_GLOB_CHARS = frozenset("*?[]")
_REVIEWED_DYNAMIC = "@reviewed-dynamic"


@dataclass(frozen=True)
class Violation:
    file: Path
    line: int
    target: str
    excluded: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: imports {self.target!r} (excluded: {self.excluded})"


@dataclass(frozen=True)
class DynamicSite:
    file: Path
    line: int
    module: str
    prefix: str

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}: f-string {self.prefix + '{...}'!r} could build an excluded "
            f"name -- review it, then declare `{_REVIEWED_DYNAMIC} {self.module} {self.prefix}`"
        )


def _content_lines(path: Path) -> Iterator[str]:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            yield line


def load_allowlist(path: Path) -> list[str]:
    entries: list[str] = []
    for line in _content_lines(path):
        if line.startswith("@"):
            continue
        if _GLOB_CHARS & set(line):
            raise ValueError(f"glob pattern not allowed in allow-list: {line!r}")
        if not all(part.isidentifier() for part in line.split(".")):
            raise ValueError(f"not a dotted module name: {line!r}")
        entries.append(line)
    if len(entries) != len(set(entries)):
        raise ValueError("duplicate entries in allow-list")
    return entries


def load_reviewed_dynamic(path: Path) -> set[tuple[str, str]]:
    reviewed: set[tuple[str, str]] = set()
    for line in _content_lines(path):
        if not line.startswith("@"):
            continue
        parts = line.split()
        if parts[0] != _REVIEWED_DYNAMIC or len(parts) != 3:
            raise ValueError(f"unrecognised directive: {line!r}")
        reviewed.add((parts[1], parts[2]))
    return reviewed


def module_name(py: Path, root: Path) -> str:
    parts = list(py.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def matches(target: str, excluded: Iterable[str]) -> str | None:
    for e in excluded:
        if target == e or target.startswith(e + "."):
            return e
    return None


def entry_exists(entry: str, roots: Iterable[Path]) -> bool:
    rel = Path(*entry.split("."))
    return any((r / rel).is_dir() or (r / rel).with_suffix(".py").is_file() for r in roots)


def is_test_code(py: Path, mod: str, excluded: Iterable[str]) -> bool:
    return (
        py.name == "conftest.py"
        or py.name.startswith("test_")
        or matches(mod, excluded) is not None
    )


def dynamic_prefix(node: ast.AST, excluded: Iterable[str]) -> str | None:
    """Static leading text of an f-string, if it could be the start of an excluded name."""
    if not isinstance(node, ast.JoinedStr) or not node.values:
        return None
    head = node.values[0]
    if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
        return None
    prefix = head.value
    if "." not in prefix or " " in prefix:
        return None
    return prefix if any(e.startswith(prefix) for e in excluded) else None


def _import_targets(node: ast.AST, mod: str, is_pkg: bool) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            pkg_parts = mod.split(".") if is_pkg else mod.split(".")[:-1]
            keep = len(pkg_parts) - (node.level - 1)
            if keep < 0:
                return
            base = ".".join(pkg_parts[:keep])
            prefix = f"{base}.{node.module}" if node.module else base
        else:
            prefix = node.module or ""
        if prefix:
            yield prefix
        for alias in node.names:
            if alias.name != "*":
                yield f"{prefix}.{alias.name}" if prefix else alias.name
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        s = node.value
        if 0 < len(s) < 200 and "." in s and " " not in s:
            yield s


def scan(
    roots: list[Path], excluded: list[str]
) -> tuple[list[Violation], list[DynamicSite], list[Path]]:
    violations: list[Violation] = []
    dynamic: list[DynamicSite] = []
    unparsable: list[Path] = []
    for root in roots:
        for py in root.rglob("*.py"):
            mod = module_name(py, root)
            if is_test_code(py, mod, excluded):
                continue
            try:
                tree = ast.parse(py.read_bytes(), filename=str(py))
            except (SyntaxError, ValueError):
                unparsable.append(py)
                continue
            is_pkg = py.name == "__init__.py"
            for node in ast.walk(tree):
                prefix = dynamic_prefix(node, excluded)
                if prefix is not None:
                    dynamic.append(DynamicSite(py, getattr(node, "lineno", 0), mod, prefix))
                for target in _import_targets(node, mod, is_pkg):
                    hit = matches(target, excluded)
                    if hit:
                        line = getattr(node, "lineno", 0)
                        violations.append(Violation(py, line, target, hit))
    return violations, dynamic, unparsable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--allowlist", type=Path, required=True)
    ap.add_argument("--root", type=Path, action="append", required=True)
    args = ap.parse_args(argv)
    try:
        excluded = load_allowlist(args.allowlist)
        reviewed = load_reviewed_dynamic(args.allowlist)
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot load allow-list: {exc}", file=sys.stderr)
        return 1
    roots: list[Path] = [r.resolve() for r in args.root]
    missing_roots = [r for r in roots if not r.is_dir()]
    if missing_roots:
        print(f"FAIL: scan root(s) missing: {missing_roots}", file=sys.stderr)
        return 1
    missing = [e for e in excluded if not entry_exists(e, roots)]
    if missing:
        print(f"FAIL: allow-list entries not found under any root: {missing}", file=sys.stderr)
        return 1
    violations, dynamic, unparsable = scan(roots, excluded)
    unreviewed = [d for d in dynamic if (d.module, d.prefix) not in reviewed]
    stale_reviews = sorted(reviewed - {(d.module, d.prefix) for d in dynamic})
    # A file we could not parse is a file whose imports we could not check -- but site-packages
    # legitimately ships a few py2-only/template files. Report them; only fail if one lives
    # under a package that also contains an excluded entry (where a miss could actually matter).
    risky = [p for p in unparsable if any(e.split(".")[0] in p.parts for e in excluded)]
    for v in violations:
        print(f"VIOLATION: {v}", file=sys.stderr)
    for d in unreviewed:
        print(f"UNREVIEWED: {d}", file=sys.stderr)
    for module, prefix in stale_reviews:
        print(f"STALE: {_REVIEWED_DYNAMIC} {module} {prefix} matches no site", file=sys.stderr)
    for p in unparsable:
        print(f"{'FAIL' if p in risky else 'note'}: could not parse {p}", file=sys.stderr)
    if violations or unreviewed or stale_reviews or risky:
        print(
            f"FAIL: {len(violations)} violation(s), {len(unreviewed)} unreviewed dynamic "
            f"site(s), {len(stale_reviews)} stale review(s), {len(risky)} unparsable risky "
            "file(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: {len(excluded)} excluded names, no non-test importer across {len(roots)} root(s); "
        f"{len(dynamic)} reviewed dynamic site(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
