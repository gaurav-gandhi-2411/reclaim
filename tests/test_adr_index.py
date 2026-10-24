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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adr_index = _load("adr_index")


def test_the_committed_adr_index_is_current() -> None:
    assert adr_index.main(["--check"]) == 0, "run: python scripts/adr_index.py"


def test_every_adr_file_is_listed_once() -> None:
    text = adr_index.render(adr_index.ADR_DIR)
    for path in adr_index.ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"):
        assert text.count(f"]({path.name})") == 1, path.name


def test_both_heading_shapes_are_read_and_shared_numbers_are_named(tmp_path: Path) -> None:
    (tmp_path / "0001-a.md").write_text("# 0001. First | with a bar\n", encoding="utf-8")
    (tmp_path / "0002-b.md").write_text("# ADR-0002: Second\n", encoding="utf-8")
    (tmp_path / "0002-c.md").write_text("# 0002. Third\n", encoding="utf-8")
    text = adr_index.render(tmp_path)
    assert "| 0001 | [First \\| with a bar](0001-a.md) |" in text
    assert "| 0002 | [Second](0002-b.md) |" in text
    assert "used by more than one ADR: 0002." in text


@pytest.mark.parametrize(
    "first_line", ["Title without a heading", "# 0009. Number does not match the file", ""]
)
def test_a_malformed_first_line_is_refused(tmp_path: Path, first_line: str) -> None:
    (tmp_path / "0003-x.md").write_text(first_line + ("\n" if first_line else ""), "utf-8")
    with pytest.raises(ValueError, match=r"0003-x\.md"):
        adr_index.render(tmp_path)
