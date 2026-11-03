"""`subtree_exclusion_match(..., match_junction_targets=True)` is a strict superset of the default.

#247 verifier F1: below a junction the True mode matched content only under the TARGET's real path
text, so a user `[safety] deny` glob or regex naming content through the junction's own path
(`*/jx1/dd1*`) matched in the default mode and not in the stricter one. Now both texts are matched.

Directory names are short and made up (`jx1`, `dd1`) and absent from every test function name,
because pytest's `tmp_path` embeds the test name and a pattern matches the whole path.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from agent_fixtures import make_junction

from reclaim import safety
from reclaim.safety import UNREADABLE_PREFIX, subtree_exclusion_match


def _linked_tree(tmp_path: Path) -> Path:
    """repo\\jx1 -> out, with out\\dd1\\f.txt. Returns the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "out" / "dd1").mkdir(parents=True)
    (tmp_path / "out" / "dd1" / "f.txt").write_text("x\n", encoding="utf-8")
    make_junction(repo / "jx1", tmp_path / "out")
    return repo


def _drop_junctions(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_junction():
            entry.rmdir()  # removes the link only, never its target


@pytest.mark.parametrize("pattern", ["*/jx1/dd1*", "re:jx1.dd1", "*jx1*dd1*"])
def test_true_mode_still_matches_content_named_through_the_junction_path(
    tmp_path: Path, pattern: str
) -> None:
    repo = _linked_tree(tmp_path)
    try:
        default = subtree_exclusion_match(repo, [pattern])
        strict = subtree_exclusion_match(repo, [pattern], match_junction_targets=True)
    finally:
        _drop_junctions(repo)

    assert default == pattern
    assert strict == pattern


def test_true_mode_matches_content_named_through_the_target_path_too(tmp_path: Path) -> None:
    repo = _linked_tree(tmp_path)
    try:
        default = subtree_exclusion_match(repo, ["*/out/dd1*"])
        strict = subtree_exclusion_match(repo, ["*/out/dd1*"], match_junction_targets=True)
    finally:
        _drop_junctions(repo)

    assert default is None  # the default mode never sees the real path
    assert strict == "*/out/dd1*"


def test_a_junction_chain_is_matched_through_the_first_link(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "out" / "dd1").mkdir(parents=True)
    make_junction(repo / "jx2", tmp_path / "out")
    make_junction(repo / "jx1", repo / "jx2")  # jx1 -> jx2 -> out
    try:
        strict = subtree_exclusion_match(repo, ["*/jx1/dd1*"], match_junction_targets=True)
        default = subtree_exclusion_match(repo, ["*/jx1/dd1*"])
        real = subtree_exclusion_match(repo, ["*/out/dd1*"], match_junction_targets=True)
    finally:
        _drop_junctions(repo)

    assert default == "*/jx1/dd1*"
    assert strict == "*/jx1/dd1*"
    assert real == "*/out/dd1*"


def test_true_mode_is_a_superset_of_the_default_on_generated_trees(tmp_path: Path) -> None:
    patterns = [
        "*/a/b*",
        "*/a/*/d1*",
        "*/j1/*",
        "*/j1/d1*",
        "*/j2/a*",
        "*/j2/j1/*",
        "re:j1.d1",
        "re:a.b.d1",
        "re:j2.j1",
        "*j1*d1*",
        "*d1*",
        "*/zz/*",
        "*nomatch*",
    ]
    rng = random.Random(42)  # noqa: S311 -- seeded test data, not security
    names = ["a", "b", "c", "d1"]
    for index in range(4):
        base = tmp_path / f"t{index}"
        repo = base / "repo"
        repo.mkdir(parents=True)
        outs = []
        for out_index in range(2):
            out = base / f"out{out_index}"
            out.mkdir()
            outs.append(out)
            for name in rng.sample(names, 3):
                (out / name).mkdir()
                if rng.random() < 0.5:
                    (out / name / rng.choice(names)).mkdir()
        for name in rng.sample(names, 2):
            (repo / name).mkdir()
            (repo / name / rng.choice(names)).mkdir()
        make_junction(repo / "j1", outs[0])
        make_junction(repo / "j2", outs[1])
        try:
            for pattern in patterns:
                default = subtree_exclusion_match(repo, [pattern])
                strict = subtree_exclusion_match(repo, [pattern], match_junction_targets=True)
                if default is not None:
                    assert strict is not None, (index, pattern)
            all_default = subtree_exclusion_match(repo, patterns)
            all_strict = subtree_exclusion_match(repo, patterns, match_junction_targets=True)
            assert all_default is None or all_strict is not None
        finally:
            _drop_junctions(repo)


def test_the_default_mode_is_unchanged_by_the_alias_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound only exists in the junction-target mode: the delete-path default never uses it."""
    repo = _linked_tree(tmp_path)
    monkeypatch.setattr(safety, "_MAX_PATH_ALIASES", 0)
    try:
        default = subtree_exclusion_match(repo, ["*/jx1/dd1*"])
    finally:
        _drop_junctions(repo)

    assert default == "*/jx1/dd1*"


def test_too_many_path_aliases_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _linked_tree(tmp_path)
    try:
        assert subtree_exclusion_match(repo, ["*nomatch*"], match_junction_targets=True) is None
        monkeypatch.setattr(safety, "_MAX_PATH_ALIASES", 1)  # own text + target text = 2
        result = subtree_exclusion_match(repo, ["*nomatch*"], match_junction_targets=True)
    finally:
        _drop_junctions(repo)

    assert result is not None
    assert result.startswith(UNREADABLE_PREFIX)
    assert "too many aliases" in result
    assert repo.name in result
