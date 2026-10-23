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


# --- #190 verifier pass 1 -------------------------------------------------------------------


def test_a_file_as_root_answers_by_its_own_path(tmp_path: Path) -> None:
    """Callers pass top-level temp/crash-dump FILES too; a file root is not an unreadable
    directory (it made every top-level temp file and every crash dump read excluded)."""
    f = tmp_path / "old.tmp"
    f.write_text("x", encoding="utf-8")
    assert subtree_exclusion_match(f, ["*intent-router*"]) is None
    assert subtree_exclusion_match(f, ["*old.tmp*"]) == "*old.tmp*"


def test_aged_temp_still_offers_top_level_files_with_exclusions_configured(tmp_path: Path) -> None:
    import time

    from reclaim import regenerable as r

    temp = tmp_path / "Temp"
    (temp / "old_dir").mkdir(parents=True)
    (temp / "old_dir" / "a.bin").write_bytes(b"x" * 100)
    (temp / "old_file.tmp").write_bytes(b"y" * 200)
    old = time.time() - 30 * 86400
    for p in (temp / "old_dir" / "a.bin", temp / "old_dir", temp / "old_file.tmp"):
        os.utime(p, (old, old))

    def never(*_a: object, **_k: object) -> object:
        raise AssertionError("dry run must not run commands")

    env = r.RegenerableEnv(
        home=tmp_path, local_appdata=tmp_path, temp_roots=(temp,), crash_dump_roots=(),
        running_process_names=lambda: frozenset(), which=lambda _n: None, run_command=never,
        has_open_handle=lambda _p: False, excluded_patterns=("*intent-router*",),
    )  # fmt: skip
    res = r._aged_children(temp, env, "aged_temp", "temp", "Temp", apply=False)
    assert res.excluded == []
    assert (res.bytes_removed, res.files_removed) == (300, 2)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_two_junctions_to_one_outside_tree_match_through_either_alias(tmp_path: Path) -> None:
    """The outside tree is covered once; a path-keyed pattern spelled through the second
    alias still matches (alias matching, only because a path-keyed pattern is configured)."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "j1"))
    _winapi.CreateJunction(str(outside), str(root / "j2"))
    for pattern in ("*secret*", "*/j1/secret.txt", "*/j2/secret.txt", "re:j2[\\\\/]secret"):
        assert subtree_exclusion_match(root, [pattern]) == pattern


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_dangling_junction_hides_nothing(tmp_path: Path) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    root = tmp_path / "root"
    gone = tmp_path / "gone"
    gone.mkdir()
    root.mkdir()
    _winapi.CreateJunction(str(gone), str(root / "dangling"))
    gone.rmdir()
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None


def test_the_unreadable_reason_says_what_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reclaim.config import Config, SafetyConfig
    from reclaim.models import FileRecord
    from reclaim.safety import SafetyValidator

    root = tmp_path / "cand"
    (root / "locked").mkdir(parents=True)
    real = os.scandir

    def flaky(path):  # type: ignore[no-untyped-def]
        if str(path).endswith("locked"):
            raise PermissionError(5, "denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    validator = SafetyValidator(Config(safety=SafetyConfig(deny=["*intent-router*"])))
    hit = validator.exclusion_match(root, is_dir=True)
    assert hit is not None
    assert hit.startswith(UNREADABLE_PREFIX)
    record = FileRecord(
        path=root,
        is_dir=True,
        size_bytes=0,
        attributes=0,
        ext="",
        git_repo_root=None,
        git_repo_clean=False,
    )
    result = validator.evaluate(record)
    assert "could not be read" in result.rationale
    assert "matches the user exclusion" not in result.rationale


# --- #190 verifier pass 2 -------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_to_a_directory_above_the_root_is_walked(tmp_path: Path) -> None:
    """`cand/j -> A` where cand sits in A: the walk must still see A's other children (the
    ancestor-prefix cycle check skipped it and missed `A/other/intent-router`)."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    a = tmp_path / "A"
    (a / "other" / "intent-router").mkdir(parents=True)
    cand = a / "cand"
    cand.mkdir()
    _winapi.CreateJunction(str(a), str(cand / "j"))
    assert subtree_exclusion_match(cand, ["*intent-router*"]) == "*intent-router*"
    assert subtree_exclusion_match(cand, ["*nothing-here*"]) is None  # and it ends


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_cycle_through_two_junctions_ends_without_a_marker(tmp_path: Path) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    d = tmp_path / "D"
    (d / "a").mkdir(parents=True)
    (d / "b").mkdir()
    _winapi.CreateJunction(str(d / "b"), str(d / "a" / "j"))
    _winapi.CreateJunction(str(d / "a"), str(d / "b" / "k"))
    (d / "b" / "two").mkdir()
    _winapi.CreateJunction(str(d / "a"), str(d / "b" / "two" / "k2"))  # two exits per lap
    assert subtree_exclusion_match(d, ["*intent-router*"]) is None
    (d / "b" / "intent-router").mkdir()
    assert subtree_exclusion_match(d, ["*intent-router*"]) == "*intent-router*"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_whose_target_cannot_be_stat_ed_is_still_walked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "does not exist" means dangling; access denied must walk it (and fail closed if
    listing fails too), never skip it."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    target = tmp_path / "target"
    (target / "intent-router").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(target), str(root / "j"))
    real_stat = os.stat

    def denied(path, *a, **k):  # type: ignore[no-untyped-def]
        if str(path).endswith(chr(92) + "j"):
            raise PermissionError(5, "Access is denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", denied)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


