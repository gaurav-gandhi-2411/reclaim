from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.build_golden_tree import FixtureCase, build_golden_tree

from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
from reclaim.models import FileRecord, Verdict
from reclaim.safety import SafetyValidator

# Spec: "Never enters candidate list" — every category listed there must never come out
# ELIGIBLE, independent of what the manifest's `expected_verdict` says. Categories not in
# this set (finance-doc override, node_modules exemption, benign files, the finance
# review-only baseline) are legitimately allowed to be ELIGIBLE under the right conditions.
_HARD_PROTECTED_CATEGORIES = {
    "protected_root_windows",
    "protected_root_program_files",
    "protected_root_program_files_x86",
    "protected_root_programdata",
    "protected_root_appdata_programs",
    "protected_root_appdata_windowsapps",
    "git_repo_root_dir",
    "git_repo_root_dir_dirty",
    "git_repo_file",
    "git_repo_node_modules_dirty",
    "protected_extension_kdbx",
    "protected_extension_ppk",
    "protected_extension_pem",
    "protected_extension_key",
    "protected_extension_pfx",
    "protected_extension_crt",
    "protected_extension_gpg",
    "ssh_dir_file",
    "database_db",
    "database_sqlite",
    "database_mdf",
    "vm_image_vhdx",
    "vm_image_vmdk",
    "vm_image_qcow2",
    "docker_wsl_root",
    "cloud_placeholder",
    "user_deny_list_hit",
}


@pytest.fixture
def golden_tree_config(tmp_path: Path) -> Config:
    root = tmp_path.as_posix()
    return Config(
        safety=SafetyConfig(
            protected_roots=[
                f"{root}/Windows",
                f"{root}/Windows/*",
                f"{root}/Program Files",
                f"{root}/Program Files/*",
                f"{root}/Program Files (x86)",
                f"{root}/Program Files (x86)/*",
                f"{root}/ProgramData",
                f"{root}/ProgramData/*",
                "*/AppData/Local/Programs/*",
                "*/AppData/Local/Microsoft/WindowsApps/*",
            ],
            deny=["*/Random/blocked_by_policy.tmp"],
            allow=["*/old_invoice_2019.pdf"],
        ),
        categories=CategoriesConfig(dev_artifacts=DevArtifactsConfig(enabled=True)),
    )


@pytest.fixture
def golden_tree_cases(tmp_path: Path) -> list[FixtureCase]:
    return build_golden_tree(tmp_path)


def test_every_case_matches_expected_verdict(
    golden_tree_cases: list[FixtureCase], golden_tree_config: Config
) -> None:
    """Catches both false negatives (protected file leaks eligible) and false positives
    (benign file wrongly blocked)."""
    validator = SafetyValidator(golden_tree_config)
    failures: list[str] = []
    for case in golden_tree_cases:
        result = validator.evaluate(case.to_file_record())
        if result.verdict != case.expected_verdict:
            failures.append(
                f"{case.id}: expected verdict {case.expected_verdict}, got {result.verdict} "
                f"(reason={result.reason_code}, rationale={result.rationale!r})"
            )
        elif case.expected_reason_contains.lower() not in result.rationale.lower():
            failures.append(
                f"{case.id}: expected rationale to contain {case.expected_reason_contains!r}, "
                f"got {result.rationale!r}"
            )
    assert not failures, "golden fixture mismatches:\n" + "\n".join(failures)


def test_hard_gate_zero_protected_files_ever_eligible(
    golden_tree_cases: list[FixtureCase], golden_tree_config: Config
) -> None:
    """The hard CI gate: no protected-category fixture may ever be classified ELIGIBLE.

    Independent of `test_every_case_matches_expected_verdict` — this reruns every
    protected-category case and asserts on `Verdict.ELIGIBLE` directly, so a manifest
    typo in `expected_verdict` can't silently defeat the safety gate.
    """
    validator = SafetyValidator(golden_tree_config)
    leaked: list[str] = []
    for case in golden_tree_cases:
        if case.category not in _HARD_PROTECTED_CATEGORIES:
            continue
        result = validator.evaluate(case.to_file_record())
        if result.verdict == Verdict.ELIGIBLE:
            leaked.append(
                f"{case.id} (category={case.category}, path={case.path}) leaked through as "
                f"ELIGIBLE — reason_code={result.reason_code}, rationale={result.rationale!r}"
            )
    assert not leaked, (
        f"SAFETY GATE FAILURE: {len(leaked)} protected-category fixture(s) were classified "
        "ELIGIBLE:\n" + "\n".join(leaked)
    )


