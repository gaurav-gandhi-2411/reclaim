from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.config import CategoriesConfig, Config, SafetyConfig
from reclaim.models import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FileRecord, Verdict
from reclaim.safety import SafetyValidator


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
            categories=CategoriesConfig(dev_artifacts=True),
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
    disabled_validator = SafetyValidator(Config(categories=CategoriesConfig(dev_artifacts=False)))
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
