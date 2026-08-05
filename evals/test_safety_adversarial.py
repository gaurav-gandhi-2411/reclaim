from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reclaim.config import (
    DEFAULT_DATABASE_EXTENSIONS,
    DEFAULT_DOCKER_WSL_ROOTS,
    DEFAULT_PROTECTED_EXTENSIONS,
    DEFAULT_PROTECTED_ROOTS,
    DEFAULT_VM_EXTENSIONS,
    CategoriesConfig,
    Config,
    DevArtifactsConfig,
    SafetyConfig,
)
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FileRecord, Verdict
from reclaim.safety import SafetyValidator

# Adversarial security-property proof for `SafetyValidator`, complementing
# `evals/test_safety_gate.py` (golden-tree fixture match + hard ELIGIBLE-leak gate) and
# `tests/test_safety.py` (unit coverage + the D13 alias-form audit). This file's job is
# specifically:
#
#   1. Every documented built-in-deny category actually blocks a REAL representative path under
#      the PRODUCTION default config (`reclaim.config.DEFAULT_*`) -- not a fixture-substituted
#      list, and not a guessed path: every representative path below is derived from, and
#      coverage-checked against, the actual `DEFAULT_*` tuples in `reclaim.config`.
#   2. Precedence proven directly, not rule-by-rule: a single path/config where more than one
#      rule could apply, confirming the documented winner (built-in deny > user deny-list >
#      built-in review-only-override > user allow-list > default eligible) actually wins.
#   3. `..`-traversal-style paths (the one path-obfuscation form in scope for this pass that
#      neither `tests/test_safety.py`'s D13 alias-form audit nor the golden tree covers).
#   4. Boundary cases for the git-repo/node_modules exemption and the cloud-placeholder
#      built-in-deny that aren't already proven elsewhere.
#   5. `path_is_protected_root()` (the separate, stat-free code path `executor.restore_batch`
#      uses to guard restore destinations) exercised independently of `evaluate()`, including
#      its documented narrower scope.
#
# Deliberately NOT re-tested here (would just re-prove an existing fix under a different name,
# same discipline as the D13-second-pass block in `tests/test_safety.py`): 8.3 short names,
# subst'd drives, junction/symlink aliasing of `protected_roots` itself, UNC-vs-local-drive form,
# mixed-case pattern matching, and the `re:`-prefixed regex deny/allow mechanism -- all already
# covered there with real-filesystem proofs.


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


# --- Section 1: every built-in-deny category, PRODUCTION default config, real paths -------------
#
# `Config()`/`SafetyValidator(Config())` below use the real `DEFAULT_*` tuples (no fixture
# override) -- per the audit brief, "read the actual default config, don't guess paths". Every
# representative path is drawn from an EXISTING real file/directory on this dev machine
# (`C:\Windows`, `C:\Program Files`, etc. all really exist here -- confirmed manually before
# writing this file) or a nonexistent-but-real-prefix path, matching `_canonical_path`'s
# documented "never requires the full path to exist" contract. `SafetyValidator.evaluate()` never
# writes to disk and `Path.resolve(strict=False)` is read-only, so this never mutates the real
# filesystem -- consistent with every other real-path test already in this suite (e.g.
# `tests/test_safety.py::test_mixed_forward_and_back_slashes_still_matches_protected_root`).

_PROTECTED_ROOT_CASES: dict[str, tuple[str, bool]] = {
    "C:/Windows": ("C:/Windows", True),
    "C:/Windows/*": ("C:/Windows/System32/kernel32.dll", False),
    "C:/Program Files": ("C:/Program Files", True),
    "C:/Program Files/*": ("C:/Program Files/Vendor/app.exe", False),
    "C:/Program Files (x86)": ("C:/Program Files (x86)", True),
    "C:/Program Files (x86)/*": ("C:/Program Files (x86)/Vendor/app.exe", False),
    "C:/ProgramData": ("C:/ProgramData", True),
    "C:/ProgramData/*": ("C:/ProgramData/Vendor/config.dat", False),
    "*/AppData/Local/Programs/*": (
        "C:/Users/testuser_does_not_exist_xyz/AppData/Local/Programs/SomeApp/app.exe",
        False,
    ),
    "*/AppData/Local/Microsoft/WindowsApps/*": (
        "C:/Users/testuser_does_not_exist_xyz/AppData/Local/Microsoft/WindowsApps/App.exe",
        False,
    ),
}


