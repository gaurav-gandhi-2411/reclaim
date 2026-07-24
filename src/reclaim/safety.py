from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from reclaim.config import Config
from reclaim.models import FileRecord, SafetyResult, Verdict

REASON_PROTECTED_SYSTEM_ROOT = "PROTECTED_SYSTEM_ROOT"
REASON_IN_GIT_REPOSITORY = "IN_GIT_REPOSITORY"
REASON_GIT_NODE_MODULES_DIRTY = "GIT_REPO_NODE_MODULES_DIRTY"
REASON_DEV_ARTIFACTS_NODE_MODULES_EXEMPT = "DEV_ARTIFACTS_NODE_MODULES_EXEMPT"
REASON_PROTECTED_EXTENSION = "PROTECTED_EXTENSION"
REASON_SSH_DIRECTORY = "SSH_DIRECTORY"
REASON_DATABASE_FILE = "DATABASE_FILE"
REASON_VM_IMAGE = "VM_IMAGE"
REASON_DOCKER_WSL_ROOT = "DOCKER_WSL_DATA_ROOT"
REASON_CLOUD_PLACEHOLDER = "CLOUD_PLACEHOLDER"
REASON_USER_DENY_LIST = "USER_DENY_LIST"
REASON_FINANCE_LEGAL_DOCUMENT = "FINANCE_LEGAL_DOCUMENT"
REASON_USER_ALLOW_LIST_OVERRIDE = "USER_ALLOW_LIST_OVERRIDE"
REASON_USER_ALLOW_LIST = "USER_ALLOW_LIST"
REASON_DEFAULT_ELIGIBLE = "DEFAULT_ELIGIBLE"
REASON_UNC_NETWORK_PATH = "UNC_NETWORK_PATH"

# (reason_code, rationale) pair carried through when a built-in deny check is skipped by
# an exemption, so the eventual ELIGIBLE result still explains *why* rather than falling
# back to the generic default-eligible rationale.
_Exemption = tuple[str, str]


def _strip_local_extended_length_prefix(path: Path) -> Path:
    r"""Strips a leading `\\?\` extended-length-path prefix off a normal LOCAL drive-letter path
    (`\\?\C:\Windows\...` -> `C:\Windows\...`) so pattern matching sees the same string whether or
    not a caller's path happens to carry D12's MAX_PATH-bypass prefix. Never called for a UNC
    path (`_is_unc_network_path` is checked ahead of every pattern-based list, so a `\\?\UNC\...`
    path is denied before it would reach here) -- `path.drive` is used, not `str.removeprefix`,
    for the same "reparse the drive segment structurally, not by string luck" reasoning as
    `_is_unc_network_path`.
    """
    drive = path.drive
    if drive.lower().startswith("\\\\?\\") and not drive.lower().startswith("\\\\?\\unc\\"):
        return Path(str(path)[4:])
    return path


def _pattern_matches(path: Path, pattern: str) -> bool:
    candidate = _strip_local_extended_length_prefix(path).as_posix()
    if pattern.startswith("re:"):
        return re.search(pattern[3:], candidate, flags=re.IGNORECASE) is not None
    return fnmatch.fnmatch(candidate.lower(), pattern.lower())


def _any_pattern_matches(path: Path, patterns: Sequence[str]) -> bool:
    return any(_pattern_matches(path, pattern) for pattern in patterns)


def _is_unc_network_path(path: Path) -> bool:
    r"""True for any path expressed in UNC network-share form (`\\host\share\...`), including
    the `\\?\UNC\host\share\...` extended-length form -- False for every drive-letter path,
    including one carrying the `\\?\` extended-length-path prefix (`\\?\C:\...`, D12's local
    MAX_PATH bypass, a completely different mechanism -- see `scanner.long_path`).

    D13: `DEFAULT_PROTECTED_ROOTS`/`DEFAULT_DOCKER_WSL_ROOTS` are entirely drive-letter-form glob
    patterns matched against `path.as_posix()` (`_pattern_matches`). A UNC administrative-share
    alias of the exact same file (`\\localhost\C$\Windows\...`, `\\127.0.0.1\C$\...`, or any other
    hostname alias for "this machine") produces a `.as_posix()` string (`//localhost/C$/...`)
    that never matches a `C:/Windows/*`-style pattern, even though it denotes the identical
    on-disk file -- so every pattern-based deny check silently passed it. `path.drive` is used
    (not `.as_posix()`/`str()`) because it isolates exactly the prefix that distinguishes "UNC
    share" from "drive letter" without needing to reparse separators by hand.
    """
    drive = path.drive.lower()
    if not drive.startswith("\\\\"):
        return False
    if drive.startswith("\\\\?\\"):
        # Extended-length prefix: only the `\\?\UNC\...` form is a real network share in its
        # long-path-safe spelling. `\\?\<drive-letter>:` (e.g. `\\?\C:`) is a normal local path
        # that merely opted into the length-limit bypass -- must NOT be treated as UNC.
        return drive.startswith("\\\\?\\unc\\")
    return True


