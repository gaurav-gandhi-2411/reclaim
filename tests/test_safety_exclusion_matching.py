"""Pins the compiled exclusion matcher (`safety._first_hit` & co.) and the string-concatenating
subtree walk to the PREVIOUS implementation, which is copied here verbatim as the oracle
(`_ref_*`). Same input -> same returned pattern (not just the same yes/no), same traversal order.

Why this exists: the candidates warm-up spent ~358 of 574 s in `subtree_exclusion_match`
(7.3M `_pattern_matches` calls). The rewrite changes cost, never a decision.

Deliberate divergence from the oracle since 2026-10 (fail-closed walk): a directory that cannot be
listed, or an entry whose type cannot be read, no longer counts as "nothing excluded here"; the
walk returns an `<unreadable directory: ...>` marker instead (tests at the end of this file)."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import random
import re
from pathlib import Path

import pytest

from reclaim import safety
from reclaim.safety import (
    UNREADABLE_PREFIX,
    _strip_local_extended_length_prefix,
    first_matching_pattern,
    subtree_exclusion_match,
)

# --- the previous implementation, verbatim --------------------------------------------------


def _ref_pattern_matches(path: Path, pattern: str) -> bool:
    candidate = _strip_local_extended_length_prefix(path).as_posix()
    if pattern.startswith("re:"):
        return re.search(pattern[3:], candidate, flags=re.IGNORECASE) is not None
    return fnmatch.fnmatch(candidate.lower(), pattern.lower())


def _ref_first_matching_pattern(path, patterns, *, is_dir=False):
    base = Path(path)
    for pattern in patterns:
        if _ref_pattern_matches(base, pattern):
            return pattern
        if is_dir and _ref_pattern_matches(Path(str(base) + "/"), pattern):
            return pattern
    return None


def _ref_subtree_exclusion_match(root, patterns):
    if not patterns:
        return None
    hit = _ref_first_matching_pattern(root, patterns, is_dir=True)
    if hit is not None:
        return hit
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    hit = _ref_first_matching_pattern(entry.path, patterns, is_dir=is_dir)
                    if hit is not None:
                        return hit
                    if is_dir:
                        stack.append(entry.path)
        except OSError:
            continue
    return None


# --- corpora ----------------------------------------------------------------------------------

PATTERNS = [
    "*fr-en-transformer*",
    "*intent-router*",
    "*shipdoc-extract*",
    "*Intent-Router*",  # mixed case: both sides are folded
    "*/secret/*",
    "*/secret",
    "c:/users/gaura/private/*",
    "C:/Users/gaura/Private*",
    "*.env",
    "*[0-9]x*",  # character class: not the literal fast path
    "*a?c*",  # `?` wildcard: not the literal fast path
    "*",
    "**",
    "",
    "plain",
    "re:intent-router",
    "re:^c:/users/[^/]+/private(/|$)",
    "re:\\.git$",
    "re:SECRET",
    "*caf\u00e9*",
    "*\u0130stanbul*",  # dotted capital I: lower() and normcase() may disagree
    "*x*y*",
    "*[!a]*",
]

PATHS = [
    "C:\\Users\\gaura\\ml-projects\\intent-router",
    "C:\\Users\\gaura\\ml-projects\\Intent-Router\\src",
    "c:\\users\\gaura\\private\\a.txt",
    "C:\\Users\\gaura\\Private",
    "C:\\Users\\gaura\\x\\secret",
    "C:\\Users\\gaura\\x\\secret\\y.txt",
    "\\\\?\\C:\\Users\\gaura\\ml-projects\\fr-en-transformer\\a",
    "\\\\?\\UNC\\server\\share\\shipdoc-extract",
    "\\\\server\\share\\x",
    "C:\\",
    "C:\\Users\\gaura\\file.env",
    "C:\\Users\\gaura\\data9x\\q",
    "C:\\Users\\gaura\\abc",
    "C:\\Users\\gaura\\caf\u00e9\\menu",
    "C:\\Users\\gaura\\\u0130stanbul\\trip",
    "C:\\Users\\gaura\\repo\\.git",
    "C:\\Users\\gaura\\trailing.dot.",
    "C:\\Users\\gaura\\trailing space ",
    "C:/Users/gaura/forward/slash",
    "relative\\path\\intent-router",
    "x",
    ".",
]


def _combos() -> list[list[str]]:
    rng = random.Random(42)  # noqa: S311 -- deterministic fixture data
    out = [[p] for p in PATTERNS]
    for _ in range(120):
        out.append(rng.sample(PATTERNS, rng.randint(2, 5)))
    return out


@pytest.mark.parametrize("is_dir", [False, True])
def test_first_matching_pattern_returns_the_same_pattern_as_before(is_dir: bool) -> None:
    checked = 0
    for patterns in _combos():
        for path in PATHS:
            try:
                expected = _ref_first_matching_pattern(path, patterns, is_dir=is_dir)
            except re.error:
                with pytest.raises(re.error):
                    first_matching_pattern(path, patterns, is_dir=is_dir)
                continue
            assert first_matching_pattern(path, patterns, is_dir=is_dir) == expected, (
                path,
                patterns,
            )
            checked += 1
    assert checked > 2000


def test_pattern_matches_helper_equals_reference() -> None:
    for pattern in PATTERNS:
        for path in PATHS:
            assert safety._pattern_matches(Path(path), pattern) == _ref_pattern_matches(
                Path(path), pattern
            ), (path, pattern)


def test_any_pattern_matches_equals_reference() -> None:
    for patterns in _combos():
        for path in PATHS:
            expected = any(_ref_pattern_matches(Path(path), p) for p in patterns)
            assert safety._any_pattern_matches(Path(path), patterns) == expected, (path, patterns)


def test_trailing_slash_form_never_differs_from_the_plain_text() -> None:
    # The shortcut `if other == text: other = None` and the walk's "dir text == file text" rest on
    # pathlib dropping a trailing slash. Pin it for every path form used here.
    for path in PATHS:
        base = Path(path)
        plain = _strip_local_extended_length_prefix(base).as_posix()
        slash = _strip_local_extended_length_prefix(Path(str(base) + "/")).as_posix()
        assert plain == slash, path


# --- walks ------------------------------------------------------------------------------------

NAMES = [
    "src", "lib", "node_modules", "Intent-Router", "intent-router", "fr-en-transformer",
    "secret", "data9x", "abc", "plain", "caf\u00e9", "dot.", "has space", "a.env", "x_y",
    ".git", "SECRET", "shipdoc-extract-copy",
]  # fmt: skip


def _make_tree(root: Path, rng: random.Random, depth: int = 3) -> None:
    root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for name in rng.sample(NAMES, rng.randint(2, 7)):
        if name.lower() in seen:  # NTFS is case-insensitive: "SECRET" would collide with "secret"
            continue
        seen.add(name.lower())
        target = root / name
        if depth > 0 and rng.random() < 0.55:
            _make_tree(target, rng, depth - 1)
        else:
            with contextlib.suppress(OSError):
                target.write_text("x", encoding="utf-8")


def test_walk_returns_the_same_pattern_as_before_on_random_trees(tmp_path: Path) -> None:
    rng = random.Random(42)  # noqa: S311 -- deterministic fixture data
    compared = 0
    for case in range(40):
        root = tmp_path / f"case{case}"
        _make_tree(root, rng)
        for _ in range(8):
            patterns = rng.sample(PATTERNS, rng.randint(1, 4))
            try:
                expected = _ref_subtree_exclusion_match(root, patterns)
            except re.error:
                with pytest.raises(re.error):
                    subtree_exclusion_match(root, patterns)
                continue
            assert subtree_exclusion_match(root, patterns) == expected, (root, patterns)
            assert subtree_exclusion_match(str(root), patterns) == expected
            compared += 1
    assert compared > 200


def test_walk_with_several_matches_reports_the_same_first_one(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "a" / "intent-router").mkdir(parents=True)
    (root / "b" / "fr-en-transformer").mkdir(parents=True)
    (root / "c").mkdir()
    (root / "c" / "x.env").write_text("x", encoding="utf-8")
    patterns = ["*.env", "*fr-en-transformer*", "*intent-router*"]
    assert subtree_exclusion_match(root, patterns) == _ref_subtree_exclusion_match(root, patterns)
    assert subtree_exclusion_match(root, patterns[::-1]) == _ref_subtree_exclusion_match(
        root, patterns[::-1]
    )


def test_walk_finds_an_excluded_name_deep_inside_a_dependency_tree(tmp_path: Path) -> None:
    # Why the walk is not pruned: the excluded name is far below a directory whose own path
    # says nothing about it.
    root = tmp_path / "app" / "node_modules"
    (root / "pkg" / "dist" / "intent-router").mkdir(parents=True)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"
    assert subtree_exclusion_match(root, ["*fr-en-transformer*"]) is None


def test_walk_finds_a_match_in_every_branch_depth(tmp_path: Path) -> None:
    for depth in range(1, 6):
        root = tmp_path / f"d{depth}"
        leaf = root.joinpath(*[f"n{i}" for i in range(depth)], "shipdoc-extract")
        leaf.mkdir(parents=True)
        assert subtree_exclusion_match(root, ["*shipdoc-extract*"]) == "*shipdoc-extract*"


def test_no_patterns_means_no_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("scandir must not be called without patterns")

    monkeypatch.setattr(os, "scandir", boom)
    assert subtree_exclusion_match(tmp_path, []) is None


def test_an_unreadable_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "t"
    (root / "ok" / "intent-router").mkdir(parents=True)
    (root / "locked" / "fr-en-transformer").mkdir(parents=True)
    real = os.scandir

    def flaky(path):  # type: ignore[no-untyped-def]
        if str(path).endswith("locked"):
            raise PermissionError(5, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    got = subtree_exclusion_match(root, ["*fr-en-transformer*"])
    # the old walk skipped the locked subtree and answered "nothing excluded" (fail open)
    assert _ref_subtree_exclusion_match(root, ["*fr-en-transformer*"]) is None
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)
    assert "locked" in got
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


def test_invalid_regex_raises_only_when_reached_like_before(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "intent-router").mkdir(parents=True)
    # a hit on the first pattern returns before the malformed second one is ever compiled
    hit_first = [f"*{tmp_path.name.lower()}*", "re:["]
    assert subtree_exclusion_match(root, hit_first) == hit_first[0]
    assert _ref_subtree_exclusion_match(root, hit_first) == hit_first[0]
    with pytest.raises(re.error):
        subtree_exclusion_match(root, ["*nomatch*", "re:["])
    with pytest.raises(re.error):
        _ref_subtree_exclusion_match(root, ["*nomatch*", "re:["])


def test_patterns_are_compiled_once_not_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "big"
    for i in range(30):
        (root / f"d{i}").mkdir(parents=True)
        for j in range(20):
            (root / f"d{i}" / f"f{j}.txt").write_text("x", encoding="utf-8")
    patterns = ["*fr-en-transformer*", "*zz?zz*", "re:neverthere"]
    safety._compile_pattern.cache_clear()
    assert subtree_exclusion_match(root, patterns) is None  # 630 entries x 3 patterns
    after_first = safety._compile_pattern.cache_info()
    assert after_first.misses == 3  # one compile per pattern, not per entry
    assert after_first.hits >= 600

    calls = {"n": 0}
    real_compile = re.compile

    def counting(*a: object, **k: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real_compile(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(re, "compile", counting)
    assert subtree_exclusion_match(root, patterns) is None
    assert calls["n"] == 0  # second walk: everything is cached


def test_literal_fast_path_is_only_used_for_wildcard_free_text() -> None:
    assert safety._compile_pattern("*abc*").literal == "abc"
    assert safety._compile_pattern("**").literal == ""
    for pattern in ("*a?c*", "*a[bc]*", "*a*b*", "abc*", "*abc", "*", "re:abc"):
        assert safety._compile_pattern(pattern).literal is None, pattern


BS = chr(92)  # a single backslash, spelled out so no tool or editor can swallow it
EXT = BS + BS + "?" + BS  # the extended-length prefix


def test_extended_length_prefix_is_stripped_for_anchored_patterns(tmp_path: Path) -> None:
    # An anchored pattern only matches when the extended-length prefix is removed from the front
    # of the path; an unanchored `*name*` would match either way, which is why the old corpus
    # did not notice.
    anchored = ["c:/users/gaura/private/*", "re:^c:/users/gaura/private/"]
    for pattern in anchored:
        for path in (
            EXT + "C:" + BS + "Users" + BS + "gaura" + BS + "Private" + BS + "a.txt",
            "C:" + BS + "Users" + BS + "gaura" + BS + "Private" + BS + "a.txt",
        ):
            assert first_matching_pattern(path, [pattern]) == pattern, (path, pattern)
            assert _ref_first_matching_pattern(path, [pattern]) == pattern
    # and the same through the walk: an extended-length root with an anchored pattern
    root = tmp_path / "ext" / "child"
    (root / "inner").mkdir(parents=True)
    plain = str(root).replace("\\", "/").lower()
    pattern = f"re:^{re.escape(plain)}/inner$"
    extended = EXT + str(root)
    assert subtree_exclusion_match(extended, [pattern]) == _ref_subtree_exclusion_match(
        extended, [pattern]
    )
    assert subtree_exclusion_match(extended, [pattern]) == pattern


def test_a_locked_directory_does_not_hide_a_match_in_its_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NTFS lists a_ok, locked, z_ok; the walk pops z_ok, locked, a_ok. Aborting at `locked` would
    # lose the only match, which sits in a_ok.
    root = tmp_path / "lk"
    (root / "a_ok" / "intent-router").mkdir(parents=True)
    (root / "locked" / "inner").mkdir(parents=True)
    (root / "z_ok" / "clean").mkdir(parents=True)
    real = os.scandir

    def flaky(path):  # type: ignore[no-untyped-def]
        if str(path).endswith("locked"):
            raise PermissionError(5, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"
    assert _ref_subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


# --- relative and drive-relative roots (found by an independent verifier) -----------------------
# pathlib normalises: the entries of "." read "proj" (not "./proj") and those of a drive-relative
# "C:" read "C:proj" (not "C:/proj"). Anchored patterns are what notice.

ANCHORED = [
    "proj*",
    "re:^proj",
    "[a-z]*",
    "re:^PROJ",
    "*/proj",
    "re:^./proj",
    "re:^[a-z]:proj",
    "c:proj*",
    "d:proj*",
    "re:^C:/proj",
    "c:/proj*",
    "*proj*",
    "sub/*",
    "re:^sub/deep$",
    "re:^\\.\\./",
]


def _relative_root_cases(cwd_drive: str) -> list[str]:
    return [".", "./", ".\\", "./.", cwd_drive, ".\\sub", "sub", "./sub", "sub/"]


@pytest.mark.parametrize("pattern", ANCHORED)
def test_relative_roots_relative_roots_match_the_previous_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    tree = tmp_path / "wd"
    (tree / "proj" / "inner").mkdir(parents=True)
    (tree / "sub" / "deep").mkdir(parents=True)
    (tree / "Other.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tree)
    drive = os.path.splitdrive(str(Path.cwd()))[0]
    compared = 0
    for root in _relative_root_cases(drive):
        for patterns in ([pattern], [pattern, "*zzz*"], ["*zzz*", pattern]):
            expected = _ref_subtree_exclusion_match(root, patterns)
            assert subtree_exclusion_match(root, patterns) == expected, (root, patterns)
            compared += 1
    assert compared == 27


def test_relative_roots_are_pinned_on_a_known_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact repro: root "." and an anchored pattern. Concatenating "./" + name loses it.
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path)
    assert subtree_exclusion_match(".", ["proj*"]) == "proj*"
    assert subtree_exclusion_match(".", ["re:^proj"]) == "re:^proj"
    drive = os.path.splitdrive(str(Path.cwd()))[0]
    assert subtree_exclusion_match(drive, ["re:^" + re.escape(drive) + "proj"]) is not None
    assert subtree_exclusion_match(drive, ["re:^" + re.escape(drive) + "/proj"]) is None


# --- weak spots found by an independent verifier (pass 2); each test below kills a mutant -------


def test_pass2_trailing_slash_form_matters_for_a_drive_relative_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a drive-relative root ("D:") has a trailing-slash text ("D:/") that differs from its
    plain text ("D:"), so only here is the `other` mechanism exercised at all."""
    monkeypatch.chdir(tmp_path)
    drive = os.path.splitdrive(str(Path.cwd()))[0]
    low = drive.lower()
    for pattern in (
        f"{low}/",
        f"{low}/*",
        f"re:^{low}/$",
        "*:/",
        "*:/*",
        "*/*",
        f"re:^{re.escape(low)}/",
    ):
        for is_dir in (True, False):
            assert first_matching_pattern(
                drive, [pattern], is_dir=is_dir
            ) == _ref_first_matching_pattern(drive, [pattern], is_dir=is_dir), (pattern, is_dir)
    # and the concrete outcome, so the equality above cannot hold by both being None
    assert first_matching_pattern(drive, [f"{low}/"], is_dir=True) == f"{low}/"
    assert first_matching_pattern(drive, [f"{low}/*"], is_dir=True) == f"{low}/*"
    assert first_matching_pattern(drive, [f"re:^{low}/$"], is_dir=True) == f"re:^{low}/$"
    assert first_matching_pattern(drive, [f"{low}/"], is_dir=False) is None
    # a `*lit*` pattern (the literal fast path) that only the trailing-slash text satisfies
    assert first_matching_pattern(drive, ["*:/*"], is_dir=True) == "*:/*"
    assert first_matching_pattern(drive, ["*:/*"], is_dir=False) is None