def test_protected_root_representative_paths_cover_every_default_pattern() -> None:
    """Guards the gate itself (same discipline as `evals/test_safety_gate.py::
    test_hard_protected_categories_cover_all_manifest_protected_entries`): if
    `DEFAULT_PROTECTED_ROOTS` ever gains/loses an entry without this file being updated, this
    fails loudly instead of the parametrized test below silently under-covering it."""
    assert set(_PROTECTED_ROOT_CASES) == set(DEFAULT_PROTECTED_ROOTS)


@pytest.mark.parametrize(
    ("pattern", "representative"),
    list(_PROTECTED_ROOT_CASES.items()),
    ids=list(_PROTECTED_ROOT_CASES),
)
def test_every_default_protected_root_pattern_blocks_its_representative_path(
    pattern: str, representative: tuple[str, bool]
) -> None:
    path, is_dir = representative
    validator = SafetyValidator(Config())  # production defaults, no override
    result = validator.evaluate(_record(path, is_dir=is_dir))
    assert result.verdict == Verdict.BLOCKED, f"pattern {pattern!r} did not block {path!r}"
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


# NOTE: unlike `_PROTECTED_ROOT_CASES` above, `DEFAULT_DOCKER_WSL_ROOTS`'s 4 entries turned out
# NOT to share one reachable-reason-code shape -- `_builtin_deny`'s fixed check ORDER
# (protected_roots -> git-repo -> protected_extensions -> .ssh -> database -> vm -> docker_wsl ->
# cloud-placeholder) means 3 of the 4 real-world-representative paths for these patterns are
# actually caught by an EARLIER check first. Still BLOCKED in every case (no safety gap), but the
# specific reason code differs from DOCKER_WSL_DATA_ROOT for those 3 -- each is its own test
# below, documenting the real finding rather than picking an artificial representative path that
# would hide it.


def test_docker_wsl_root_patterns_are_all_exercised_by_name() -> None:
    """Guards the gate itself (same discipline as `evals/test_safety_gate.py::
    test_hard_protected_categories_cover_all_manifest_protected_entries`): if
    `DEFAULT_DOCKER_WSL_ROOTS` ever gains/loses an entry without this file being updated, this
    fails loudly instead of the tests below silently under-covering it."""
    exercised_patterns = {
        "*/AppData/Local/Docker/*",
        "*/AppData/Local/Packages/*WSL*/*",
        "*/ProgramData/Docker/*",
        "//wsl$/*",
    }
    assert exercised_patterns == set(DEFAULT_DOCKER_WSL_ROOTS)


def test_docker_root_appdata_local_docker_pattern_blocks_representative_path() -> None:
    """The one `DEFAULT_DOCKER_WSL_ROOTS` entry with no earlier-checked-list collision for a
    generic representative filename -- genuinely reaches `DOCKER_WSL_DATA_ROOT`."""
    validator = SafetyValidator(Config())
    record = _record("C:/Users/testuser_does_not_exist_xyz/AppData/Local/Docker/data.dat")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "DOCKER_WSL_DATA_ROOT"