# --- #190 verifier pass 3 -------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Win32 strips trailing spaces/dots")
@pytest.mark.parametrize("name", ["sp ", "dot."])
def test_a_root_named_with_a_trailing_space_or_dot_is_still_walked(
    tmp_path: Path, name: str
) -> None:
    """abspath (GetFullPathNameW) strips the trailing space/dot, so the walk listed a path that
    does not exist and answered "nothing excluded" while the deleter (exact name) would delete
    the real directory with the excluded entry in it."""
    prefix = chr(92) * 2 + "?" + chr(92)
    exact = prefix + str(tmp_path / name)
    os.makedirs(exact + chr(92) + "intent-router")  # noqa: PTH103 -- the exact name needs it
    root = exact.removeprefix(prefix)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_to_a_file_hides_nothing(tmp_path: Path) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(f), str(root / "j"))
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def _counting_scandir(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = os.scandir

    def counting(path):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", counting)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_pnpm_shaped_tree_is_listed_once_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pnpm: packages live in .pnpm, every dependency is a junction to one of them. Rewalking
    each alias made such trees exceed any budget (#190 verifier pass 4); junctions into the
    candidate's own tree are not descended, so each directory is listed once."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    nm = tmp_path / "node_modules"
    store = nm / ".pnpm"
    pkgs = []
    for i in range(12):
        pkg = store / f"p{i}@1" / "node_modules" / f"p{i}"
        (pkg / "lib" / "deep").mkdir(parents=True)
        pkgs.append(pkg)
    for i, pkg in enumerate(pkgs):
        _winapi.CreateJunction(str(pkg), str(nm / f"p{i}"))  # top-level alias
        for j in range(6):  # every package depends on six others
            dep = pkgs[(i + j + 1) % len(pkgs)]
            _winapi.CreateJunction(str(dep), str(pkg.parent / f"dep{j}"))
    plain_dirs = sum(1 for _ in nm.rglob("*") if _.is_dir() and not _.is_junction())
    calls = _counting_scandir(monkeypatch)
    assert subtree_exclusion_match(nm, ["*intent-router*"]) is None
    assert len(calls) <= plain_dirs + 1  # listed once each, no alias rewalked


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_dense_junction_cycle_ends_quickly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    root = tmp_path / "dense"
    dirs = [root / f"n{i}" for i in range(8)]
    for d in dirs:
        d.mkdir(parents=True)
    for a in dirs:  # every directory links to every other one: factorial for alias rewalks
        for b in dirs:
            if a != b:
                _winapi.CreateJunction(str(b), str(a / f"to-{b.name}"))
    calls = _counting_scandir(monkeypatch)
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None
    assert len(calls) <= len(dirs) + 1


# --- #190 verifier pass 4 -------------------------------------------------------------------


def test_a_root_whose_real_path_cannot_be_resolved_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """realpath re-raises some reparse errors (WinError 4392 on an invalid mount point); the
    walk must answer, never raise into the caller (scan, evaluate, executor last-line check)."""
    root = tmp_path / "root"
    (root / "intent-router").mkdir(parents=True)
    real = safety._real_path

    def broken(path: str) -> str:
        if path.rstrip(chr(92)).endswith("root"):
            raise OSError(4392, "The data present in the reparse point buffer is invalid")
        return real(path)

    monkeypatch.setattr(safety, "_real_path", broken)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"
    assert subtree_exclusion_match(root, ["*nothing*"]) is None


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions; Win32 strips trailing spaces")
def test_a_trailing_space_root_is_not_confused_with_its_sibling(tmp_path: Path) -> None:
    """`tw ` holds a junction to its sibling `tw`, and only the sibling holds the excluded
    entry. With the root's real path taken from the plain form (Win32 strips the space) the
    root looked like `tw` itself, the junction looked like one back into the root, and the
    walk skipped it: "nothing excluded" (#190 verifier pass 4)."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    prefix = chr(92) * 2 + "?" + chr(92)
    exact = prefix + str(tmp_path / "tw ")
    os.makedirs(exact)  # noqa: PTH103 -- the exact name needs the prefix
    sibling = tmp_path / "tw"
    (sibling / "intent-router").mkdir(parents=True)
    _winapi.CreateJunction(str(sibling), exact + chr(92) + "j")
    root = exact.removeprefix(prefix)
    assert subtree_exclusion_match(root, ["*intent-router*"]) == "*intent-router*"


@pytest.mark.skipif(os.name != "nt", reason="Win32 strips trailing spaces")
def test_exclusion_match_walks_the_exact_path_when_resolve_names_a_sibling(tmp_path: Path) -> None:
    from reclaim.config import Config, SafetyConfig
    from reclaim.safety import SafetyValidator

    prefix = chr(92) * 2 + "?" + chr(92)
    exact = prefix + str(tmp_path / "sp2 ")
    os.makedirs(exact + chr(92) + "intent-router")  # noqa: PTH103 -- the exact name needs it
    (tmp_path / "sp2").mkdir()  # the clean sibling resolve() lands on
    validator = SafetyValidator(Config(safety=SafetyConfig(deny=["*intent-router*"])))
    assert validator.exclusion_match(Path(exact.removeprefix(prefix)), is_dir=True) == (
        "*intent-router*"
    )


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_whose_real_path_cannot_be_resolved_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(target), str(root / "j"))
    real = safety._real_path

    def broken(path: str) -> str:
        if path.endswith(chr(92) + "j"):
            raise OSError(4392, "The data present in the reparse point buffer is invalid")
        return real(path)

    monkeypatch.setattr(safety, "_real_path", broken)
    got = subtree_exclusion_match(root, ["*intent-router*"])
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)


# --- #190 verifier pass 5 -------------------------------------------------------------------


def _pnpm_app(tmp_path: Path) -> Path:
    """`myapp/node_modules/foo` is a junction into the store inside the same node_modules."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    nm = tmp_path / "myapp" / "node_modules"
    pkg = nm / ".pnpm" / "foo@1.0.0" / "node_modules" / "foo"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(pkg), str(nm / "foo"))
    return nm


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
@pytest.mark.parametrize(
    "pattern",
    [
        "*/myapp/node_modules/foo/*",
        "*/myapp/node_modules/foo/index.js",
        "re:node_modules.foo.index",
    ],
)
def test_a_path_keyed_deny_through_an_alias_inside_the_candidate_protects_it(
    tmp_path: Path, pattern: str
) -> None:
    """The content behind the alias is deleted with the candidate at its real place, so a deny
    spelled through the alias text must still protect the candidate (base did; the covering
    walk alone did not)."""
    nm = _pnpm_app(tmp_path)
    assert subtree_exclusion_match(nm, [pattern]) == pattern


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_path_keyed_deny_through_a_junction_to_an_ancestor(tmp_path: Path) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    a = tmp_path / "a"
    r = a / "r"
    r.mkdir(parents=True)
    (r / "f.txt").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(a), str(r / "up"))
    assert subtree_exclusion_match(r, ["*/up/r/f.txt"]) == "*/up/r/f.txt"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_name_only_patterns_never_rewalk_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi  # Windows only; CreateJunction needs no admin rights

    a = tmp_path / "a"
    r = a / "r"
    (r / "sub").mkdir(parents=True)
    (a / "other").mkdir()
    _winapi.CreateJunction(str(a), str(r / "up"))  # above the root: walked once, covering
    _winapi.CreateJunction(str(r / "sub"), str(r / "alias"))  # inside: not rewalked
    calls = _counting_scandir(monkeypatch)
    assert subtree_exclusion_match(r, ["*intent-router*"]) is None
    # r, r/sub, a (via up), a/other; r is not entered again under a, the alias is skipped
    assert len(calls) == 4


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_the_junction_walk_budget_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_JUNCTION_WALK_BUDGET", 0)
    nm = _pnpm_app(tmp_path)
    got = subtree_exclusion_match(nm, ["*/nothing/here/*"])
    assert got is not None
    assert got.startswith(UNREADABLE_PREFIX)
    assert "too many junctions" in got
    assert subtree_exclusion_match(nm, ["*nothing-here*"]) is None  # cover-once: no budget


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_cycle_between_two_outside_trees_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`root/j -> X`, `X/k -> Y`, `Y/k -> X`: both trees lie outside the candidate; each is
    walked once (its target is recorded as covered), so the walk ends."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    x, y, root = tmp_path / "X", tmp_path / "Y", tmp_path / "root"
    for d in (x, y, root):
        d.mkdir()
    _winapi.CreateJunction(str(x), str(root / "j"))
    _winapi.CreateJunction(str(y), str(x / "k"))
    _winapi.CreateJunction(str(x), str(y / "k"))
    calls = _counting_scandir(monkeypatch)
    assert subtree_exclusion_match(root, ["*intent-router*"]) is None
    assert len(calls) == 3  # root, X (via j), Y (via j/k)


