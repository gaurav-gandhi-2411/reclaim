from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    """Standalone scripts/ file -- load by path (same convention as test_git_guard)."""
    path = _REPO / "scripts" / "check_dist_dll_closure.py"
    spec = importlib.util.spec_from_file_location("check_dist_dll_closure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_dist_dll_closure"] = module
    spec.loader.exec_module(module)
    return module


closure = _load()

# A real PE that imports the VC runtime: the running interpreter's pythonXY.dll (CPython on
# Windows links VCRUNTIME140.dll). Windows-only, like the rest of this project's packaging tests.
_PYDLL = Path(sys.base_prefix) / f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not _PYDLL.is_file(), reason="needs a real Windows PE binary"
)


def test_parses_real_pe_imports() -> None:
    imports = [n.lower() for n in closure.pe_imports(_PYDLL.read_bytes())]
    assert "kernel32.dll" in imports
    assert "vcruntime140.dll" in imports


def test_missing_runtime_at_root_fails_then_passes_once_shipped(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    shutil.copy(_PYDLL, pkg / "ext.pyd")
    assert closure.main([str(tmp_path)]) == 1
    # A copy inside a package dir does not count -- only the dist root is load-order independent.
    shutil.copy(_PYDLL, pkg / "vcruntime140.dll")  # a real PE stand-in; content is irrelevant
    assert closure.main([str(tmp_path)]) == 1
    shutil.copy(_PYDLL, tmp_path / "VCRUNTIME140.dll")  # case-insensitive name match
    assert closure.main([str(tmp_path)]) == 0


def test_unparsable_binary_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "broken.dll").write_bytes(b"MZ not really a PE")
    assert closure.main([str(tmp_path)]) == 1


def test_non_pe_file_with_dll_suffix_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "text.pyd").write_bytes(b"hello")
    assert closure.main([str(tmp_path)]) == 1


def test_missing_dist_dir_fails(tmp_path: Path) -> None:
    assert closure.main([str(tmp_path / "nope")]) == 1


@pytest.mark.parametrize(
    ("name", "is_runtime"),
    [
        ("MSVCP140.dll", True),
        ("msvcp140_1.dll", True),
        ("VCOMP140.DLL", True),
        ("vcruntime140_1.dll", True),
        ("concrt140.dll", True),
        ("KERNEL32.dll", False),
        ("api-ms-win-crt-runtime-l1-1-0.dll", False),
        ("msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll", False),  # delvewheel-mangled copy
    ],
)
def test_runtime_name_classification(name: str, is_runtime: bool) -> None:
    assert bool(closure.VC_RUNTIME_RE.match(name)) is is_runtime