def test_docker_root_wsl_packages_pattern_blocks_non_vm_extension_representative_path() -> None:
    """A non-`.vhdx`/`.vmdk`/`.qcow2` file under a `*WSL*`-named Packages directory (e.g. WSL's
    own metadata alongside the disk image) genuinely reaches DOCKER_WSL_DATA_ROOT -- see
    `test_wsl_packages_vhdx_disk_image_is_shadowed_by_vm_image_extension_check` below for why the
    disk image itself does NOT."""
    validator = SafetyValidator(Config())
    record = _record(
        "C:/Users/testuser_does_not_exist_xyz/AppData/Local/Packages/"
        "CanonicalGroupLimited.UbuntuWSL_79rhkp1fndgsc/LocalState/wsl_metadata.json"
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "DOCKER_WSL_DATA_ROOT"


def test_wsl_packages_vhdx_disk_image_is_shadowed_by_vm_image_extension_check() -> None:
    r"""FINDING (not a safety gap -- still BLOCKED either way): a real WSL2 distro's disk image
    is genuinely named `ext4.vhdx` under
    `...\AppData\Local\Packages\<Distro>\LocalState\ext4.vhdx` -- the exact real-world file the
    `*/AppData/Local/Packages/*WSL*/*` `docker_wsl_roots` pattern most plausibly exists to catch.
    But `_builtin_deny` checks `vm_extensions` (`.vhdx`/`.vmdk`/`.qcow2`) BEFORE `docker_wsl_roots`
    in its fixed order, so this exact real file is always blocked with reason VM_IMAGE, never
    DOCKER_WSL_DATA_ROOT -- the docker_wsl_roots pattern's own reason code is effectively
    unreachable for the one file it most plausibly targets. Confirmed here rather than assumed."""
    validator = SafetyValidator(Config())
    record = _record(
        "C:/Users/testuser_does_not_exist_xyz/AppData/Local/Packages/"
        "CanonicalGroupLimited.UbuntuWSL_79rhkp1fndgsc/LocalState/ext4.vhdx"
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "VM_IMAGE"


def test_programdata_docker_pattern_is_shadowed_by_protected_root_on_c_drive() -> None:
    """FINDING (not a safety gap -- still BLOCKED either way): under PRODUCTION defaults,
    `C:/ProgramData` and `C:/ProgramData/*` are already covered by `DEFAULT_PROTECTED_ROOTS`, and
    `protected_roots` is checked before `docker_wsl_roots` in `_builtin_deny` -- so Docker
    Desktop's real default ProgramData location (on the C: drive, the only realistic case for a
    default Windows install) is always blocked via PROTECTED_SYSTEM_ROOT, never
    DOCKER_WSL_DATA_ROOT. The `*/ProgramData/Docker/*` docker_wsl_roots entry is only reachable
    at all for a ProgramData relocated to a non-C: drive, which `DEFAULT_PROTECTED_ROOTS`'s
    C:-anchored ProgramData patterns don't cover."""
    validator = SafetyValidator(Config())
    record = _record("C:/ProgramData/Docker/config/daemon.json")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_wsl_unc_share_pattern_blocks_via_unc_check_not_docker_wsl_reason() -> None:
    r"""The `//wsl$/*` entry: a real `\\wsl$\Distro\...` path IS blocked (verdict BLOCKED), but
    via the UNC-network-path built-in deny (checked first, before ANY pattern-based list), never
    via DOCKER_WSL_DATA_ROOT -- the 4th and final `DEFAULT_DOCKER_WSL_ROOTS` reason-code
    shadowing case."""
    validator = SafetyValidator(Config())
    record = _record(r"\\wsl$\Ubuntu\home\user\.bashrc")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


@pytest.fixture
def validator_default() -> SafetyValidator:
    """Production-default `SafetyValidator` -- no protected_roots/deny/allow override -- for the
    extension-list coverage tests below, which don't touch `protected_roots` at all."""
    return SafetyValidator(Config())


def test_protected_extensions_cover_every_default_and_block(
    validator_default: SafetyValidator,
) -> None:
    assert set(DEFAULT_PROTECTED_EXTENSIONS)  # sanity: config actually has entries
    for ext in DEFAULT_PROTECTED_EXTENSIONS:
        result = validator_default.evaluate(_record(f"C:/Data/secret{ext}"))
        assert result.verdict == Verdict.BLOCKED, f"extension {ext!r} was not blocked"
        assert result.reason_code == "PROTECTED_EXTENSION"


def test_database_extensions_cover_every_default_and_block(
    validator_default: SafetyValidator,
) -> None:
    for ext in DEFAULT_DATABASE_EXTENSIONS:
        result = validator_default.evaluate(_record(f"C:/Data/app{ext}"))
        assert result.verdict == Verdict.BLOCKED, f"extension {ext!r} was not blocked"
        assert result.reason_code == "DATABASE_FILE"


def test_vm_extensions_cover_every_default_and_block(validator_default: SafetyValidator) -> None:
    for ext in DEFAULT_VM_EXTENSIONS:
        result = validator_default.evaluate(_record(f"C:/VMs/disk{ext}"))
        assert result.verdict == Verdict.BLOCKED, f"extension {ext!r} was not blocked"
        assert result.reason_code == "VM_IMAGE"


def test_ssh_directory_blocked_case_insensitive_and_mid_path(
    validator_default: SafetyValidator,
) -> None:
    """`.ssh` blocking must hold for a mixed-case segment (`.SSH`, Windows is case-insensitive)
    AND when it's a mid-path segment several levels deep, not just the final directory before the
    key file -- neither variant is covered by the existing `.ssh` test in `tests/test_safety.py`
    (which uses a single lowercase, leaf-adjacent `.ssh`)."""
    record = _record("C:/Users/someone/Documents/backup/.SSH/nested/deep/id_ed25519")
    result = validator_default.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "SSH_DIRECTORY"


# --- Section 2: `..`-traversal-style paths -------------------------------------------------------


def test_dotdot_traversal_into_protected_root_from_benign_looking_prefix_is_blocked() -> None:
    r"""A path string that never literally contains `C:\Windows` as a leading segment
    (`C:\Users\Public\..\..\Windows\System32\cmd.exe`) but RESOLVES into a real file physically
    under the protected root must still be blocked -- proving pattern matching runs against the
    canonicalized (`..`-collapsed) form, not the raw string. Verified manually that
    `Path(...).resolve()` collapses this to `C:\Windows\System32\cmd.exe` on this machine before
    writing this test."""
    validator = SafetyValidator(Config(safety=SafetyConfig(protected_roots=["C:/Windows/*"])))
    record = _record(r"C:\Users\Public\..\..\Windows\System32\cmd.exe")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_dotdot_traversal_cannot_smuggle_protected_file_behind_decoy_allow_listed_prefix() -> None:
    r"""The adversarial variant: a path whose RAW string starts inside an allow-listed directory
    (`*/allow-me/*`) but `..`-escapes out of it into a protected root
    (`C:\Data\allow-me\..\..\Windows\System32\kernel32.dll` -> resolves to
    `C:\Windows\System32\kernel32.dll`) must be BLOCKED, not promoted to ELIGIBLE by the allow-list
    match. This would be a real bypass if pattern matching ran against `record.path` directly
    instead of the canonicalized form -- the raw string literally contains `/allow-me/`, which
    `fnmatch`-matches `*/allow-me/*`."""
    validator = SafetyValidator(
        Config(
            safety=SafetyConfig(
                protected_roots=["C:/Windows/*"],
                allow=["*/allow-me/*"],
            )
        )
    )
    record = _record(r"C:\Data\allow-me\..\..\Windows\System32\kernel32.dll")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


# --- Section 3: git-repo / node_modules exemption boundary ---------------------------------------


def test_node_modules_lookalike_directory_name_is_not_exempted() -> None:
    """A directory whose name merely CONTAINS `node_modules` as a substring
    (`my_node_modules_lookalike`), not as an exact path segment, must NOT get the node_modules
    dev-artifacts exemption -- even in a clean repo with the category enabled. Proves
    `_has_path_segment`'s exact-segment-equality check can't be fooled by an attacker/careless
    naming a directory to look like node_modules to slip a regular git-tracked file past the
    in-repo block."""
    validator = SafetyValidator(
        Config(categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)))
    )
    record = _record(
        "C:/Data/repo/my_node_modules_lookalike/index.js",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=True,
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "IN_GIT_REPOSITORY"


def test_node_modules_exemption_is_case_insensitive() -> None:
    """The positive counterpart: an uppercase `NODE_MODULES` segment DOES get the exemption
    (Windows is case-insensitive, and `_has_path_segment` lowers both sides) -- confirmed with a
    real test rather than assumed, since no existing test exercises a non-lowercase node_modules
    segment."""
    validator = SafetyValidator(
        Config(categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)))
    )
    record = _record(
        "C:/Data/repo/NODE_MODULES/pkg/index.js",
        git_repo_root=Path("C:/Data/repo"),
        git_repo_clean=True,
    )
    result = validator.evaluate(record)
    assert result.verdict == Verdict.ELIGIBLE
    assert result.reason_code == "DEV_ARTIFACTS_NODE_MODULES_EXEMPT"