def test_pass2_non_ascii_regex_is_case_insensitive_like_before() -> None:
    caf = "caf" + chr(0xE9)
    path = "C:/x/" + caf
    pattern = "re:CAF" + chr(0xC9)
    assert first_matching_pattern(path, [pattern]) == pattern
    assert _ref_first_matching_pattern(path, [pattern]) == pattern


class _FakeEntry:
    """A DirEntry stand-in: `is_dir` can raise, and a directory symlink answers True only when
    asked to follow links."""

    def __init__(self, parent: str, name: str, *, is_dir_raises: bool = False, link: bool = False):
        self.name = name
        self.path = parent + chr(92) + name
        self._raises = is_dir_raises
        self._link = link
        self.calls: list[bool] = []

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        self.calls.append(follow_symlinks)
        if self._raises:
            raise OSError("is_dir failed")
        return self._link and follow_symlinks


def _plain(path: object) -> str:
    """The walk lists through the extended-length form of each path; the fakes key on the plain
    one."""
    return str(path).removeprefix(chr(92) * 2 + "?" + chr(92))


class _FakeScan:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries

    def __enter__(self) -> _FakeScan:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._entries)


def test_pass2_entry_whose_is_dir_raises_is_still_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    root = "C:" + chr(92) + "fake-root"

    def fake_scandir(path: object) -> _FakeScan:
        assert _plain(path) == root
        return _FakeScan([_FakeEntry(root, "intent-router", is_dir_raises=True)])

    monkeypatch.setattr(os, "scandir", fake_scandir)
    # the root itself does not match; the only match is the entry whose is_dir() failed
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"
    assert _ref_subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