def _has_path_segment(path: Path, segment: str) -> bool:
    segment_lower = segment.lower()
    return any(part.lower() == segment_lower for part in path.parts)


def _matched_finance_token(record: FileRecord, tokens: Sequence[str]) -> str | None:
    name = record.path.name.lower()
    for token in tokens:
        if token.lower() in name:
            return token
    return None


class SafetyValidator:
    """Deny-first gate. Runs before any file enters the candidate pipeline (spec principle 3).

    Precedence, highest wins: built-in deny > user deny-list > built-in review-only >
    user allow-list > default eligible. Nothing below built-in deny can ever override it,
    including the user allow-list.
    """

    def __init__(self, config: Config) -> None:
        self._safety = config.safety
        self._dev_artifacts_enabled = config.categories.dev_artifacts.enabled

    def evaluate(self, record: FileRecord) -> SafetyResult:
        blocked, exemption = self._builtin_deny(record)
        if blocked is not None:
            return blocked

        if _any_pattern_matches(record.path, self._safety.deny):
            return SafetyResult(
                record=record,
                verdict=Verdict.BLOCKED,
                reason_code=REASON_USER_DENY_LIST,
                rationale=(
                    "Path matches a user-configured deny-list pattern in config.toml "
                    "[safety.deny] — blocked regardless of any allow-list entry."
                ),
            )

        finance_token = _matched_finance_token(record, self._safety.finance_tokens)
        allow_hit = _any_pattern_matches(record.path, self._safety.allow)

        if finance_token is not None:
            if allow_hit:
                return SafetyResult(
                    record=record,
                    verdict=Verdict.ELIGIBLE,
                    reason_code=REASON_USER_ALLOW_LIST_OVERRIDE,
                    rationale=(
                        f"Filename matches finance/tax/legal pattern (token: '{finance_token}') "
                        "but is explicitly allow-listed in config.toml [safety.allow] — "
                        "promoted from review-only to eligible."
                    ),
                )
            return SafetyResult(
                record=record,
                verdict=Verdict.REVIEW_ONLY,
                reason_code=REASON_FINANCE_LEGAL_DOCUMENT,
                rationale=(
                    f"Filename matches a finance/tax/legal document pattern "
                    f"(token: '{finance_token}') — routed to manual review, never auto-quarantined."
                ),
            )

        if allow_hit:
            return SafetyResult(
                record=record,
                verdict=Verdict.ELIGIBLE,
                reason_code=REASON_USER_ALLOW_LIST,
                rationale=(
                    "Path matches a user-configured allow-list pattern in config.toml "
                    "[safety.allow] — eligible for the normal candidate pipeline."
                ),
            )

        if exemption is not None:
            reason_code, rationale = exemption
            return SafetyResult(
                record=record,
                verdict=Verdict.ELIGIBLE,
                reason_code=reason_code,
                rationale=rationale,
            )

        return SafetyResult(
            record=record,
            verdict=Verdict.ELIGIBLE,
            reason_code=REASON_DEFAULT_ELIGIBLE,
            rationale=(
                "No protected-root, git-repo, protected-extension, database/VM, "
                "cloud-placeholder, deny-list, or finance-document rule matched — eligible "
                "for the normal candidate pipeline."
            ),
        )

    def filter_candidates(self, records: Iterable[FileRecord]) -> list[SafetyResult]:
        return [self.evaluate(record) for record in records]

    def path_is_protected_root(self, path: Path) -> bool:
        """Pattern-only check usable when no `FileRecord`/stat is available — e.g.
        `executor.restore_batch` validating a restore *destination* that doesn't exist yet (the
        file is about to be recreated there, so there's nothing to stat).

        Checks only the two `_builtin_deny` sub-checks that need no live stat or git-repo state
        (`protected_roots`, `docker_wsl_roots`), plus the UNC-network-path check (D13, also stat-
        free) — not the full `evaluate()` precedence chain (extensions, cloud-placeholder, finance
        tokens, user allow/deny lists all either need a stat or accept an ambiguity that's fine for
        a proactive scan decision but not for a last-resort "never write here" restore guard, where
        a false negative is the only acceptable failure mode and a false positive just means one
        restore item is refused).
        """
        cfg = self._safety
        return (
            _is_unc_network_path(path)
            or _any_pattern_matches(path, cfg.protected_roots)
            or _any_pattern_matches(path, cfg.docker_wsl_roots)
        )

    def _builtin_deny(self, record: FileRecord) -> tuple[SafetyResult | None, _Exemption | None]:
        cfg = self._safety

        # D13: checked before every pattern-based deny list (protected_roots, docker_wsl_roots,
        # and any future one) — a UNC network-share path can alias any drive-letter path those
        # lists are trying to protect without ever matching a drive-letter-form glob pattern (see
        # `_is_unc_network_path`'s docstring). This tool is a local-disk cleanup tool: every real
        # local file is always reachable via its drive letter too, so a UNC form is never required
        # to reach anything this tool legitimately needs to touch — blanket-denying the entire
        # class is strictly safer than trying to enumerate every "this machine" alias
        # (localhost/127.0.0.1/::1/the real hostname/`.`), which would itself be an incomplete and
        # fragile allowlist.
        if _is_unc_network_path(record.path):
            return self._blocked(
                record,
                REASON_UNC_NETWORK_PATH,
                "Path is expressed in UNC network-share form (\\\\host\\share\\... or its "
                "\\\\?\\UNC\\host\\share\\... extended-length form) — blocked outright rather "
                "than pattern-matched, because a UNC alias (including a localhost/loopback "
                "administrative share like \\\\localhost\\C$\\...) can reference the exact same "
                "on-disk file as a protected drive-letter path without ever matching that path's "
                "drive-letter-form deny pattern. A drive letter always reaches any real local "
                "file, so no legitimate scan/apply/restore target ever requires UNC form.",
            ), None

        if _any_pattern_matches(record.path, cfg.protected_roots):
            return self._blocked(
                record,
                REASON_PROTECTED_SYSTEM_ROOT,
                "Path lies under a protected Windows system root (Windows, Program Files, "
                "ProgramData, or an AppData binary directory) — never auto-quarantine eligible.",
            ), None

        exemption: _Exemption | None = None
        if record.git_repo_root is not None:
            in_node_modules = _has_path_segment(record.path, "node_modules")
            exempt = in_node_modules and record.git_repo_clean and self._dev_artifacts_enabled
            if not exempt:
                if in_node_modules and not record.git_repo_clean:
                    return self._blocked(
                        record,
                        REASON_GIT_NODE_MODULES_DIRTY,
                        f"Path is inside 'node_modules' of git repo '{record.git_repo_root}', but "
                        "the repo working tree is not clean, so the dev-artifacts exemption does "
                        "not apply — blocked as an in-repo file.",
                    ), None
                return self._blocked(
                    record,
                    REASON_IN_GIT_REPOSITORY,
                    f"Path is inside a git repository rooted at '{record.git_repo_root}' — "
                    "in-repo files are blocked from automated quarantine to protect repository "
                    "integrity.",
                ), None
            exemption = (
                REASON_DEV_ARTIFACTS_NODE_MODULES_EXEMPT,
                f"Path is inside 'node_modules' under a clean git repository "
                f"('{record.git_repo_root}') with the dev-artifacts category enabled, so the "
                "in-repo block does not apply — eligible for the normal candidate pipeline.",
            )

        if record.ext in cfg.protected_extensions:
            return self._blocked(
                record,
                REASON_PROTECTED_EXTENSION,
                f"Extension '{record.ext}' is in the protected credential/secret extension list "
                "(e.g. .kdbx, .pem, .key) — blocked to prevent deleting secrets or credentials.",
            ), None

        if _has_path_segment(record.path, ".ssh"):
            return self._blocked(
                record,
                REASON_SSH_DIRECTORY,
                "Path contains a '.ssh' directory segment — blocked to prevent deleting SSH keys "
                "or credentials.",
            ), None

        if record.ext in cfg.database_extensions:
            return self._blocked(
                record,
                REASON_DATABASE_FILE,
                f"Extension '{record.ext}' identifies a database file — blocked to prevent data "
                "loss.",
            ), None

        if record.ext in cfg.vm_extensions:
            return self._blocked(
                record,
                REASON_VM_IMAGE,
                f"Extension '{record.ext}' identifies a virtual machine disk image — blocked to "
                "prevent data loss.",
            ), None

        if _any_pattern_matches(record.path, cfg.docker_wsl_roots):
            return self._blocked(
                record,
                REASON_DOCKER_WSL_ROOT,
                "Path is under a Docker/WSL data root — blocked to prevent corrupting container "
                "or WSL distro state.",
            ), None

        if record.is_cloud_placeholder:
            return self._blocked(
                record,
                REASON_CLOUD_PLACEHOLDER,
                "File is a cloud-only placeholder (not synced locally) — deleting it frees no "
                "local space and destroys the cloud copy.",
            ), None

        return None, exemption

    @staticmethod
    def _blocked(record: FileRecord, reason_code: str, rationale: str) -> SafetyResult:
        return SafetyResult(
            record=record, verdict=Verdict.BLOCKED, reason_code=reason_code, rationale=rationale
        )
