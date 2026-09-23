from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load(name: str) -> ModuleType:
    """Standalone scripts/ files aren't a package -- load by path (same as test_git_guard)."""
    spec = importlib.util.spec_from_file_location(name, _REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


checker = _load("check_nofollow_allowlist")
breakdown = _load("nuitka_compile_breakdown")


def _write(root: Path, rel: str, body: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _run(tmp_path: Path, entries: list[str]) -> int:
    allow = tmp_path / "allow.txt"
    allow.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return int(checker.main(["--allowlist", str(allow), "--root", str(tmp_path / "sp")]))


@pytest.fixture
def site(tmp_path: Path) -> Path:
    sp = tmp_path / "sp"
    _write(sp, "pkg/__init__.py", "from pkg import core\n")
    _write(sp, "pkg/core.py", "X = 1\n")
    _write(sp, "pkg/tests/__init__.py", "")
    _write(sp, "pkg/tests/test_core.py", "from pkg.tests import helpers\n")
    _write(sp, "pkg/tests/helpers.py", "")
    _write(sp, "pkg/conftest.py", "import pkg.tests.helpers\n")
    return sp


def test_clean_tree_passes(tmp_path: Path, site: Path) -> None:
    assert _run(tmp_path, ["pkg.tests"]) == 0


@pytest.mark.parametrize(
    "body",
    [
        "import pkg.tests\n",
        "import pkg.tests.helpers as h\n",
        "from pkg.tests import helpers\n",
        "from pkg import tests\n",  # the jinja2.defaults shape
        "from . import tests\n",  # relative form of the same
        "from .tests.helpers import x\n",
        "import importlib\nimportlib.import_module('pkg.tests.helpers')\n",
        "def lazy():\n    import pkg.tests\n",  # the numpy.__getattr__ shape
    ],
)
def test_runtime_importer_of_excluded_name_fails(tmp_path: Path, site: Path, body: str) -> None:
    _write(site, "pkg/runtime.py", body)
    assert _run(tmp_path, ["pkg.tests"]) == 1


def test_unreviewed_fstring_that_could_build_excluded_name_fails(
    tmp_path: Path, site: Path
) -> None:
    _write(
        site,
        "pkg/lazy.py",
        "import importlib\ndef get(n):\n    importlib.import_module(f'pkg.{n}')\n",
    )
    assert _run(tmp_path, ["pkg.tests"]) == 1


def test_reviewed_fstring_site_passes_and_stale_review_fails(tmp_path: Path, site: Path) -> None:
    _write(
        site,
        "pkg/lazy.py",
        "import importlib\ndef get(n):\n    importlib.import_module(f'pkg.{n}')\n",
    )
    assert _run(tmp_path, ["pkg.tests", "@reviewed-dynamic pkg.lazy pkg."]) == 0
    (site / "pkg" / "lazy.py").write_text("X = 1\n", encoding="utf-8")
    assert _run(tmp_path, ["pkg.tests", "@reviewed-dynamic pkg.lazy pkg."]) == 1


def test_fstring_with_unrelated_prefix_ignored(tmp_path: Path, site: Path) -> None:
    _write(site, "pkg/msg.py", "def m(x):\n    return f'other.thing.{x}'\n")
    assert _run(tmp_path, ["pkg.tests"]) == 0


def test_unknown_directive_rejected(tmp_path: Path, site: Path) -> None:
    assert _run(tmp_path, ["pkg.tests", "@skip-everything"]) == 1


def test_importer_in_sibling_package_fails(tmp_path: Path, site: Path) -> None:
    _write(site, "other/__init__.py", "from pkg.tests.helpers import thing\n")
    assert _run(tmp_path, ["pkg.tests"]) == 1


def test_prefix_that_is_not_a_dotted_child_does_not_match(tmp_path: Path, site: Path) -> None:
    _write(site, "pkg/tests_util.py", "")
    _write(site, "pkg/runtime.py", "import pkg.tests_util\n")
    assert _run(tmp_path, ["pkg.tests"]) == 0


@pytest.mark.parametrize("entry", ["pkg.*", "*.tests", "pkg.test?", "pkg.[t]ests"])
def test_glob_entry_rejected(tmp_path: Path, site: Path, entry: str) -> None:
    assert _run(tmp_path, [entry]) == 1


def test_missing_entry_fails_closed(tmp_path: Path, site: Path) -> None:
    assert _run(tmp_path, ["pkg.tests", "pkg.no_such_tests"]) == 1


def test_missing_root_fails_closed(tmp_path: Path) -> None:
    allow = tmp_path / "allow.txt"
    allow.write_text("pkg.tests\n", encoding="utf-8")
    assert checker.main(["--allowlist", str(allow), "--root", str(tmp_path / "nope")]) == 1


def test_unparsable_file_near_excluded_package_fails(tmp_path: Path, site: Path) -> None:
    _write(site, "pkg/broken.py", "def (:\n")
    assert _run(tmp_path, ["pkg.tests"]) == 1


def test_comments_and_blank_lines_ignored(tmp_path: Path) -> None:
    allow = tmp_path / "a.txt"
    allow.write_text("# header\n\npkg.tests  # trailing\n", encoding="utf-8")
    assert checker.load_allowlist(allow) == ["pkg.tests"]


def test_shipped_allowlist_parses_with_no_globs() -> None:
    entries = checker.load_allowlist(_REPO / "packaging" / "nofollow_allowlist.txt")
    assert entries
    # The three runtime-imported "test"-named packages that crashed the last glob-based build
    # must never re-enter the list.
    for runtime_dep in (
        "structlog.testing",
        "jinja2.tests",
        "scipy._external.array_api_extra.testing",
        "numpy.testing",
    ):
        assert checker.matches(runtime_dep, entries) is None


def test_breakdown_package_and_test_classification() -> None:
    assert breakdown.package_of("module.scipy.linalg.tests.test_basic.c") == "scipy"
    assert breakdown.package_of("__helpers.c") == "<nuitka>"
    assert breakdown.is_test_module("module.numpy._core.tests.test_umath.c")
    assert not breakdown.is_test_module("module.scipy.linalg._basic.c")


def test_breakdown_parses_ccache_log(tmp_path: Path) -> None:
    log = tmp_path / "ccache-1.txt"
    log.write_text(
        "[2026-09-23T10:00:00.000000 11  ] === CCACHE 4.12.3 STARTED ===\n"
        "[2026-09-23T10:00:00.000000 11  ] Source file: module.a.c\n"
        "[2026-09-23T10:00:05.000000 11  ] Result: cache_miss\n"
        "[2026-09-23T10:00:05.000000 11  ] === CCACHE DONE ===\n"
        "[2026-09-23T10:00:07.000000 12  ] === CCACHE 4.12.3 STARTED ===\n"
        "[2026-09-23T10:00:07.000000 12  ] Source file: module.b.c\n"
        "[2026-09-23T10:00:07.100000 12  ] Result: direct_cache_hit\n"
        "[2026-09-23T10:00:07.100000 12  ] Result: local_storage_hit\n"
        "[2026-09-23T10:00:07.200000 12  ] === CCACHE DONE ===\n"
        "[2026-09-23T10:00:08.000000 13  ] === CCACHE 4.12.3 STARTED ===\n"
        "[2026-09-23T10:00:08.000000 13  ] Source file: module.c.c\n",
        encoding="utf-8",
    )
    invs, _ = breakdown.parse_ccache_log(log)
    by_src = {i.source: i for i in invs}
    assert by_src["module.a.c"].result == "cache_miss"
    assert by_src["module.a.c"].ccache_s == pytest.approx(5.0)
    assert by_src["module.b.c"].result == "direct_cache_hit"
    assert by_src["module.b.c"].gap_before_s == pytest.approx(2.0)
    assert by_src["module.c.c"].result == "in_flight_at_stop"
