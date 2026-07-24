from __future__ import annotations

from pathlib import Path

import pytest

from reclaim.config import CategoriesConfig, Config, DevArtifactsConfig, SafetyConfig
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