# --- #190 verifier pass 6 -------------------------------------------------------------------


def _inner_link(tmp_path: Path) -> Path:
    """`cand/link` is a junction into `cand/store/pkg`, which holds `data.txt`."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    cand = tmp_path / "cand"
    pkg = cand / "store" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "data.txt").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(pkg), str(cand / "link"))
    return cand


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
@pytest.mark.parametrize(
    "pattern",
    [
        "*link*data*",  # glob `*` spans separators: not a one-component pattern
        "*link?data.txt",
        "*link[!a]data.txt",
        "*" + chr(92) + "link" + chr(92) + "data.txt",  # the HOWTO's backslash form
        "re:link.data[.]txt$",  # regex without a separator
    ],
)
def test_patterns_spanning_components_match_through_an_inner_junction(
    tmp_path: Path, pattern: str
) -> None:
    cand = _inner_link(tmp_path)
    assert subtree_exclusion_match(cand, [pattern]) == pattern


@pytest.mark.parametrize(
    ("pattern", "one_component"),
    [
        ("*intent-router*", True),
        ("*fr-en-transformer*", True),
        ("*link*data*", False),
        ("*a?b*", False),
        ("*a[b]c*", False),
        ("*/x/*", False),
        ("*" + chr(92) + "x" + chr(92) + "*", False),
        ("re:abc", False),
        ("abc*", False),
    ],
)
def test_only_star_literal_star_patterns_take_the_cover_once_walk(
    pattern: str, one_component: bool
) -> None:
    assert safety._one_component_only(pattern) is one_component


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_realistic_pnpm_tree_with_a_path_keyed_pattern_is_still_offered(
    tmp_path: Path,
) -> None:
    """A full walk of a pnpm-shaped tree (120 packages, 4 dependents each, 4 directories per
    package: ~2.4k junction-reached listings) stays far under the budget and answers None."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    nm = tmp_path / "node_modules"
    pkgs = []
    for i in range(120):
        pkg = nm / ".pnpm" / f"p{i}@1" / "node_modules" / f"p{i}"
        (pkg / "lib" / "a" / "b").mkdir(parents=True)
        pkgs.append(pkg)
    for i, pkg in enumerate(pkgs):
        _winapi.CreateJunction(str(pkg), str(nm / f"p{i}"))
        for j in range(4):
            _winapi.CreateJunction(str(pkgs[(i + j + 1) % len(pkgs)]), str(pkg.parent / f"d{j}"))
    assert subtree_exclusion_match(nm, ["*/nothing/here/*"]) is None


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_the_full_walk_cuts_cycles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path-keyed pattern selects the full walk; its cycle guard (a junction whose target is
    already on the walk path) must still end every cycle quickly."""
    import _winapi  # Windows only; CreateJunction needs no admin rights

    d = tmp_path / "D"
    (d / "a").mkdir(parents=True)
    (d / "b").mkdir()
    _winapi.CreateJunction(str(d / "b"), str(d / "a" / "j"))
    _winapi.CreateJunction(str(d / "a"), str(d / "b" / "k"))
    _winapi.CreateJunction(str(d), str(d / "a" / "up"))
    calls = _counting_scandir(monkeypatch)
    assert subtree_exclusion_match(d, ["*/nothing/here/*"]) is None
    assert len(calls) < 30


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_the_full_walk_budget_ignores_trees_without_junctions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safety, "_JUNCTION_WALK_BUDGET", 3)
    plain = tmp_path / "plain"
    for i in range(10):
        (plain / f"d{i}" / "e").mkdir(parents=True)
    assert subtree_exclusion_match(plain, ["*/nothing/here/*"]) is None  # 20 dirs, no junction