def test_hard_protected_categories_cover_all_manifest_protected_entries(
    golden_tree_cases: list[FixtureCase],
) -> None:
    """Guards the gate itself: every fixture whose expected_verdict is BLOCKED, other than
    the deliberately-not-hard-protected node_modules/allow-override exemptions, must be
    represented in `_HARD_PROTECTED_CATEGORIES` — otherwise the hard gate above is
    silently under-covering the fixture tree."""
    blocked_categories = {
        case.category for case in golden_tree_cases if case.expected_verdict == Verdict.BLOCKED
    }
    missing = blocked_categories - _HARD_PROTECTED_CATEGORIES
    assert not missing, f"BLOCKED categories missing from the hard gate set: {missing}"


# --- D13: UNC-aliased-path safety bypass ---------------------------------------------------------


def _unc_alias(path: Path) -> Path:
    r"""Rewrites a real drive-letter path (`C:\some\dir\file`) into its UNC administrative-share
    alias (`\\localhost\C$\some\dir\file`) -- the exact same on-disk file, a different string
    form. Used to prove the golden-tree's real, physically-materialized protected-root fixture is
    still denied when addressed by its UNC alias instead of its drive-letter form."""
    drive = path.drive  # e.g. "C:"
    letter = drive.rstrip(":")
    rest = str(path)[len(drive) :]
    return Path(f"\\\\localhost\\{letter}$" + rest)


def test_unc_alias_of_real_protected_root_fixture_is_denied(
    golden_tree_cases: list[FixtureCase], golden_tree_config: Config
) -> None:
    """The audit's exact adversarial case, against a REAL materialized fixture file (not just a
    synthetic path): the golden tree's `protected_root_windows` case is a real file physically
    under `<tmp_path>/Windows/...`; addressed via its UNC admin-share alias
    (`\\localhost\<drive>$/...`) instead of its drive-letter path, it must still be BLOCKED --
    proving the pattern-based `protected_roots` deny list cannot be bypassed by UNC aliasing even
    against a genuine on-disk file, not merely a hand-built path string."""
    validator = SafetyValidator(golden_tree_config)
    real_case = next(c for c in golden_tree_cases if c.category == "protected_root_windows")
    aliased_record = FileRecord(
        path=_unc_alias(real_case.path),
        is_dir=real_case.kind == "dir",
        size_bytes=real_case.size_bytes,
        attributes=real_case.attributes,
        ext=real_case.path.suffix.lower(),
        git_repo_root=real_case.git_repo_root,
        git_repo_clean=real_case.git_repo_clean,
    )
    result = validator.evaluate(aliased_record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"


def test_unc_alias_of_real_docker_wsl_root_fixture_is_denied(
    golden_tree_cases: list[FixtureCase], golden_tree_config: Config
) -> None:
    """Same proof as above, for the `docker_wsl_root` category -- confirms the same built-in-deny
    protection (BLOCKED verdict, upstream of any pattern match) applies for every pattern-based
    deny list `_any_pattern_matches` backs, not just `protected_roots`. (`DEFAULT_DOCKER_WSL_ROOTS`
    entries are all leading-`*/`-relative, so this particular real fixture was incidentally still
    caught by the pre-existing pattern match too, just under `DOCKER_WSL_DATA_ROOT` instead of
    `UNC_NETWORK_PATH` -- `tests/test_safety.py::
    test_unc_alias_of_drive_anchored_pattern_list_entry_denied` is the genuine
    false-negative-closure proof, using a drive-anchored pattern.)"""
    validator = SafetyValidator(golden_tree_config)
    real_case = next(c for c in golden_tree_cases if c.category == "docker_wsl_root")
    aliased_record = FileRecord(
        path=_unc_alias(real_case.path),
        is_dir=real_case.kind == "dir",
        size_bytes=real_case.size_bytes,
        attributes=real_case.attributes,
        ext=real_case.path.suffix.lower(),
        git_repo_root=real_case.git_repo_root,
        git_repo_clean=real_case.git_repo_clean,
    )
    result = validator.evaluate(aliased_record)
    assert result.verdict == Verdict.BLOCKED
    assert result.reason_code == "UNC_NETWORK_PATH"