def test_pass2_links_are_never_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    root = "C:" + chr(92) + "fake-root"
    link = _FakeEntry(root, "linked-dir", link=True)
    scanned: list[str] = []

    def fake_scandir(path: object) -> _FakeScan:
        scanned.append(_plain(path))
        if _plain(path) == root:
            return _FakeScan([link])
        raise AssertionError("a directory link must not be descended into")

    monkeypatch.setattr(os, "scandir", fake_scandir)
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None
    assert scanned == [root]
    assert link.calls and all(
        call is False for call in link.calls
    )  # asked with follow_symlinks=False


def test_pass2_the_root_alone_can_match_without_any_children(tmp_path: Path) -> None:
    protected = tmp_path / "intent-router"
    protected.mkdir()
    assert subtree_exclusion_match(protected, ["*intent-router*"]) == "*intent-router*"
    assert subtree_exclusion_match(protected, ["*zzz*"]) is None


# --- fail-closed walk (2026-10, from the #187 data-safety verifier) -----------------------------


def test_an_entry_whose_type_cannot_be_read_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    root = "C:" + chr(92) + "fake-root"

    def fake_scandir(path: object) -> _FakeScan:
        assert _plain(path) == root
        return _FakeScan([_FakeEntry(root, "opaque", is_dir_raises=True)])

    monkeypatch.setattr(os, "scandir", fake_scandir)
    got = subtree_exclusion_match(root, ["*intent-router*"])
    assert _ref_subtree_exclusion_match(root, ["*intent-router*"]) is None  # old: fail open
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)