# --- Section 4: cloud-placeholder built-in-deny precedence ----------------------------------------


def test_cloud_placeholder_beats_user_allow_list() -> None:
    """`is_cloud_placeholder` is checked inside `_builtin_deny` (per `safety.py`'s own code, not
    an assumption) -- so, matching every other built-in deny reason, a user allow-list entry can
    never override it. Confirms the documented behavior is actually BLOCK, not e.g. review-only,
    and that it holds even when the path is explicitly allow-listed."""
    validator = SafetyValidator(Config(safety=SafetyConfig(allow=["*/CloudSync/*"])))
    record = _record("C:/CloudSync/photo.jpg", attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "CLOUD_PLACEHOLDER"


# --- Section 5: precedence proven directly (not rule-by-rule) -------------------------------------


def test_builtin_deny_beats_user_allow_list_and_finance_token_simultaneously() -> None:
    """Stress version of the documented precedence's top rule: a single path matches a built-in
    deny pattern (`protected_roots`) AND a user allow-list entry AND a finance-document filename
    token all at once -- built-in deny must still win outright (BLOCKED
    PROTECTED_SYSTEM_ROOT), not merely when only one of the lower-precedence rules is also in
    play (`tests/test_safety.py::test_builtin_deny_beats_user_allow` only combines two)."""
    validator = SafetyValidator(
        Config(safety=SafetyConfig(protected_roots=["C:/Protected/*"], allow=["*/Protected/*"]))
    )
    record = _record("C:/Protected/2024_tax_invoice.pdf")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "PROTECTED_SYSTEM_ROOT"


def test_user_deny_list_beats_user_allow_list_and_finance_token_simultaneously() -> None:
    """Same stress shape one level down: a path matches BOTH a user deny-list pattern AND a user
    allow-list pattern (deliberately overlapping config) AND carries a finance-document token --
    the user deny-list must win (BLOCKED USER_DENY_LIST), proving the allow-list's
    finance-token override mechanism (`USER_ALLOW_LIST_OVERRIDE`) can never fire once the
    deny-list has already matched. Not covered by
    `tests/test_safety.py::test_user_deny_beats_default_eligible`, which only tests deny vs.
    default-eligible, never deny vs. a competing allow-list match."""
    validator = SafetyValidator(
        Config(
            safety=SafetyConfig(
                deny=["*/UserDenied/*"],
                allow=["*/UserDenied/*"],
            )
        )
    )
    record = _record("C:/UserDenied/2024_tax_invoice.pdf")
    result = validator.evaluate(record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "USER_DENY_LIST"


# --- Section 6: `path_is_protected_root()` exercised independently of `evaluate()` ----------------


def test_path_is_protected_root_does_not_cover_extension_or_ssh_based_blocks() -> None:
    """Documented scope limitation (`path_is_protected_root`'s own docstring): it checks ONLY
    `protected_roots`/`docker_wsl_roots`/UNC-form, never `protected_extensions`/database/VM
    extensions/`.ssh`/deny-list/finance tokens -- those all need a stat or accept an ambiguity
    that's acceptable for a scan decision but not a restore-destination guard. Confirms that
    documented narrower scope actually holds (a `.kdbx`/`.db` path with no protected-root overlap
    is NOT flagged by this check) rather than assuming it from the docstring alone -- this is
    intentional, by-design behavior, not a gap: `executor.restore_batch` only needs "never
    recreate a file under a protected root," and every restored file's original safety verdict
    was already computed by the full `evaluate()` pipeline before it was ever quarantined."""
    validator = SafetyValidator(Config(safety=SafetyConfig(protected_roots=["C:/Windows/*"])))
    assert validator.path_is_protected_root(Path("C:/Data/secret.kdbx")) is False
    assert validator.path_is_protected_root(Path("C:/Data/app.db")) is False
    assert validator.path_is_protected_root(Path("C:/Users/gg/.ssh/id_rsa")) is False


def test_path_is_protected_root_denies_junction_into_protected_root(tmp_path: Path) -> None:
    r"""`restore_batch`'s guard is a SEPARATE code path from `evaluate()` -- proven independently
    here with a real NTFS junction (`mklink /J`), mirroring
    `tests/test_safety.py::test_junction_into_protected_root_denied` but calling
    `path_is_protected_root()` instead of `evaluate()`, so a future change that canonicalizes one
    call site but not the other would be caught by this test even if the `evaluate()` one still
    passes. Cleans up the junction in `finally` even if the assertion fails."""
    real_dir = tmp_path / "Protected Root For Restore Guard Junction"
    real_dir.mkdir()
    (real_dir / "payload.dll").write_text("stand-in")

    innocuous = tmp_path / "innocuous_restore_target"
    created = subprocess.run(  # noqa: S603 -- fixed test args, not untrusted input
        ["cmd", "/c", "mklink", "/J", str(innocuous), str(real_dir)],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"mklink /J failed on this machine: {created.stderr.strip()}")
    try:
        junctioned_path = innocuous / "payload.dll"
        assert junctioned_path.exists(), "junction must resolve to the same real file"

        validator = SafetyValidator(
            Config(safety=SafetyConfig(protected_roots=[f"{real_dir.as_posix()}/*"]))
        )
        assert validator.path_is_protected_root(junctioned_path) is True
    finally:
        innocuous.rmdir()
