from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path

import pytest

from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FileRecord, Verdict
from reclaim.safety import SafetyValidator, _canonical_path


def _record(
    path: str,
    *,
    is_dir: bool = False,
    size_bytes: int = 1024,
    attributes: int = 0,
    git_repo_root: Path | None = None,
    git_repo_clean: bool = False,
) -> FileRecord:
    p = Path(path)
    return FileRecord(
        path=p,
        is_dir=is_dir,
        size_bytes=size_bytes,
        attributes=attributes,
        ext=p.suffix.lower(),
        git_repo_root=git_repo_root,
        git_repo_clean=git_repo_clean,
    )


@pytest.fixture
def validator() -> SafetyValidator:
    return SafetyValidator(
        Config(
            safety=SafetyConfig(
                protected_roots=["C:/Windows/*"],
                deny=["*/deny-me/*"],
                allow=["*/allow-me/*"],
            ),
            categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)),
        )
    )


def test_builtin_deny_beats_user_allow(validator: SafetyValidator) -> None:
    record = _record("C:/Windows/allow-me/system.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_user_deny_beats_default_eligible(validator: SafetyValidator) -> None:
    record = _record("C:/Data/deny-me/report.csv")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "USER_DENY_LIST"


def test_user_allow_promotes_review_only_finance_doc(validator: SafetyValidator) -> None:
    record = _record("C:/Data/allow-me/2025_tax_return.pdf")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "USER_ALLOW_LIST_OVERRIDE"


def test_finance_pattern_without_allow_is_review_only(validator: SafetyValidator) -> None:
    record = _record("C:/Data/2025_tax_return.pdf")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.REVIEW_ONLY
    assert result.reason_code == "FINANCE_LEGAL_DOCUMENT"


def test_default_eligible_for_benign_file(validator: SafetyValidator) -> None:
    record = _record("C:/Data/notes.txt")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "DEFAULT_ELIGIBLE"


def test_git_repo_blocks_by_default(validator: SafetyValidator) -> None:
    record = _record(
        "C:/Data/repo/src/main.py",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=True,
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "IN_GIT_REPOSITORY"


def test_node_modules_exempt_when_clean_and_category_enabled(validator: SafetyValidator) -> None:
    record = _record(
        "C:/Data/repo/node_modules/pkg/index.js",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=True,
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "DEV_ARTIFACTS_NODE_MODULES_EXEMPT"


def test_node_modules_blocked_when_repo_dirty(validator: SafetyValidator) -> None:
    record = _record(
        "C:/Data/repo/node_modules/pkg/index.js",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=False,
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "GIT_REPO_NODE_MODULES_DIRTY"


def test_node_modules_blocked_when_category_disabled() -> None:
    disabled_validator = SafetyValidator(
        Config(categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=False)))
    )
    record = _record(
        "C:/Data/repo/node_modules/pkg/index.js",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=True,
    )
    result = disabled_validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "IN_GIT_REPOSITORY"


@pytest.mark.parametrize("ext", [".kdbx", ".ppk", ".pem", ".key", ".pfx", ".crt", ".gpg"])
def test_protected_extensions_blocked(validator: SafetyValidator, ext: str) -> None:
    result = validator.evaluate(_record(f"C:/Data/secret{ext}"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_EXTENSION"


def test_ssh_directory_blocked(validator: SafetyValidator) -> None:
    result = validator.evaluate(_record("C:/Users/gg/.ssh/id_rsa"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "SSH_DIRECTORY"


@pytest.mark.parametrize("ext", [".db", ".sqlite", ".mdf"])
def test_database_extensions_blocked(validator: SafetyValidator, ext: str) -> None:
    result = validator.evaluate(_record(f"C:/Data/app{ext}"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "DATABASE_FILE"


@pytest.mark.parametrize("ext", [".vhdx", ".vmdk", ".qcow2"])
def test_vm_extensions_blocked(validator: SafetyValidator, ext: str) -> None:
    result = validator.evaluate(_record(f"C:/VMs/disk{ext}"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "VM_IMAGE"


def test_docker_wsl_root_blocked(validator: SafetyValidator) -> None:
    result = validator.evaluate(_record("C:/Users/gg/AppData/Local/Docker/data.dat"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "DOCKER_WSL_DATA_ROOT"


def test_cloud_placeholder_blocked(validator: SafetyValidator) -> None:
    record = _record("C:/OneDrive/photo.jpg", attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "CLOUD_PLACEHOLDER"


def test_filter_candidates_preserves_order(validator: SafetyValidator) -> None:
    records = [_record("C:/Data/notes.txt"), _record("C:/Windows/system.dll")]
    results = validator.filter_candidates(records)
    assert [r.verdict for r in results] == [Verdict.ELIGIBLE, Verdict.BLOCKED]


def test_protected_root_denial_is_unaffected_by_process_elevation_state(
    validator: SafetyValidator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SafetyValidator` is a pure pattern match with no OS-permission or elevation-state
    dependency anywhere in it — confirms that holds even if the process happened to be
    elevated (`reclaim.elevation.is_elevated()` mocked True here), which is the scenario the
    no-elevation CLI guard (`assert_not_elevated`, wired into every mutating command) exists to
    make unreachable in the first place. Same verdict, same reason code, regardless."""
    from reclaim import elevation

    record = _record("C:/Windows/allow-me/system.dll")

    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    not_elevated_result = validator.evaluate(record)

    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    elevated_result = validator.evaluate(record)

    assert not_elevated_result.verdict == Verdict.BLOCKED
    assert elevated_result.verdict == Verdict.BLOCKED
    assert elevated_result.reason_code == not_elevated_result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_path_is_protected_root_matches_protected_roots_pattern(
    validator: SafetyValidator,
) -> None:
    """Used by `executor.restore_batch`'s manifest-integrity guard, which validates a restore
    *destination* that doesn't exist yet — so unlike `evaluate()`, this needs no `FileRecord`/
    stat at all, just the path string."""
    assert validator.path_is_protected_root(Path("C:/Windows/system.dll")) is True
    assert validator.path_is_protected_root(Path("C:/Data/notes.txt")) is False


def test_path_is_protected_root_matches_docker_wsl_roots(validator: SafetyValidator) -> None:
    assert validator.path_is_protected_root(Path("C:/Users/gg/AppData/Local/Docker/data")) is True


# --- `re:`-prefixed regex patterns (deny/allow) -------------------------------------------------


def test_re_prefixed_deny_pattern_blocks_matching_path_and_allows_non_matching() -> None:
    """A `re:`-prefixed deny pattern is regex, not glob -- must actually block a path matching
    the regex and must NOT block a path that doesn't, proving the regex is real (not silently
    matching everything or nothing)."""
    validator = SafetyValidator(Config(safety=SafetyConfig(deny=[r"re:/scratch/.*\.tmp$"])))
    matching = _record("C:/scratch/build_output.tmp")
    result = validator.evaluate(matching)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "USER_DENY_LIST"

    non_matching = _record("C:/scratch/build_output.log")
    result = validator.evaluate(non_matching)
    assert result.verdict == Verdict.ELIGIBLE


def test_re_prefixed_allow_pattern_promotes_finance_doc_and_leaves_non_matching_review_only() -> (
    None
):
    """Same regex mechanism on the allow-list side: a finance-tokened file matching a `re:`
    allow pattern is promoted to eligible (USER_ALLOW_LIST_OVERRIDE); a finance-tokened file
    that doesn't match is left at REVIEW_ONLY."""
    custom_validator = SafetyValidator(
        Config(safety=SafetyConfig(allow=[r"re:/allow-me/.*\.pdf$"]))
    )
    matching = _record("C:/Data/allow-me/2025_tax_return.pdf")
    result = custom_validator.evaluate(matching)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "USER_ALLOW_LIST_OVERRIDE"

    non_matching = _record("C:/Data/2025_tax_return.pdf")
    result = custom_validator.evaluate(non_matching)
    assert result.verdict == Verdict.REVIEW_ONLY
    assert result.reason_code == "FINANCE_LEGAL_DOCUMENT"


def test_re_prefixed_pattern_is_case_insensitive() -> None:
    """`_pattern_matches`'s `re:` branch passes `re.IGNORECASE` explicitly -- an upper/mixed-case
    path must still match a lowercase regex pattern."""
    validator = SafetyValidator(Config(safety=SafetyConfig(deny=[r"re:/deny-me/.*\.dat$"])))
    result = validator.evaluate(_record("C:/DENY-ME/FILE.DAT"))
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "USER_DENY_LIST"


# --- Plain USER_ALLOW_LIST (no finance token present) --------------------------------------------


def test_user_allow_list_without_finance_token_gets_plain_reason_code(
    validator: SafetyValidator,
) -> None:
    """An allow-listed path with no finance/tax/legal token in its name gets REASON_USER_ALLOW_LIST
    specifically -- distinct from REASON_USER_ALLOW_LIST_OVERRIDE, which only fires when a
    finance token IS present."""
    record = _record("C:/Data/allow-me/random_notes.txt")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "USER_ALLOW_LIST"


# --- D13: UNC-aliased-path safety bypass ---------------------------------------------------------
#
# Audit finding: `_pattern_matches`/`_any_pattern_matches` match `path.as_posix()` against
# drive-letter-form glob patterns (`DEFAULT_PROTECTED_ROOTS` is entirely `"C:/Windows"`-style).
# A UNC administrative-share alias of the identical on-disk file (`\\localhost\C$\Windows\...`,
# `\\127.0.0.1\C$\...`) never matches a `C:/Windows/*` pattern, so every pattern-based deny list
# silently passed it before this fix. Without the fix (i.e. reverting the `_is_unc_network_path`
# check in `_builtin_deny`), every test below that asserts BLOCKED/UNC_NETWORK_PATH for a UNC path
# would instead see the record fall through to DEFAULT_ELIGIBLE.


def test_unc_localhost_alias_of_protected_root_denied(validator: SafetyValidator) -> None:
    """The exact adversarial case named in the audit: a file physically under C:\\Windows,
    represented as a `\\\\localhost\\C$\\...` UNC admin-share alias, must still be denied --
    even though `validator`'s `protected_roots` is drive-letter-only (`C:/Windows/*`)."""
    record = _record(r"\\localhost\C$\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_loopback_ip_alias_of_protected_root_denied(validator: SafetyValidator) -> None:
    """Same as the localhost-hostname case, but via the `127.0.0.1` loopback IP alias --
    proves the check isn't keyed to the literal string "localhost"."""
    record = _record(r"\\127.0.0.1\C$\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_arbitrary_remote_host_alias_also_denied(validator: SafetyValidator) -> None:
    """Not merely a localhost-alias blocklist: a well-formed, drive-C-equivalent UNC path
    naming a totally different (hypothetical, non-localhost) host is denied too -- proving this
    is a blanket UNC-form deny, not an incomplete enumeration of "this machine" aliases."""
    record = _record(r"\\some-other-host\C$\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_alias_of_docker_wsl_root_also_denied(validator: SafetyValidator) -> None:
    """The same protection applies for `docker_wsl_roots`, not just `protected_roots` -- blocked
    via the blanket UNC check, upstream of (and pre-empting) the docker_wsl_roots pattern match
    itself. Note: `DEFAULT_DOCKER_WSL_ROOTS`'s entries are all leading-`*/`-relative (e.g.
    `*/AppData/Local/Docker/*`), so this exact path was incidentally still caught by the
    pre-existing pattern match even without the D13 fix (reason code differs, not verdict) --
    `test_unc_alias_of_drive_anchored_pattern_list_entry_denied` below proves a genuine
    false-negative closure for a drive-anchored pattern, which is the shape that actually leaked
    before this fix."""
    record = _record(r"\\localhost\C$\Users\gg\AppData\Local\Docker\data.dat")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_alias_of_drive_anchored_pattern_list_entry_denied() -> None:
    """A genuine false-negative demonstration (not merely a reason-code change) for a
    pattern-based list OTHER than `protected_roots`: `_any_pattern_matches` is a shared helper
    used by `docker_wsl_roots` (and any future pattern-based list) too, so a drive-anchored
    `docker_wsl_roots` entry is exactly as vulnerable to UNC aliasing as `protected_roots` was --
    proving D13's fix closes the gap at the shared-helper level, not just for the one list that
    happens to ship entirely leading-wildcard patterns by default."""
    custom_validator = SafetyValidator(
        Config(safety=SafetyConfig(docker_wsl_roots=["C:/DockerData", "C:/DockerData/*"]))
    )
    record = _record(r"\\localhost\C$\DockerData\volumes\data.dat")
    result = custom_validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_extended_length_form_also_denied(validator: SafetyValidator) -> None:
    r"""The `\\?\UNC\host\share\...` extended-length spelling of a UNC path (distinct from the
    plain `\\host\share\...` form) must be caught too -- it denotes the identical network share,
    just in the long-path-safe string form."""
    record = _record(r"\\?\UNC\localhost\C$\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_deny_beats_user_allow_list(validator: SafetyValidator) -> None:
    """Precedence: the UNC-network-path built-in deny is checked inside `_builtin_deny`, so
    (matching every other built-in deny reason) a user allow-list entry can never override it --
    same precedence `PROTECTED_SYSTEM_ROOT` already has (`test_builtin_deny_beats_user_allow`)."""
    record = _record(r"\\localhost\C$\Windows\allow-me\system.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_plain_drive_letter_path_unaffected_by_unc_check(validator: SafetyValidator) -> None:
    """Zero new false positives: an ordinary drive-letter path, nowhere near any protected root,
    is completely unaffected by the D13 fix."""
    record = _record(r"C:\Users\gg\Documents\file.txt")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "DEFAULT_ELIGIBLE"


def test_extended_length_local_drive_path_not_mistaken_for_unc(validator: SafetyValidator) -> None:
    r"""D12's `\\?\C:\...` extended-length-path prefix (a normal local drive-letter path that
    merely bypasses MAX_PATH) must keep evaluating exactly like its unprefixed equivalent --
    NOT be mistaken for a UNC network path just because it starts with `\\`."""
    record = _record(r"\\?\C:\Users\gg\Documents\file.txt")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "DEFAULT_ELIGIBLE"


def test_extended_length_local_drive_path_under_protected_root_still_blocked(
    validator: SafetyValidator,
) -> None:
    r"""Same D12 extended-length local form, but under a genuinely protected drive-letter root --
    confirms it still hits PROTECTED_SYSTEM_ROOT (via the ordinary pattern match), not
    UNC_NETWORK_PATH, and not a false-negative pass-through either."""
    record = _record(r"\\?\C:\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_path_is_protected_root_denies_unc_alias(validator: SafetyValidator) -> None:
    """`executor.restore_batch`'s stat-free restore-destination guard gets the same D13
    protection as the full `evaluate()` pipeline."""
    assert validator.path_is_protected_root(Path(r"\\localhost\C$\Windows\system.dll")) is True
    assert validator.path_is_protected_root(Path(r"\\some-other-host\C$\Windows\file")) is True
    assert validator.path_is_protected_root(Path(r"\\?\C:\Windows\system.dll")) is True


# --- D13 second pass: enumerated alias-form audit ------------------------------------------------
#
# GG's brief: enumerate every path-alias form that could reach the same physical file as a
# protected root without matching a drive-letter glob pattern, and prove (via a real test)
# whether each is denied or leaks through TODAY. Forms 1-3 (\\?\C:\..., \\?\UNC\...,
# \\localhost|127.0.0.1\C$\...) were already closed by pass 1 -- the D13 block above re-runs
# unchanged against this second-pass code (and still passes), re-confirming them; no new tests
# added for those three. Forms 8 (mixed slashes) and 10 (case variation) were already handled by
# `_pattern_matches`'s `.as_posix()`/`.lower()` calls -- confirmed below rather than assumed.
#
# Form 11 (Unicode NFC/NFD) is D11's territory, not D13's: D11 normalizes at
# `build_record_for_path`'s `entry.name` comparison boundary (the point a caller-supplied
# `path.name` string is matched against real scandir entries), and `FileRecord.path` itself is
# always built from `entry.name` VERBATIM (see D11's own docstring) -- so by the time a record
# reaches `SafetyValidator`, its path is already whatever NTFS actually returned, with no further
# NFC/NFD ambiguity for `_pattern_matches` to introduce. `protected_roots`/`docker_wsl_roots`
# patterns are pure ASCII, so NFC/NFD variance in an unrelated path segment can never affect
# whether they match. No new test added here -- manufacturing one would just re-test D11's
# already-covered fix under a different name.
#
# Forms 4 (8.3 short names), 6 (subst'd drives), 7 (junctions/symlinks), and the in-scope slice
# of 5 (mapped network drives looping back to a local admin share) were genuinely open gaps at
# the `SafetyValidator` pattern-matching layer before this pass -- closed by resolving to a
# canonical real path (`_canonical_path`) before any pattern/UNC check, per GG's explicit
# preference for one structural fix over a growing special-case list. Form 9 (trailing dot/space)
# turned out to already be closed as a side effect of the same fix (`resolve()` strips both).


def _validator_with_protected_root(root: Path) -> SafetyValidator:
    """Builds a `SafetyValidator` whose `protected_roots` matches the given REAL directory --
    used by every real-filesystem alias test below so the protected-root pattern lines up with
    an actual fixture, not the module-level `validator` fixture's fixed `C:/Windows/*`."""
    return SafetyValidator(
        Config(
            safety=SafetyConfig(protected_roots=[f"{root.as_posix()}/*"]),
            categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)),
        )
    )


def test_mixed_forward_and_back_slashes_still_matches_protected_root(
    validator: SafetyValidator,
) -> None:
    """Form 8: `_pattern_matches` already normalizes via `.as_posix()` before comparing -- a
    path spelled with backslashes must still match a forward-slash-form protected_roots pattern.
    Confirmed with a real test rather than assumed, per the audit brief."""
    record = _record(r"C:\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_case_variation_still_matches_protected_root(validator: SafetyValidator) -> None:
    """Form 10: `_pattern_matches` already lowercases both sides before `fnmatch` -- a
    differently-cased drive letter/segment must still match. Confirmed with a real test (the
    existing case-insensitivity coverage in this file is regex-pattern-only, not glob-pattern)."""
    record = _record(r"c:\WINDOWS\System32\KERNEL32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_8dot3_short_name_alias_of_protected_root_denied(tmp_path: Path) -> None:
    r"""Form 4: an 8.3 short-name alias (`C:\PROGRA~1`-style) of a real, long-named protected
    directory must still be denied. Requires 8.3 short-name generation to actually be enabled on
    the volume `tmp_path` lives on (the Windows default, but an administrator can disable it via
    `fsutil 8dot3name`) -- honestly skipped, not silently passed, if this volume doesn't produce
    a distinct short form.

    Proven to fail without the fix: `_canonical_path`'s `resolve()` call is what turns the short
    alias back into the long form before pattern-matching; reverting to `_pattern_matches`
    matching `record.path` directly (pass 1's shape) leaves the short-name `.as_posix()` string
    compared against a long-name-form pattern, which `fnmatch` never matches -- DEFAULT_ELIGIBLE
    instead of BLOCKED (verified manually against the pre-canonicalization code during review).
    """
    real_dir = tmp_path / "Protected Root With A Long Name"
    real_dir.mkdir()
    target_file = real_dir / "payload.dll"
    target_file.write_text("stand-in")

    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(real_dir), buf, 260)  # type: ignore[attr-defined]
    if not n or buf.value == str(real_dir):
        pytest.skip("8.3 short-name generation is disabled on this volume (fsutil 8dot3name)")

    short_alias = Path(buf.value) / "payload.dll"
    assert short_alias.exists(), "short-name alias must resolve to the same real file"

    validator = _validator_with_protected_root(real_dir)
    record = _record(str(short_alias))
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_substd_drive_alias_of_protected_root_denied(tmp_path: Path) -> None:
    r"""Form 6: `subst Y: C:\Windows` creates a drive letter that IS the protected root, with no
    special privilege required -- a real, easy bypass of drive-letter-form pattern matching if
    left unaddressed. Picks the first free drive letter from a fixed candidate pool; skips if
    none is free rather than failing the suite on a machine with an unusual drive layout.

    Cleans up the `subst` mapping in `finally` even if the assertion fails, matching this
    project's real-filesystem test convention (see `tests/test_scanner.py::_make_deep_tree` and
    its callers for the same discipline around `\\?\`-prefixed fixtures)."""
    real_dir = tmp_path / "Protected Root For Subst"
    real_dir.mkdir()
    (real_dir / "payload.dll").write_text("stand-in")

    used_letters = {d for d in "ZYXWVUTSRQPONMLKJIHGFEDCBA" if Path(d + ":\\").exists()}
    free_letters = [c for c in "ZYXWVUTSRQ" if c not in used_letters]
    if not free_letters:
        pytest.skip("no free drive letter available to subst on this machine")
    drive = free_letters[0] + ":"

    mount = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["subst", drive, str(real_dir)],  # noqa: S607 -- subst is a builtin
        capture_output=True,
        text=True,
    )
    if mount.returncode != 0:
        pytest.skip(f"subst failed on this machine: {mount.stderr.strip() or mount.stdout.strip()}")
    try:
        substituted_path = Path(drive + "\\payload.dll")
        assert substituted_path.exists(), "subst'd drive must resolve to the same real file"

        validator = _validator_with_protected_root(real_dir)
        record = _record(str(substituted_path))
        result = validator.evaluate(record)
        assert result.verdict == Verdict.BLOCKED
        assert result.reason_code == "PROTECTED_SYSTEM_ROOT"
    finally:
        subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
            ["subst", drive, "/D"],  # noqa: S607 -- subst is a builtin
            capture_output=True,
            text=True,
        )


def test_junction_into_protected_root_denied(tmp_path: Path) -> None:
    r"""Form 7: an NTFS junction (`mklink /J innocuous_folder C:\Windows\System32`) makes an
    innocuous-looking path physically BE a protected directory -- the hardest form to close via
    pattern-matching (no pattern on the alias string `innocuous_folder\...` could ever guess the
    real target), which is exactly why canonical-real-path resolution (not another special case)
    is the right fix. No special privilege required to create a junction (unlike a symlink --
    see the skipped symlink test below).

    Cleans up the junction in `finally` even if the assertion fails."""
    real_dir = tmp_path / "Protected Root For Junction"
    real_dir.mkdir()
    (real_dir / "payload.dll").write_text("stand-in")

    innocuous = tmp_path / "innocuous_folder"
    created = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(innocuous), str(real_dir)],  # noqa: S607 -- cmd is a builtin
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"mklink /J failed on this machine: {created.stderr.strip()}")
    try:
        junctioned_path = innocuous / "payload.dll"
        assert junctioned_path.exists(), "junction must resolve to the same real file"

        validator = _validator_with_protected_root(real_dir)
        record = _record(str(junctioned_path))
        result = validator.evaluate(record)
        assert result.verdict == Verdict.BLOCKED
        assert result.reason_code == "PROTECTED_SYSTEM_ROOT"
    finally:
        innocuous.rmdir()


def test_symlink_into_protected_root_denied(tmp_path: Path) -> None:
    r"""Form 7 (symlink variant): same physical-aliasing shape as the junction test above, but
    via an NTFS directory symlink instead of a junction. Unlike a junction, creating a symlink
    requires `SeCreateSymbolicLinkPrivilege` (elevated process, or Developer Mode enabled) --
    this tool's own threat model assumes a NON-elevated user (the safety model exists precisely
    because reclaim itself refuses to run elevated), and CI runners typically run unprivileged
    too, so this is expected to skip in most environments. Honest skip with the real `OSError`
    reason, not a silent pass -- if it DOES run (e.g. Developer Mode enabled locally), it proves
    the same canonical-resolution fix that closes the junction case also closes the symlink case,
    since both are NTFS reparse points resolved identically by `GetFinalPathNameByHandle`
    (`Path.resolve()`'s underlying mechanism)."""
    real_dir = tmp_path / "Protected Root For Symlink"
    real_dir.mkdir()
    (real_dir / "payload.dll").write_text("stand-in")

    symlinked = tmp_path / "symlinked_folder"
    try:
        symlinked.symlink_to(real_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation requires elevation/Developer Mode, unavailable here: {exc}")
    try:
        symlinked_path = symlinked / "payload.dll"
        assert symlinked_path.exists(), "symlink must resolve to the same real file"

        validator = _validator_with_protected_root(real_dir)
        record = _record(str(symlinked_path))
        result = validator.evaluate(record)
        assert result.verdict == Verdict.BLOCKED
        assert result.reason_code == "PROTECTED_SYSTEM_ROOT"
    finally:
        if symlinked.is_symlink():
            symlinked.unlink()
        else:
            symlinked.rmdir()


def test_mapped_network_drive_looping_back_to_local_admin_share_denied() -> None:
    r"""Form 5, the in-scope slice only: `net use Z: \\localhost\C$` maps a drive letter to a
    UNC share that happens to loop back to THIS machine's own local disk -- the general "mapped
    drive to an unrelated remote host's storage" case is an explicit non-goal (see the module
    comment above this block): a genuinely remote share is a different physical disk entirely,
    never "the same protected root" this tool would need to reason about. This test proves the
    LOOPBACK case specifically, which is in-scope because the mapped drive denotes the identical
    local file a `protected_roots` pattern is trying to protect.

    Requires connecting to the built-in `C$` administrative share, which needs local-admin-
    equivalent rights even over loopback on some Windows configurations -- honestly skipped, not
    silently passed, if the connection fails here.

    Not closed by a NEW check: `_canonical_path` resolves the mapped drive to its UNC form
    (`\\localhost\C$\...`), and the EXISTING `_is_unc_network_path(canonical_path)` check in
    `_builtin_deny` (checked against the canonical form, not just the raw input, as of this
    pass) then denies it exactly like any other UNC path -- proving the general canonicalization
    fix closes this form as a free side effect, with zero new pattern/special-case code."""
    used_letters = {d for d in "ZYXWVUTSRQPONMLKJIHGFEDCBA" if Path(d + ":\\").exists()}
    free_letters = [c for c in "ZYXWVUTSRQ" if c not in used_letters]
    if not free_letters:
        pytest.skip("no free drive letter available to map on this machine")
    drive = free_letters[0] + ":"

    mount = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["net", "use", drive, r"\\localhost\C$"],  # noqa: S607 -- net is a builtin
        capture_output=True,
        text=True,
    )
    if mount.returncode != 0:
        pytest.skip(
            r"net use \\localhost\C$ failed on this machine (needs local-admin-equivalent "
            f"rights even over loopback): {mount.stderr.strip() or mount.stdout.strip()}"
        )
    try:
        mapped_windows_path = Path(drive + "\\Windows\\System32\\kernel32.dll")
        assert mapped_windows_path.exists(), r"mapped drive must reach the real C:\Windows"

        validator = SafetyValidator(Config(safety=SafetyConfig(protected_roots=["C:/Windows/*"])))
        record = _record(str(mapped_windows_path))
        result = validator.evaluate(record)
        assert result.verdict == Verdict.BLOCKED
        assert result.reason_code == "UNC_NETWORK_PATH"
    finally:
        subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
            ["net", "use", drive, "/delete", "/y"],  # noqa: S607 -- net is a builtin
            capture_output=True,
            text=True,
        )


def test_trailing_dot_and_space_alias_of_protected_root_denied(tmp_path: Path) -> None:
    r"""Form 9: Windows silently strips trailing dots/spaces off a path component at the Win32
    API layer -- `C:\SomeDir.` and `C:\SomeDir ` can denote the identical directory as
    `C:\SomeDir`. Turned out to already be closed as a side effect of the canonical-resolution
    fix (`Path.resolve()` strips both), confirmed empirically rather than assumed; this test
    proves it, using a real fixture directory (not just a synthetic Path) so the "does this alias
    actually reach the same on-disk file" claim is genuine, not merely string-plausible.

    Proof-of-same-file uses `.resolve() == target.resolve()`, not `.exists()`: empirically, a
    trailing space stripped ONLY when it's the very last component of the whole path string
    (`Path("...RealDir ").exists()` is `True`) is NOT stripped by `Path.exists()` when it's an
    INTERMEDIATE component with a further segment appended after it
    (`(Path("...RealDir ") / "file").exists()` is `False`, even though the file genuinely exists)
    -- `Path.resolve()` handles both cases correctly (confirmed empirically), so it -- not
    `.exists()` -- is the right same-file proof here, and is also the exact mechanism
    `_canonical_path` itself relies on."""
    real_dir = tmp_path / "Protected Root With Trailing Chars"
    real_dir.mkdir()
    target_file = real_dir / "payload.dll"
    target_file.write_text("stand-in")

    for suffix, label in [(".", "trailing dot"), (" ", "trailing space")]:
        aliased_dir = Path(str(real_dir) + suffix)
        aliased_file = aliased_dir / "payload.dll"
        assert aliased_file.resolve() == target_file.resolve(), (
            f"{label} alias must resolve to the same real file"
        )

        validator = _validator_with_protected_root(real_dir)
        record = _record(str(aliased_file))
        result = validator.evaluate(record)
        assert result.verdict == Verdict.BLOCKED, label
        assert result.reason_code == "PROTECTED_SYSTEM_ROOT", label


def test_canonical_path_falls_back_to_unresolved_on_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_canonical_path`'s `except OSError` fallback: if `resolve()` itself raises (e.g. a
    genuinely inaccessible volume), the function must return the path UNRESOLVED rather than
    propagate the exception -- this safety-critical helper's failure mode is "fall back to
    today's plain-pattern-match behavior for this one path", never "crash the safety
    evaluation". Forces the failure via `monkeypatch` (real-world triggers for this branch --
    an unmapped/disconnected volume -- aren't reliably reproducible in a test fixture)."""

    def _raise(self: Path) -> Path:
        raise OSError("simulated: volume inaccessible")

    monkeypatch.setattr(Path, "resolve", _raise)

    original = Path("C:/Data/notes.txt")
    result = _canonical_path(original)

    assert result == original