def test_an_error_while_listing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "mid"
    (root / "a").mkdir(parents=True)
    real = os.scandir

    class Broken:
        def __init__(self, it: object) -> None:
            self._it = it

        def __enter__(self) -> Broken:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise OSError(59, "unexpected network error")

    def flaky(path):  # type: ignore[no-untyped-def]
        return Broken(real(path)) if str(path).endswith("a") else real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    got = subtree_exclusion_match(root, ["*intent-router*"])
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)


def test_a_subdirectory_that_vanishes_mid_walk_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the tree is changing under the walk (a live tree): what it held is unknown
    root = tmp_path / "live"
    (root / "gone").mkdir(parents=True)
    real = os.scandir

    def vanishing(path):  # type: ignore[no-untyped-def]
        if str(path).endswith("gone"):
            raise FileNotFoundError(2, "vanished")
        return real(path)

    monkeypatch.setattr(os, "scandir", vanishing)
    got = subtree_exclusion_match(root, ["*intent-router*"])
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)


def test_a_missing_root_still_answers_nothing_excluded(tmp_path: Path) -> None:
    assert subtree_exclusion_match(tmp_path / "gone", ["*intent-router*"]) is None


def test_an_unreadable_root_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def denied(path):  # type: ignore[no-untyped-def]
        raise PermissionError(5, "denied")

    monkeypatch.setattr(os, "scandir", denied)
    got = subtree_exclusion_match(root, ["*intent-router*"])
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH is a Windows limit")
def test_a_match_deeper_than_max_path_is_found(tmp_path: Path) -> None:
    root = tmp_path / "deep"
    long_dir = "\\\\?\\" + str(root)
    for i in range(6):
        long_dir += "\\" + f"{i}" + "d" * 50
    os.makedirs(long_dir + "\\intent-router")  # noqa: PTH103 -- needs the extended-length string
    assert len(long_dir) > 300
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_loop_ends_and_a_junction_into_an_excluded_tree_still_protects(
    tmp_path: Path,
) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    root = tmp_path / "loop"
    (root / "a").mkdir(parents=True)
    _winapi.CreateJunction(str(root), str(root / "a" / "back"))
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None

    side = tmp_path / "side"
    (side / "intent-router").mkdir(parents=True)
    _winapi.CreateJunction(str(side), str(root / "a" / "into-side"))
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"
