from __future__ import annotations

from pathlib import Path

import pytest
from fixtures.build_golden_tree import FixtureCase, build_golden_tree

from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
from reclaim.models import Verdict
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
